"""Pure-Python SMS PDU and modem-response helpers.

The module has no USB or serial side effects.  PDU strings are canonical upper-case
hexadecimal strings and include the SMSC length prefix expected by ``AT+CMGS``.
"""

from __future__ import annotations

import csv
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence


class SMSCodecError(ValueError):
    """Raised when SMS input or modem output cannot be represented safely."""


@dataclass(frozen=True)
class ConcatInfo:
    reference: int
    total_parts: int
    part_number: int | None
    reference_bits: int


@dataclass(frozen=True)
class DecodedSMS:
    sender: str | None
    destination: str | None
    text: str | None
    data: bytes
    encoding: str
    dcs: int
    timestamp: datetime | None
    service_center: str | None
    concat: ConcatInfo | None
    raw_pdu: str
    index: int | None = None
    storage: str | None = None
    status: str | None = None
    complete: bool = True
    parts_received: tuple[int, ...] = ()
    raw_pdus: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutgoingPart:
    pdu: str
    tpdu_length: int
    text: str
    encoding: str
    part_number: int
    total_parts: int
    concat: ConcatInfo | None


@dataclass(frozen=True)
class SMSStatusReport:
    message_reference: int
    recipient: str
    service_center_time: datetime | None
    discharge_time: datetime | None
    status: int
    outcome: str
    service_center: str | None
    raw_pdu: str


# 3GPP TS 23.038 default alphabet.  Index is the septet value.
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

if len(_GSM7_BASIC) != 128:  # Defensive check for accidental table edits.
    raise RuntimeError("invalid GSM 7-bit alphabet table")

_GSM7_EXTENSION_DECODE = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}
_GSM7_BASIC_ENCODE = {
    character: value
    for value, character in enumerate(_GSM7_BASIC)
    if value != 0x1B
}
_GSM7_EXTENSION_ENCODE = {
    character: value for value, character in _GSM7_EXTENSION_DECODE.items()
}

_PHONE_RE = re.compile(r"^\+?[0-9]{3,15}$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


def validate_phone_number(number: str) -> str:
    """Validate an outbound numeric address without silently normalizing it."""

    if not isinstance(number, str) or not _PHONE_RE.fullmatch(number):
        raise SMSCodecError("phone number must be 3-15 ASCII digits with an optional leading +")
    return number


def gsm7_can_encode(text: str) -> bool:
    if not isinstance(text, str):
        return False
    return all(
        character in _GSM7_BASIC_ENCODE or character in _GSM7_EXTENSION_ENCODE
        for character in text
    )


def gsm7_encode(text: str) -> list[int]:
    """Return GSM 7-bit septets, including ESC pairs for extension characters."""

    if not isinstance(text, str):
        raise SMSCodecError("GSM 7-bit input must be text")
    result: list[int] = []
    for character in text:
        if character in _GSM7_BASIC_ENCODE:
            result.append(_GSM7_BASIC_ENCODE[character])
        elif character in _GSM7_EXTENSION_ENCODE:
            result.extend((0x1B, _GSM7_EXTENSION_ENCODE[character]))
        else:
            raise SMSCodecError(f"character is not representable in GSM 7-bit: U+{ord(character):04X}")
    return result


def gsm7_decode(septets: Sequence[int]) -> str:
    result: list[str] = []
    position = 0
    while position < len(septets):
        value = septets[position]
        if not isinstance(value, int) or not 0 <= value <= 0x7F:
            raise SMSCodecError("invalid GSM 7-bit septet")
        if value != 0x1B:
            result.append(_GSM7_BASIC[value])
            position += 1
            continue
        if position + 1 >= len(septets):
            raise SMSCodecError("truncated GSM 7-bit extension escape")
        extension = septets[position + 1]
        if not isinstance(extension, int) or not 0 <= extension <= 0x7F:
            raise SMSCodecError("invalid GSM 7-bit extension septet")
        character = _GSM7_EXTENSION_DECODE.get(extension)
        if character is None:
            # TS 23.038 requires unknown extension codes to fall back to the
            # corresponding default-alphabet character; ESC ESC displays space.
            character = " " if extension == 0x1B else _GSM7_BASIC[extension]
        result.append(character)
        position += 2
    return "".join(result)


def _pack_septets(
    septets: Sequence[int], *, prefix: bytes = b"", bit_offset: int = 0
) -> bytes:
    if bit_offset < len(prefix) * 8:
        raise SMSCodecError("septet bit offset overlaps prefix")
    total_bits = bit_offset + len(septets) * 7
    result = bytearray(prefix)
    required_bytes = (total_bits + 7) // 8
    if len(result) < required_bytes:
        result.extend(b"\x00" * (required_bytes - len(result)))
    cursor = bit_offset
    for septet in septets:
        if not isinstance(septet, int) or not 0 <= septet <= 0x7F:
            raise SMSCodecError("invalid GSM 7-bit septet")
        for bit in range(7):
            if septet & (1 << bit):
                result[cursor // 8] |= 1 << (cursor % 8)
            cursor += 1
    return bytes(result)


def _unpack_septets(data: bytes, septet_count: int, *, bit_offset: int = 0) -> list[int]:
    if not isinstance(septet_count, int) or septet_count < 0:
        raise SMSCodecError("septet count must be a non-negative integer")
    if bit_offset < 0 or bit_offset + septet_count * 7 > len(data) * 8:
        raise SMSCodecError("packed GSM 7-bit data is shorter than its declared length")
    result: list[int] = []
    cursor = bit_offset
    for _ in range(septet_count):
        value = 0
        for bit in range(7):
            if data[cursor // 8] & (1 << (cursor % 8)):
                value |= 1 << bit
            cursor += 1
        result.append(value)
    return result


def gsm7_pack(text: str, *, fill_bits: int = 0) -> bytes:
    """Pack text into GSM septets, optionally leaving 0-6 leading fill bits."""

    if not isinstance(fill_bits, int) or not 0 <= fill_bits <= 6:
        raise SMSCodecError("GSM 7-bit fill_bits must be between 0 and 6")
    return _pack_septets(gsm7_encode(text), bit_offset=fill_bits)


def gsm7_unpack(data: bytes, septet_count: int, *, fill_bits: int = 0) -> str:
    """Unpack exactly ``septet_count`` characters, ignoring unused high spare bits."""

    if not isinstance(data, bytes):
        raise SMSCodecError("packed GSM 7-bit data must be bytes")
    if not isinstance(fill_bits, int) or not 0 <= fill_bits <= 6:
        raise SMSCodecError("GSM 7-bit fill_bits must be between 0 and 6")
    return gsm7_decode(_unpack_septets(data, septet_count, bit_offset=fill_bits))


def encode_ucs2(text: str) -> bytes:
    if not isinstance(text, str):
        raise SMSCodecError("UCS2 input must be text")
    try:
        return text.encode("utf-16-be", errors="strict")
    except UnicodeEncodeError as error:
        raise SMSCodecError("text contains an invalid Unicode surrogate") from error


def decode_ucs2(data: bytes) -> str:
    if not isinstance(data, bytes) or len(data) % 2:
        raise SMSCodecError("UCS2 data must contain complete UTF-16BE code units")
    try:
        return data.decode("utf-16-be", errors="strict")
    except UnicodeDecodeError as error:
        raise SMSCodecError("invalid UTF-16BE user data") from error


def _encode_numeric_address(number: str) -> tuple[int, bytes]:
    number = validate_phone_number(number)
    international = number.startswith("+")
    digits = number[1:] if international else number
    padded = digits + ("F" if len(digits) % 2 else "")
    encoded = bytes(
        int(padded[position + 1] + padded[position], 16)
        for position in range(0, len(padded), 2)
    )
    return 0x91 if international else 0x81, encoded


_SEMI_OCTET_CHARACTERS = "0123456789*#abc"


def _decode_numeric_address(encoded: bytes, digit_count: int, toa: int) -> str:
    if digit_count < 1:
        raise SMSCodecError("numeric address must not be empty")
    if digit_count % 2 and encoded and encoded[-1] >> 4 != 0x0F:
        raise SMSCodecError("odd-length numeric address is missing its filler semi-octet")
    nibbles: list[str] = []
    for value in encoded:
        for nibble in (value & 0x0F, value >> 4):
            if len(nibbles) == digit_count:
                break
            if nibble >= len(_SEMI_OCTET_CHARACTERS):
                raise SMSCodecError("address contains an invalid semi-octet")
            nibbles.append(_SEMI_OCTET_CHARACTERS[nibble])
    if len(nibbles) != digit_count:
        raise SMSCodecError("numeric address is shorter than its declared length")
    result = "".join(nibbles)
    if ((toa >> 4) & 0x07) == 1:
        result = "+" + result
    return result


def _decode_smsc(encoded: bytes) -> str:
    if not encoded:
        raise SMSCodecError("SMSC address is missing its type-of-address octet")
    toa = encoded[0]
    digits: list[str] = []
    address = encoded[1:]
    for position, value in enumerate(address):
        low = value & 0x0F
        high = value >> 4
        if low >= len(_SEMI_OCTET_CHARACTERS):
            raise SMSCodecError("SMSC contains an invalid semi-octet")
        digits.append(_SEMI_OCTET_CHARACTERS[low])
        if high == 0x0F:
            if position != len(address) - 1:
                raise SMSCodecError("SMSC filler semi-octet is not at the end")
        elif high >= len(_SEMI_OCTET_CHARACTERS):
            raise SMSCodecError("SMSC contains an invalid semi-octet")
        else:
            digits.append(_SEMI_OCTET_CHARACTERS[high])
    result = "".join(digits)
    if not result:
        raise SMSCodecError("SMSC address is empty")
    if ((toa >> 4) & 0x07) == 1:
        return "+" + result
    return result


def _smsc_prefix(smsc: str | None) -> bytes:
    if smsc is None:
        return b"\x00"
    toa, encoded = _encode_numeric_address(smsc)
    field = bytes((toa,)) + encoded
    return bytes((len(field),)) + field


def _character_units(text: str, encoding: str) -> list[int]:
    if encoding == "gsm7":
        return [1 if character in _GSM7_BASIC_ENCODE else 2 for character in text]
    units: list[int] = []
    for character in text:
        units.append(len(encode_ucs2(character)) // 2)
    return units


def _split_text(text: str, encoding: str, maximum_units: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for character, units in zip(text, _character_units(text, encoding), strict=True):
        if units > maximum_units:
            raise SMSCodecError("one character exceeds the SMS segment capacity")
        if current and used + units > maximum_units:
            chunks.append("".join(current))
            current = []
            used = 0
        current.append(character)
        used += units
    if current:
        chunks.append("".join(current))
    return chunks


def build_submit_pdus(
    destination: str,
    text: str,
    *,
    encoding: str = "auto",
    reference: int | None = None,
    smsc: str | None = None,
    request_status_report: bool = False,
) -> list[OutgoingPart]:
    """Build SMS-SUBMIT PDUs suitable for ``AT+CMGS=<tpdu_length>``.

    Multipart messages use an 8-bit concatenation UDH.  ``OutgoingPart.pdu`` is
    upper-case hexadecimal text ready to be followed by Ctrl-Z.
    """

    destination = validate_phone_number(destination)
    if not isinstance(text, str) or not text:
        raise SMSCodecError("SMS text must not be empty")
    if encoding not in {"auto", "gsm7", "ucs2"}:
        raise SMSCodecError("encoding must be auto, gsm7, or ucs2")
    if reference is not None and (
        isinstance(reference, bool) or not isinstance(reference, int) or not 0 <= reference <= 255
    ):
        raise SMSCodecError("8-bit concatenation reference must be between 0 and 255")
    if not isinstance(request_status_report, bool):
        raise SMSCodecError("request_status_report must be a boolean")

    if encoding == "auto":
        selected_encoding = "gsm7" if gsm7_can_encode(text) else "ucs2"
    else:
        selected_encoding = encoding
    if selected_encoding == "gsm7" and not gsm7_can_encode(text):
        raise SMSCodecError("text is not representable in GSM 7-bit")
    if selected_encoding == "ucs2":
        encode_ucs2(text)

    single_limit = 160 if selected_encoding == "gsm7" else 70
    multipart_limit = 153 if selected_encoding == "gsm7" else 67
    total_units = sum(_character_units(text, selected_encoding))
    chunks = [text] if total_units <= single_limit else _split_text(
        text, selected_encoding, multipart_limit
    )
    if len(chunks) > 255:
        raise SMSCodecError("an 8-bit concatenated SMS cannot exceed 255 parts")
    if len(chunks) > 1 and reference is None:
        reference = secrets.randbelow(256)

    toa, destination_bytes = _encode_numeric_address(destination)
    destination_digits = len(destination.removeprefix("+"))
    prefix = _smsc_prefix(smsc)
    parts: list[OutgoingPart] = []
    total_parts = len(chunks)
    for part_number, chunk in enumerate(chunks, start=1):
        concat = None
        user_header = b""
        if total_parts > 1:
            concat = ConcatInfo(
                reference=reference,
                total_parts=total_parts,
                part_number=part_number,
                reference_bits=8,
            )
            user_header = bytes((5, 0, 3, reference, total_parts, part_number))

        first_octet = 0x41 if user_header else 0x01
        if request_status_report:
            first_octet |= 0x20
        tpdu = bytearray((first_octet, 0x00, destination_digits, toa))
        tpdu.extend(destination_bytes)
        tpdu.extend((0x00, 0x00 if selected_encoding == "gsm7" else 0x08))
        if selected_encoding == "gsm7":
            septets = gsm7_encode(chunk)
            if user_header:
                header_septets = (len(user_header) * 8 + 6) // 7
                user_data = _pack_septets(
                    septets,
                    prefix=user_header,
                    bit_offset=header_septets * 7,
                )
                tpdu.append(header_septets + len(septets))
            else:
                user_data = _pack_septets(septets)
                tpdu.append(len(septets))
        else:
            user_data = user_header + encode_ucs2(chunk)
            tpdu.append(len(user_data))
        tpdu.extend(user_data)

        full_pdu = prefix + tpdu
        parts.append(
            OutgoingPart(
                pdu=full_pdu.hex().upper(),
                tpdu_length=len(tpdu),
                text=chunk,
                encoding=selected_encoding,
                part_number=part_number,
                total_parts=total_parts,
                concat=concat,
            )
        )
    return parts


class _Cursor:
    def __init__(self, data: bytes, position: int = 0) -> None:
        self.data = data
        self.position = position

    def take(self, length: int, label: str) -> bytes:
        if length < 0 or self.position + length > len(self.data):
            raise SMSCodecError(f"truncated {label}")
        result = self.data[self.position : self.position + length]
        self.position += length
        return result

    def byte(self, label: str) -> int:
        return self.take(1, label)[0]


def _parse_hex(pdu: str) -> bytes:
    if not isinstance(pdu, str):
        raise SMSCodecError("PDU must be hexadecimal text")
    compact = "".join(pdu.split())
    if not compact or len(compact) % 2 or not _HEX_RE.fullmatch(compact):
        raise SMSCodecError("PDU must contain an even number of hexadecimal digits")
    return bytes.fromhex(compact)


def _decode_address(cursor: _Cursor, address_length: int, toa: int) -> str:
    ton = (toa >> 4) & 0x07
    address_octets = (address_length + 1) // 2
    encoded = cursor.take(address_octets, "originating/destination address")
    if ton == 5:
        septet_count = (address_length * 4) // 7
        return gsm7_unpack(encoded, septet_count)
    return _decode_numeric_address(encoded, address_length, toa)


def _swapped_decimal(value: int, label: str) -> int:
    tens = value & 0x0F
    ones = value >> 4
    # TS 23.040 says a non-integer timestamp semi-octet is interpreted as 0.
    if tens > 9:
        tens = 0
    if ones > 9:
        ones = 0
    return tens * 10 + ones


def _decode_timestamp(value: bytes) -> datetime | None:
    if len(value) != 7:
        raise SMSCodecError("SMS timestamp must contain seven octets")
    year = _swapped_decimal(value[0], "year")
    year += 2000 if year <= 69 else 1900
    month = _swapped_decimal(value[1], "month")
    day = _swapped_decimal(value[2], "day")
    hour = _swapped_decimal(value[3], "hour")
    minute = _swapped_decimal(value[4], "minute")
    second = _swapped_decimal(value[5], "second")

    swapped_timezone = ((value[6] & 0x0F) << 4) | (value[6] >> 4)
    negative = bool(swapped_timezone & 0x80)
    tens = (swapped_timezone >> 4) & 0x07
    ones = swapped_timezone & 0x0F
    if ones > 9:
        ones = 0
    offset = timedelta(minutes=(tens * 10 + ones) * 15)
    if negative:
        offset = -offset
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    except ValueError:
        # Preserve the SMS even if the sender supplied an impossible calendar
        # value; callers can still inspect the canonical PDU.
        return None


def _decode_dcs(dcs: int) -> str:
    if (dcs & 0xF0) == 0xF0:
        if dcs & 0x08:
            return "gsm7"
        return "8bit" if dcs & 0x04 else "gsm7"
    group = dcs & 0xF0
    if group in (0xC0, 0xD0):
        return "gsm7"
    if group == 0xE0:
        return "gsm7" if dcs & 0x04 else "ucs2"
    if (dcs & 0xC0) in (0x00, 0x40):
        alphabet = (dcs >> 2) & 0x03
        if alphabet == 3:
            return "gsm7"
        if dcs & 0x20:
            raise SMSCodecError("compressed SMS user data is not supported")
        if alphabet == 0:
            return "gsm7"
        if alphabet == 1:
            return "8bit"
        if alphabet == 2:
            return "ucs2"
    if 0x80 <= dcs <= 0xBF:
        return "gsm7"
    raise SMSCodecError(f"unsupported SMS data coding scheme: 0x{dcs:02X}")


def _parse_user_header(user_data: bytes) -> tuple[int, ConcatInfo | None]:
    if not user_data:
        raise SMSCodecError("UDHI is set but user data is empty")
    header_octets = user_data[0] + 1
    if header_octets > len(user_data):
        raise SMSCodecError("user-data header exceeds the declared TP-UD length")
    concat: ConcatInfo | None = None
    position = 1
    while position < header_octets:
        if position + 2 > header_octets:
            raise SMSCodecError("truncated user-data information element")
        identifier = user_data[position]
        length = user_data[position + 1]
        position += 2
        if position + length > header_octets:
            raise SMSCodecError("user-data information element exceeds UDHL")
        value = user_data[position : position + length]
        position += length
        if identifier == 0x00:
            if length != 3:
                raise SMSCodecError("8-bit concatenation IE must contain three octets")
            candidate = ConcatInfo(value[0], value[1], value[2], 8)
        elif identifier == 0x08:
            if length != 4:
                raise SMSCodecError("16-bit concatenation IE must contain four octets")
            candidate = ConcatInfo((value[0] << 8) | value[1], value[2], value[3], 16)
        else:
            continue
        if concat is not None:
            raise SMSCodecError("multiple concatenation information elements are ambiguous")
        if candidate.total_parts < 1 or not 1 <= candidate.part_number <= candidate.total_parts:
            raise SMSCodecError("invalid concatenated SMS part numbering")
        concat = candidate
    return header_octets, concat


def _decode_user_data(
    cursor: _Cursor, *, dcs: int, has_header: bool
) -> tuple[str | None, bytes, str, ConcatInfo | None, int, int]:
    encoding = _decode_dcs(dcs)
    user_data_length = cursor.byte("TP-UDL")
    if encoding == "gsm7":
        octet_length = (user_data_length * 7 + 7) // 8
    else:
        octet_length = user_data_length
    if octet_length > 140:
        raise SMSCodecError("TP-UD exceeds the 140-octet SMS limit")
    user_data = cursor.take(octet_length, "TP-UD")

    header_octets = 0
    concat = None
    if has_header:
        header_octets, concat = _parse_user_header(user_data)

    if encoding == "gsm7":
        header_septets = (header_octets * 8 + 6) // 7
        if header_septets > user_data_length:
            raise SMSCodecError("GSM 7-bit header exceeds TP-UDL")
        septet_count = user_data_length - header_septets
        bit_offset = header_septets * 7
        text = gsm7_decode(
            _unpack_septets(user_data, septet_count, bit_offset=bit_offset)
        )
        used_bits = (user_data_length * 7) % 8
        return text, text.encode("utf-8"), encoding, concat, used_bits, header_octets

    payload = user_data[header_octets:]
    if encoding == "ucs2":
        text = decode_ucs2(payload)
        return text, text.encode("utf-8"), encoding, concat, 0, header_octets
    return None, payload, encoding, concat, 0, header_octets


def decode_pdu(pdu: str) -> DecodedSMS:
    """Decode an SMS-DELIVER or SMS-SUBMIT PDU.

    Zero octets after the length-declared TP-UD are accepted as QDC fixed-slot
    padding.  Any non-zero trailing byte is rejected rather than hidden.
    """

    raw = _parse_hex(pdu)
    cursor = _Cursor(raw)
    smsc_length = cursor.byte("SMSC length")
    service_center = None
    if smsc_length:
        service_center = _decode_smsc(cursor.take(smsc_length, "SMSC address"))

    first_octet = cursor.byte("TPDU first octet")
    message_type = first_octet & 0x03
    has_header = bool(first_octet & 0x40)
    sender = None
    destination = None
    timestamp = None

    if message_type == 0:  # SMS-DELIVER
        address_length = cursor.byte("originating-address length")
        toa = cursor.byte("originating-address type")
        sender = _decode_address(cursor, address_length, toa)
        cursor.byte("TP-PID")
        dcs = cursor.byte("TP-DCS")
        timestamp = _decode_timestamp(cursor.take(7, "TP-SCTS"))
    elif message_type == 1:  # SMS-SUBMIT (useful for STO SENT/UNSENT records)
        cursor.byte("TP-MR")
        address_length = cursor.byte("destination-address length")
        toa = cursor.byte("destination-address type")
        destination = _decode_address(cursor, address_length, toa)
        cursor.byte("TP-PID")
        dcs = cursor.byte("TP-DCS")
        validity_format = (first_octet >> 3) & 0x03
        if validity_format == 2:
            cursor.take(1, "relative TP-VP")
        elif validity_format in (1, 3):
            cursor.take(7, "enhanced/absolute TP-VP")
    else:
        raise SMSCodecError(f"unsupported or reserved TPDU message type: {message_type}")

    user_data_offset = cursor.position + 1
    text, data, encoding, concat, used_bits, header_octets = _decode_user_data(
        cursor, dcs=dcs, has_header=has_header
    )
    trailing = raw[cursor.position :]
    if any(trailing):
        raise SMSCodecError("non-zero data follows the length-declared TP-UD")
    canonical_bytes = bytearray(raw[: cursor.position])
    if encoding == "gsm7" and header_octets:
        header_bits = header_octets * 8
        text_bit_offset = ((header_bits + 6) // 7) * 7
        for bit in range(header_bits, text_bit_offset):
            byte_position = user_data_offset + bit // 8
            canonical_bytes[byte_position] &= ~(1 << (bit % 8))
    if used_bits:
        canonical_bytes[-1] &= (1 << used_bits) - 1
    canonical = canonical_bytes.hex().upper()
    return DecodedSMS(
        sender=sender,
        destination=destination,
        text=text,
        data=data,
        encoding=encoding,
        dcs=dcs,
        timestamp=timestamp,
        service_center=service_center,
        concat=concat,
        raw_pdu=canonical,
        complete=concat is None or concat.total_parts == 1,
        parts_received=(concat.part_number,) if concat and concat.part_number else (),
        raw_pdus=(canonical,),
    )


def _csv_fields(header: str, prefix: str) -> list[str]:
    if not header.startswith(prefix):
        raise SMSCodecError(f"expected {prefix} modem response")
    try:
        fields = next(csv.reader([header[len(prefix) :].strip()], skipinitialspace=True))
    except (csv.Error, StopIteration) as error:
        raise SMSCodecError(f"malformed {prefix} modem response") from error
    return [field.strip() for field in fields]


def _response_records(response: str, prefix: str) -> list[tuple[str, list[str]]]:
    if not isinstance(response, str):
        raise SMSCodecError("modem response must be text")
    lines = response.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    records: list[tuple[str, list[str]]] = []
    position = 0
    while position < len(lines):
        line = lines[position].strip()
        if not line:
            position += 1
            continue
        if line.startswith(prefix):
            header = line
            position += 1
            body: list[str] = []
            while position < len(lines) and not lines[position].strip().startswith(prefix):
                body.append(lines[position])
                position += 1
            while body and not body[-1].strip():
                body.pop()
            if body and body[-1].strip() == "OK":
                body.pop()
                while body and not body[-1].strip():
                    body.pop()
            records.append((header, body))
            continue
        if line.startswith(prefix.split(":", 1)[0] + ":"):
            raise SMSCodecError(f"malformed {prefix} modem record")
        position += 1
    return records


def _crop_declared_pdu(pdu: str, tpdu_length: int) -> str:
    if tpdu_length < 1:
        raise SMSCodecError("declared TPDU length must be positive")
    raw = _parse_hex(pdu)
    if not raw:
        raise SMSCodecError("empty PDU")
    expected = 1 + raw[0] + tpdu_length
    if len(raw) < expected:
        raise SMSCodecError("PDU is shorter than the modem's declared TPDU length")
    if any(raw[expected:]):
        raise SMSCodecError("non-zero data follows the modem's declared TPDU length")
    return raw[:expected].hex().upper()


def _status_report_outcome(status: int) -> str:
    if status == 0x00:
        return "delivered"
    if 0x20 <= status <= 0x3F:
        return "pending"
    if 0x40 <= status <= 0x7F:
        return "failed"
    return "unconfirmed"


def decode_status_report_pdu(
    pdu: str, *, tpdu_length: int | None = None
) -> SMSStatusReport:
    """Decode the mandatory fields of a PDU-mode SMS-STATUS-REPORT."""

    if tpdu_length is not None:
        if isinstance(tpdu_length, bool) or not isinstance(tpdu_length, int):
            raise SMSCodecError("declared TPDU length must be an integer")
        pdu = _crop_declared_pdu(pdu, tpdu_length)
    raw = _parse_hex(pdu)
    cursor = _Cursor(raw)
    smsc_length = cursor.byte("SMSC length")
    service_center = None
    if smsc_length:
        service_center = _decode_smsc(cursor.take(smsc_length, "SMSC address"))

    first_octet = cursor.byte("TPDU first octet")
    if first_octet & 0x03 != 0x02:
        raise SMSCodecError("PDU is not an SMS-STATUS-REPORT")
    message_reference = cursor.byte("TP-MR")
    address_length = cursor.byte("recipient-address length")
    toa = cursor.byte("recipient-address type")
    recipient = _decode_address(cursor, address_length, toa)
    service_center_time = _decode_timestamp(cursor.take(7, "TP-SCTS"))
    discharge_time = _decode_timestamp(cursor.take(7, "TP-DT"))
    status = cursor.byte("TP-ST")

    canonical = raw.hex().upper()
    return SMSStatusReport(
        message_reference=message_reference,
        recipient=recipient,
        service_center_time=service_center_time,
        discharge_time=discharge_time,
        status=status,
        outcome=_status_report_outcome(status),
        service_center=service_center,
        raw_pdu=canonical,
    )


def parse_cmgl_pdu(response: str, *, storage: str | None = None) -> list[DecodedSMS]:
    messages: list[DecodedSMS] = []
    errors: list[SMSCodecError] = []
    for header, body in _response_records(response, "+CMGL:"):
        try:
            fields = _csv_fields(header, "+CMGL:")
            if len(fields) < 4:
                raise SMSCodecError("PDU +CMGL header is missing fields")
            try:
                index = int(fields[0])
                declared_length = int(fields[-1])
            except ValueError as error:
                raise SMSCodecError("PDU +CMGL index/length is not an integer") from error
            payload_lines = [line.strip() for line in body if line.strip()]
            if len(payload_lines) != 1:
                raise SMSCodecError("PDU +CMGL record must contain exactly one PDU line")
            pdu = _crop_declared_pdu(payload_lines[0], declared_length)
            messages.append(
                replace(
                    decode_pdu(pdu),
                    index=index,
                    storage=storage,
                    status=fields[1],
                )
            )
        except SMSCodecError as error:
            errors.append(error)
    if errors and not messages:
        raise errors[0]
    return messages


def parse_cmgr_pdu(
    response: str, *, index: int | None = None, storage: str | None = None
) -> DecodedSMS:
    records = _response_records(response, "+CMGR:")
    if len(records) != 1:
        raise SMSCodecError("expected exactly one +CMGR PDU record")
    header, body = records[0]
    fields = _csv_fields(header, "+CMGR:")
    if len(fields) < 3:
        raise SMSCodecError("PDU +CMGR header is missing fields")
    try:
        declared_length = int(fields[-1])
    except ValueError as error:
        raise SMSCodecError("PDU +CMGR length is not an integer") from error
    payload_lines = [line.strip() for line in body if line.strip()]
    if len(payload_lines) != 1:
        raise SMSCodecError("PDU +CMGR record must contain exactly one PDU line")
    message = decode_pdu(_crop_declared_pdu(payload_lines[0], declared_length))
    return replace(message, index=index, storage=storage, status=fields[0])


_TEXT_TIMESTAMP_RE = re.compile(
    r"^(?P<year>\d{2})/(?P<month>\d{2})/(?P<day>\d{2}),"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<sign>[+-])(?P<quarters>\d{2})$"
)


def _parse_text_timestamp(value: str) -> datetime:
    match = _TEXT_TIMESTAMP_RE.fullmatch(value)
    if not match:
        raise SMSCodecError("invalid text-mode SMS timestamp")
    parts = {key: int(match.group(key)) for key in (
        "year", "month", "day", "hour", "minute", "second", "quarters"
    )}
    year = parts["year"] + (2000 if parts["year"] <= 69 else 1900)
    offset = timedelta(minutes=parts["quarters"] * 15)
    if match.group("sign") == "-":
        offset = -offset
    try:
        return datetime(
            year,
            parts["month"],
            parts["day"],
            parts["hour"],
            parts["minute"],
            parts["second"],
            tzinfo=timezone(offset),
        )
    except ValueError as error:
        raise SMSCodecError("invalid text-mode SMS timestamp") from error


def _decode_text_mode_ucs2(value: str, label: str) -> str:
    compact = "".join(value.split())
    if not compact or len(compact) % 2 or not _HEX_RE.fullmatch(compact):
        raise SMSCodecError(f"{label} is not valid UCS2 hexadecimal text")
    return decode_ucs2(bytes.fromhex(compact))


def _decode_text_mode_sender(value: str) -> str:
    # QDC commonly emits ASCII addresses as 00xx UTF-16BE units.  Detect that
    # shape before accepting a digit-only value as an already-decoded number;
    # otherwise encoded short codes such as 003100320033 become dangerous.
    if re.fullmatch(r"(?:00[0-9A-Fa-f]{2})+", value):
        return _decode_text_mode_ucs2(value, "text-mode sender")
    if len(value) % 4 == 0 and _HEX_RE.fullmatch(value) and not _PHONE_RE.fullmatch(value):
        return _decode_text_mode_ucs2(value, "text-mode sender")
    return value


def parse_cmgl_text(
    response: str,
    *,
    storage: str | None = None,
    charset: str = "IRA",
) -> list[DecodedSMS]:
    """Parse text-mode ``+CMGL`` output for explicit compatibility use.

    Text mode has no byte length framing, so an SMS body containing a complete
    ``+CMGL:`` line is inherently indistinguishable from a modem record header.
    Unattended ingestion must use :func:`parse_cmgl_pdu` instead.
    """

    normalized_charset = charset.upper() if isinstance(charset, str) else ""
    if normalized_charset not in {"IRA", "GSM", "UCS2"}:
        raise SMSCodecError("text-mode charset must be IRA, GSM, or UCS2")
    messages: list[DecodedSMS] = []
    for header, body in _response_records(response, "+CMGL:"):
        fields = _csv_fields(header, "+CMGL:")
        if len(fields) < 5:
            raise SMSCodecError("text +CMGL header is missing fields")
        try:
            index = int(fields[0])
        except ValueError as error:
            raise SMSCodecError("text +CMGL index is not an integer") from error
        sender = fields[2] or None
        if normalized_charset == "UCS2" and sender:
            sender = _decode_text_mode_sender(sender)
        timestamp = _parse_text_timestamp(fields[-1])
        text = "\n".join(body)
        if normalized_charset == "UCS2":
            text = _decode_text_mode_ucs2(text, "text-mode body")
        messages.append(
            DecodedSMS(
                sender=sender,
                destination=None,
                text=text,
                data=text.encode("utf-8"),
                encoding="text",
                dcs=-1,
                timestamp=timestamp,
                service_center=None,
                concat=None,
                raw_pdu="",
                index=index,
                storage=storage,
                status=fields[1],
            )
        )
    return messages


def _timestamp_seconds(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def reassemble_messages(
    messages: Iterable[DecodedSMS], *, max_gap: timedelta = timedelta(hours=6)
) -> list[DecodedSMS]:
    """Reassemble multipart messages within one caller-defined scan scope.

    The 8-bit reference is reusable, so one key can own multiple assemblies.  A
    conflicting sequence starts another bounded instance instead of overwriting or
    cross-combining it.  Identical canonical PDUs are duplicates.  The caller must
    not pass unbounded history; ``max_gap`` is an additional temporal guard, not a
    globally unique message identifier.
    """

    if not isinstance(max_gap, timedelta) or max_gap <= timedelta(0):
        raise SMSCodecError("reassembly max_gap must be a positive timedelta")
    maximum_seconds = max_gap.total_seconds()
    standalone: list[tuple[int, DecodedSMS]] = []
    grouped: dict[tuple[object, ...], list[tuple[int, DecodedSMS]]] = {}

    for ordinal, message in enumerate(messages):
        if not isinstance(message, DecodedSMS):
            raise SMSCodecError("reassembly input must contain DecodedSMS values")
        concat = message.concat
        if concat is None or concat.part_number is None:
            standalone.append((ordinal, message))
            continue
        if (
            concat.reference_bits not in (8, 16)
            or concat.total_parts < 1
            or not 1 <= concat.part_number <= concat.total_parts
        ):
            raise SMSCodecError("invalid concatenated SMS metadata")
        key = (
            message.sender,
            message.destination,
            message.service_center,
            concat.reference,
            concat.reference_bits,
            concat.total_parts,
            message.encoding,
            message.dcs,
        )
        grouped.setdefault(key, []).append((ordinal, message))

    assembled_rows: list[tuple[int, DecodedSMS]] = list(standalone)
    for rows in grouped.values():
        seen_pdus: set[str] = set()
        unique_rows: list[tuple[int, DecodedSMS]] = []
        for ordinal, message in rows:
            if message.raw_pdu and message.raw_pdu in seen_pdus:
                continue
            if message.raw_pdu:
                seen_pdus.add(message.raw_pdu)
            unique_rows.append((ordinal, message))

        unique_rows.sort(
            key=lambda row: (
                _timestamp_seconds(row[1].timestamp) is None,
                _timestamp_seconds(row[1].timestamp) or 0.0,
                row[0],
            )
        )
        instances: list[dict[str, object]] = []
        for ordinal, message in unique_rows:
            sequence = message.concat.part_number
            message_time = _timestamp_seconds(message.timestamp)
            candidates: list[tuple[float, int, dict[str, object]]] = []
            for instance_number, instance in enumerate(instances):
                parts = instance["parts"]
                if sequence in parts:
                    continue
                times = instance["times"]
                if message_time is None or not times:
                    distance = 0.0
                else:
                    distance = min(abs(message_time - value) for value in times)
                    if distance > maximum_seconds:
                        continue
                candidates.append((distance, instance_number, instance))
            if candidates:
                instance = min(candidates, key=lambda value: (value[0], value[1]))[2]
            else:
                instance = {"parts": {}, "times": [], "ordinals": []}
                instances.append(instance)
            instance["parts"][sequence] = message
            instance["ordinals"].append(ordinal)
            if message_time is not None:
                instance["times"].append(message_time)

        for instance in instances:
            parts = instance["parts"]
            numbers = tuple(sorted(parts))
            ordered = [parts[number] for number in numbers]
            base = parts.get(1, ordered[0])
            expected = tuple(range(1, base.concat.total_parts + 1))
            complete = numbers == expected
            raw_pdus = tuple(part.raw_pdu for part in ordered)
            if complete and all(part.text is not None for part in ordered):
                text = "".join(part.text for part in ordered)
                data = text.encode("utf-8")
            elif complete:
                text = None
                data = b"".join(part.data for part in ordered)
            else:
                text = None
                data = b""
            assembled_rows.append(
                (
                    min(instance["ordinals"]),
                    replace(
                        base,
                        text=text,
                        data=data,
                        concat=replace(base.concat, part_number=None),
                        complete=complete,
                        parts_received=numbers,
                        raw_pdu=raw_pdus[0],
                        raw_pdus=raw_pdus,
                    ),
                )
            )

    assembled_rows.sort(key=lambda row: row[0])
    return [message for _ordinal, message in assembled_rows]


__all__ = [
    "ConcatInfo",
    "DecodedSMS",
    "OutgoingPart",
    "SMSStatusReport",
    "SMSCodecError",
    "build_submit_pdus",
    "decode_pdu",
    "decode_status_report_pdu",
    "decode_ucs2",
    "encode_ucs2",
    "gsm7_can_encode",
    "gsm7_decode",
    "gsm7_encode",
    "gsm7_pack",
    "gsm7_unpack",
    "parse_cmgl_pdu",
    "parse_cmgl_text",
    "parse_cmgr_pdu",
    "reassemble_messages",
    "validate_phone_number",
]
