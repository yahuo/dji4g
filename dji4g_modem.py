"""SMS operations over the DJI Baiwang AT transport.

This module contains the device-facing SMS workflow.  It deliberately does not
delete messages and keeps the codec and the USB transport independently
testable.
"""

from __future__ import annotations

import csv
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator

from dji4g_at import AtError, AtTimeoutError, AtTransport, response_ok
from dji4g_sms import (
    DecodedSMS,
    OutgoingPart,
    SMSCodecError,
    SMSStatusReport,
    build_submit_pdus,
    decode_status_report_pdu,
    parse_cmgr_pdu,
    parse_cmgl_pdu,
    parse_cmgl_text,
    reassemble_messages,
)


SMS_STORAGES = ("SM", "ME")
MAX_SMS_PARTS = 10


class SmsModemError(RuntimeError):
    pass


class SmsSendError(SmsModemError):
    def __init__(
        self,
        message: str,
        *,
        sent_parts: int,
        total_parts: int,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.sent_parts = sent_parts
        self.total_parts = total_parts
        self.uncertain = uncertain
        self.submitted = uncertain or sent_parts > 0


@dataclass(frozen=True)
class SmsSettings:
    message_format: int | None
    storages: tuple[str, ...]
    charset: str | None
    new_message_indication: str | None


@dataclass(frozen=True)
class SendReceipt:
    segment_count: int
    message_references: tuple[int | None, ...]
    status: str = "accepted_by_modem"


def parse_cmgf(response: str) -> int | None:
    match = re.search(r"\+CMGF:\s*([01])", response)
    return int(match.group(1)) if match else None


def parse_cpms_storages(response: str) -> tuple[str, ...]:
    match = re.search(r"\+CPMS:\s*(.+?)(?:\r?\n|$)", response)
    if not match:
        return ()
    return tuple(re.findall(r'"([A-Za-z0-9]{1,8})"', match.group(1))[:3])


def parse_cpms_usage(
    response: str, *, selected_storage: str | None = None
) -> dict[str, dict[str, int]]:
    match = re.search(r"\+CPMS:\s*(.+?)(?:\r?\n|$)", response)
    if not match:
        return {}
    usage: dict[str, dict[str, int]] = {}
    for storage, used, total in re.findall(
        r'"([A-Za-z0-9]{1,8})"\s*,\s*(\d+)\s*,\s*(\d+)', match.group(1)
    ):
        used_value = int(used)
        total_value = int(total)
        if total_value > 0 and used_value <= total_value:
            usage[storage.upper()] = {"used": used_value, "total": total_value}
    if usage or selected_storage is None:
        return usage
    storage = selected_storage.upper()
    values = [value.strip() for value in match.group(1).split(",")]
    if storage not in SMS_STORAGES or len(values) < 2 or any(
        re.fullmatch(r"\d+", value) is None for value in values
    ):
        return {}
    used_value, total_value = int(values[0]), int(values[1])
    if total_value > 0 and used_value <= total_value:
        usage[storage] = {"used": used_value, "total": total_value}
    return usage


def parse_registration_status(response: str) -> str:
    matches = re.findall(r"\+CEREG:\s*([^\r\n]+)", response)
    if not matches:
        return "unknown"
    candidates: list[str] = []
    for value in reversed(matches):
        fields = value.split(",")
        if len(fields) >= 2 and re.fullmatch(r"\s*[0-5]\s*", fields[1]):
            candidates.append(fields[1].strip())
    if not candidates:
        candidates.append(matches[-1].strip().strip('"'))
    status = None
    for candidate in candidates:
        try:
            status = int(candidate)
            break
        except ValueError:
            continue
    if status is None:
        return "unknown"
    return {
        0: "not_registered",
        1: "home",
        2: "searching",
        3: "denied",
        4: "unknown",
        5: "roaming",
    }.get(status, "unknown")


def parse_csq(response: str) -> int | None:
    match = re.search(r"\+CSQ:\s*(\d+)\s*,", response)
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 31 else None


def parse_qnwinfo(response: str) -> dict[str, object]:
    if re.search(r"\+QNWINFO:\s*No Service(?:\r?\n|$)", response, re.IGNORECASE):
        return {"parse_status": "no_service", "service": "no_service"}
    match = re.search(
        r'\+QNWINFO:\s*"([^"]+)"\s*,\s*"[^"]*"\s*,\s*"([^"]*)"\s*,\s*(\d+)',
        response,
    )
    if not match:
        return {"parse_status": "parse_error"}
    access = match.group(1).strip().upper()
    band_text = match.group(2).strip().upper()
    channel = int(match.group(3))
    pieces = access.split()
    duplex = pieces[0] if pieces and pieces[0] in {"FDD", "TDD"} else None
    rat = " ".join(pieces[1:]) if duplex is not None else access
    if rat not in {
        "LTE",
        "WCDMA",
        "GSM",
        "GPRS",
        "EDGE",
        "HSDPA",
        "HSUPA",
        "HSPA+",
        "TDSCDMA",
        "CAT-M1",
        "CAT-NB1",
        "NR5G-SA",
        "NR5G-NSA",
    }:
        return {"parse_status": "parse_error"}
    band_match = re.search(r"\bBAND\s+(\d+)\b", band_text)
    result: dict[str, object] = {
        "parse_status": "ok",
        "service": "registered",
        "rat": rat,
        "channel": channel,
    }
    if duplex is not None:
        result["duplex"] = duplex
    if band_match:
        result["band"] = int(band_match.group(1))
    return result


def parse_qeng_servingcell(response: str) -> dict[str, object]:
    match = re.search(r"^\+QENG:\s*(.+)$", response, re.MULTILINE)
    if not match:
        return {"parse_status": "parse_error"}
    try:
        fields = next(csv.reader([match.group(1).strip()]))
    except (csv.Error, StopIteration):
        return {"parse_status": "parse_error"}
    fields = [field.strip() for field in fields]
    if (
        len(fields) < 18
        or fields[0].lower() != "servingcell"
        or fields[2].upper() != "LTE"
        or fields[3].upper() not in {"FDD", "TDD"}
    ):
        return {"parse_status": "parse_error"}
    try:
        channel = int(fields[8])
        band = int(fields[9])
    except ValueError:
        return {"parse_status": "parse_error"}
    if channel < 0 or band <= 0:
        return {"parse_status": "parse_error"}
    raw_state = fields[1].upper()
    state = {
        "NOCONN": "idle",
        "CONNECT": "connected",
        "SEARCH": "searching",
        "LIMSRV": "limited_service",
    }.get(raw_state, "unknown")
    service = "registered" if raw_state in {"NOCONN", "CONNECT"} else "no_service"
    result: dict[str, object] = {
        "parse_status": "ok",
        "service": service,
        "state": state,
        "rat": "LTE",
        "duplex": fields[3].upper(),
        "channel": channel,
        "band": band,
    }
    for key, index, minimum, maximum in (
        ("rsrp_dbm", 13, -200, 0),
        ("rsrq_db", 14, -50, 20),
        ("rssi_dbm", 15, -200, 0),
        ("sinr_db", 16, -50, 100),
    ):
        try:
            metric = int(fields[index])
        except ValueError:
            continue
        if minimum <= metric <= maximum:
            result[key] = metric
    return result


def parse_cscs(response: str) -> str | None:
    match = re.search(r'\+CSCS:\s*"([A-Za-z0-9_-]+)"', response)
    return match.group(1) if match else None


def parse_cnmi(response: str) -> str | None:
    match = re.search(r"\+CNMI:\s*([0-9, ]+)", response)
    return re.sub(r"\s+", "", match.group(1)) if match else None


def parse_cmti(response: str) -> list[tuple[str, int]]:
    notifications: list[tuple[str, int]] = []
    for storage, index in re.findall(r'\+CMTI:\s*"([A-Za-z0-9]+)"\s*,\s*(\d+)', response):
        notifications.append((storage.upper(), int(index)))
    return notifications


def _pdu_reaches_declared_length(pdu: str, declared_length: int) -> bool:
    if len(pdu) < 2 or len(pdu) % 2 or re.fullmatch(r"[0-9A-Fa-f]+", pdu) is None:
        return False
    raw_pdu = bytes.fromhex(pdu)
    return len(raw_pdu) >= 1 + raw_pdu[0] + declared_length


def _cpms_command(storages: Iterable[str]) -> str | None:
    values = tuple(storage.upper() for storage in storages)
    if not values:
        return None
    if len(values) == 1:
        values = values * 3
    values = values[:3]
    if any(not re.fullmatch(r"[A-Z0-9]{1,8}", value) for value in values):
        raise SmsModemError("Invalid SMS storage name")
    return "AT+CPMS=" + ",".join(f'"{value}"' for value in values)


class SmsModem:
    """Own one AT transport and perform non-overlapping SMS operations."""

    def __init__(self, transport: AtTransport) -> None:
        self.transport = transport
        self._notification_backlog: list[tuple[str, int]] = []
        self._delivery_backlog: list[SMSStatusReport] = []
        self._delivery_fragment: tuple[str, str | None] | None = None
        self._storage_usage: dict[str, dict[str, int]] = {}

    @classmethod
    def open(cls) -> "SmsModem":
        return cls(AtTransport())

    def __enter__(self) -> "SmsModem":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    def _checked(self, command: str, timeout: float = 3.0) -> str:
        response = self.transport.send(command, timeout=timeout)
        if not response_ok(response):
            raise SmsModemError(f"The modem rejected {command.split('=')[0]}")
        return response

    def capture_settings(self) -> SmsSettings:
        message_format = self.transport.send("AT+CMGF?")
        cpms = self.transport.send("AT+CPMS?")
        self._storage_usage.update(parse_cpms_usage(cpms))
        return SmsSettings(
            message_format=parse_cmgf(message_format),
            storages=parse_cpms_storages(cpms),
            charset=parse_cscs(self.transport.send("AT+CSCS?")),
            new_message_indication=parse_cnmi(self.transport.send("AT+CNMI?")),
        )

    def restore_settings(self, settings: SmsSettings) -> None:
        errors: list[str] = []
        commands: list[str] = []
        cpms = _cpms_command(settings.storages)
        if cpms:
            commands.append(cpms)
        if settings.charset:
            commands.append(f'AT+CSCS="{settings.charset}"')
        if settings.message_format is not None:
            commands.append(f"AT+CMGF={settings.message_format}")
        if settings.new_message_indication:
            commands.append(f"AT+CNMI={settings.new_message_indication}")
        for command in commands:
            try:
                self._checked(command, timeout=5.0)
            except (AtError, SmsModemError) as error:
                errors.append(str(error))
        if errors:
            raise SmsModemError("Could not restore SMS settings: " + "; ".join(errors))

    @contextmanager
    def temporary_settings(self) -> Iterator[SmsSettings]:
        before = self.capture_settings()
        operation_error: BaseException | None = None
        try:
            yield before
        except BaseException as error:
            operation_error = error
            raise
        finally:
            try:
                self.restore_settings(before)
            except SmsModemError:
                if operation_error is None:
                    raise

    def list_messages(
        self,
        storages: Iterable[str] | str = SMS_STORAGES,
        *,
        mode: str = "pdu",
    ) -> list[DecodedSMS]:
        mode = mode.lower()
        if mode not in {"pdu", "text"}:
            raise SmsModemError("SMS list mode must be pdu or text")
        if isinstance(storages, str):
            storages = (storages,)
        requested = tuple(dict.fromkeys(storage.upper() for storage in storages))
        if not requested:
            raise SmsModemError("At least one SMS storage is required")
        messages: list[DecodedSMS] = []
        failures: list[str] = []
        with self.temporary_settings():
            if mode == "pdu":
                self._checked("AT+CMGF=0")
            else:
                self._checked("AT+CMGF=1")
                self._checked('AT+CSCS="UCS2"')
            for storage in requested:
                try:
                    cpms = self._checked(_cpms_command((storage,)) or "", timeout=5.0)
                    self._storage_usage.update(
                        parse_cpms_usage(cpms, selected_storage=storage)
                    )
                    command = "AT+CMGL=4" if mode == "pdu" else 'AT+CMGL="ALL"'
                    response = self.transport.send(command, timeout=15.0)
                    if not response_ok(response):
                        raise SmsModemError(f"The modem could not list {storage} messages")
                    if mode == "pdu":
                        messages.extend(parse_cmgl_pdu(response, storage=storage))
                    else:
                        messages.extend(
                            parse_cmgl_text(response, storage=storage, charset="UCS2")
                        )
                except (AtError, SmsModemError, ValueError) as error:
                    failures.append(f"{storage}: {error}")
        if failures and not messages and len(failures) == len(requested):
            raise SmsModemError("Could not list SMS storage: " + "; ".join(failures))
        seen: set[str] = set()
        unique: list[DecodedSMS] = []
        for message in messages:
            if message.raw_pdu:
                fingerprint = "pdu:" + message.raw_pdu.upper()
            else:
                fingerprint = "text:" + "\x00".join(
                    str(value)
                    for value in (
                        message.storage,
                        message.index,
                        message.sender,
                        message.timestamp,
                        message.text,
                    )
                )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append(message)
        return unique

    def list_complete_messages(
        self,
        storages: Iterable[str] | str = SMS_STORAGES,
        *,
        mode: str = "pdu",
    ) -> list[DecodedSMS]:
        """Return the direct-CLI view with multipart messages reassembled."""

        return reassemble_messages(self.list_messages(storages, mode=mode))

    def read_message(self, storage: str, index: int) -> DecodedSMS:
        if index < 0:
            raise SmsModemError("SMS index must be non-negative")
        with self.temporary_settings():
            self._checked("AT+CMGF=0")
            self._checked(_cpms_command((storage.upper(),)) or "", timeout=5.0)
            response = self.transport.send(f"AT+CMGR={index}", timeout=10.0)
            if not response_ok(response):
                raise SmsModemError("The modem could not read the requested SMS")
            return parse_cmgr_pdu(response, storage=storage.upper(), index=index)

    def preview(
        self,
        destination: str,
        text: str,
        encoding: str = "auto",
        *,
        request_status_report: bool = False,
    ) -> list[OutgoingPart]:
        parts = build_submit_pdus(
            destination,
            text,
            encoding=encoding,
            request_status_report=request_status_report,
        )
        if len(parts) > MAX_SMS_PARTS:
            raise SmsModemError(f"SMS would use {len(parts)} segments; maximum is {MAX_SMS_PARTS}")
        return parts

    def send_sms(
        self,
        destination: str,
        text: str,
        *,
        encoding: str = "auto",
        mode: str = "auto",
        request_status_report: bool = False,
    ) -> SendReceipt:
        mode = mode.lower()
        if mode not in {"auto", "pdu", "text"}:
            raise SmsModemError("SMS mode must be auto, pdu or text")
        parts = self.preview(
            destination,
            text,
            encoding,
            request_status_report=request_status_report,
        )
        if mode == "text":
            return self._send_text(destination, text, parts)
        return self._send_pdu(parts)

    def _send_pdu(self, parts: list[OutgoingPart]) -> SendReceipt:
        references: list[int | None] = []
        try:
            with self.temporary_settings():
                self._checked("AT+CMGF=0")
                for offset, part in enumerate(parts):
                    try:
                        response = self.transport.send_with_prompt(
                            f"AT+CMGS={part.tpdu_length}",
                            part.pdu.encode("ascii") + b"\x1a",
                            timeout=45.0,
                        )
                    except AtTimeoutError as error:
                        uncertain = bool(
                            getattr(error, "follow_up_sent", False)
                            or getattr(error, "submitted", False)
                        )
                        raise SmsSendError(
                            f"SMS segment {offset + 1}/{len(parts)} timed out",
                            sent_parts=offset,
                            total_parts=len(parts),
                            uncertain=uncertain,
                        ) from error
                    except AtError as error:
                        raise SmsSendError(
                            f"SMS segment {offset + 1}/{len(parts)} failed",
                            sent_parts=offset,
                            total_parts=len(parts),
                        ) from error
                    if not response_ok(response) or "+CMGS:" not in response:
                        raise SmsSendError(
                            f"The modem rejected SMS segment {offset + 1}/{len(parts)}",
                            sent_parts=offset,
                            total_parts=len(parts),
                        )
                    match = re.search(r"\+CMGS:\s*(\d+)", response)
                    references.append(int(match.group(1)) if match else None)
                    if offset + 1 < len(parts):
                        time.sleep(0.5)
        except SmsSendError:
            raise
        except SmsModemError as error:
            if references:
                raise SmsSendError(
                    "SMS was accepted but modem settings could not be restored",
                    sent_parts=len(references),
                    total_parts=len(parts),
                    uncertain=True,
                ) from error
            raise
        return SendReceipt(len(parts), tuple(references))

    def _send_text(
        self,
        destination: str,
        text: str,
        parts: list[OutgoingPart],
    ) -> SendReceipt:
        if len(parts) != 1:
            raise SmsModemError("Text mode is limited to one segment; use PDU mode")
        selected_encoding = parts[0].encoding.lower()
        if selected_encoding not in {"gsm7", "ucs2"}:
            raise SmsModemError("Text mode supports GSM7 or UCS2 only")
        if selected_encoding == "gsm7" and (not destination.isascii() or not text.isascii()):
            raise SmsModemError("Non-ASCII text mode is not portable; use PDU mode")

        submitted = False
        reference: int | None = None
        try:
            with self.temporary_settings():
                self._checked("AT+CMGF=1")
                if selected_encoding == "ucs2":
                    self._checked('AT+CSCS="UCS2"')
                    encoded_destination = destination.encode("utf-16-be").hex().upper()
                    follow_up = text.encode("utf-16-be").hex().upper().encode("ascii") + b"\x1a"
                else:
                    self._checked('AT+CSCS="GSM"')
                    encoded_destination = destination
                    follow_up = text.encode("ascii") + b"\x1a"
                try:
                    response = self.transport.send_with_prompt(
                        f'AT+CMGS="{encoded_destination}"',
                        follow_up,
                        timeout=45.0,
                    )
                except AtTimeoutError as error:
                    raise SmsSendError(
                        "Text SMS timed out",
                        sent_parts=0,
                        total_parts=1,
                        uncertain=bool(
                            getattr(error, "follow_up_sent", False)
                            or getattr(error, "submitted", False)
                        ),
                    ) from error
                except AtError as error:
                    raise SmsSendError(
                        "Text SMS failed", sent_parts=0, total_parts=1
                    ) from error
                if not response_ok(response) or "+CMGS:" not in response:
                    raise SmsSendError(
                        "The modem rejected the text SMS", sent_parts=0, total_parts=1
                    )
                match = re.search(r"\+CMGS:\s*(\d+)", response)
                reference = int(match.group(1)) if match else None
                submitted = True
        except SmsSendError:
            raise
        except SmsModemError as error:
            if submitted:
                raise SmsSendError(
                    "SMS was accepted but modem settings could not be restored",
                    sent_parts=1,
                    total_parts=1,
                    uncertain=True,
                ) from error
            raise
        return SendReceipt(1, (reference,))

    def configure_delivery_reports(self) -> None:
        self._checked("AT+CMGF=0")
        current = parse_cnmi(self._checked("AT+CNMI?"))
        fields = current.split(",") if current else []
        if len(fields) != 5 or any(not field.isdigit() for field in fields):
            raise SmsModemError("The modem returned an invalid CNMI setting")
        fields[3] = "1"
        self._checked("AT+CNMI=" + ",".join(fields))

    def _consume_unsolicited(self, timeout: float) -> None:
        response = self.transport.read_unsolicited(timeout=timeout)
        if not response:
            return
        normalized = response.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return
        lines = normalized.split("\n")
        if self._delivery_fragment is not None:
            header, partial_pdu = self._delivery_fragment
            self._delivery_fragment = None
            if partial_pdu is None and lines:
                lines[0] = header + lines[0]
            elif partial_pdu == "" and lines:
                joined_header = header + lines[0]
                joined_match = re.fullmatch(r"\+CDS:\s*([0-9]+)\s*", joined_header)
                if (
                    joined_match is not None
                    and len(lines) > 1
                    and _pdu_reaches_declared_length(
                        lines[1].strip(), int(joined_match.group(1))
                    )
                ):
                    lines[0] = joined_header
                else:
                    lines.insert(0, header)
            else:
                if partial_pdu and lines:
                    lines[0] = partial_pdu + lines[0]
                lines.insert(0, header)
        self._notification_backlog.extend(parse_cmti("\n".join(lines)))
        position = 0
        while position < len(lines):
            line = lines[position].strip()
            match = re.fullmatch(r"\+CDS:\s*([0-9]+)\s*", line)
            if match is None:
                if position + 1 == len(lines) and (
                    "+CDS:".startswith(line)
                    or (
                        line.startswith("+CDS:")
                        and re.fullmatch(r"[ \t]*", line[len("+CDS:") :]) is not None
                    )
                ):
                    self._delivery_fragment = (line, None)
                    break
                position += 1
                continue
            if position + 1 >= len(lines):
                self._delivery_fragment = (line, "")
                break
            pdu = lines[position + 1].strip()
            if not re.fullmatch(r"[0-9A-Fa-f]+", pdu):
                position += 1
                continue
            declared_length = int(match.group(1))
            if len(pdu) < 2 or len(pdu) % 2:
                if position + 2 == len(lines):
                    self._delivery_fragment = (line, pdu)
                    break
                position += 2
                continue
            if not _pdu_reaches_declared_length(pdu, declared_length):
                if position + 2 == len(lines):
                    self._delivery_fragment = (line, pdu)
                    break
                position += 2
                continue
            try:
                self._delivery_backlog.append(
                    decode_status_report_pdu(pdu, tpdu_length=declared_length)
                )
            except SMSCodecError:
                pass
            position += 2

    def read_notifications(self, timeout: float = 0.25) -> list[tuple[str, int]]:
        self._consume_unsolicited(timeout)
        notifications = self._notification_backlog
        self._notification_backlog = []
        return notifications

    def read_delivery_reports(self, timeout: float = 0.25) -> list[SMSStatusReport]:
        self._consume_unsolicited(timeout)
        reports = self._delivery_backlog
        self._delivery_backlog = []
        return reports

    def health(self) -> dict[str, object]:
        ping = self.transport.send("AT", timeout=2.0)
        sim = self.transport.send("AT+CPIN?", timeout=2.0)
        registration = self.transport.send("AT+CEREG?", timeout=2.0)
        optional: dict[str, str] = {}
        for key, command in (
            ("signal", "AT+CSQ"),
            ("network", "AT+QNWINFO"),
            ("engineering", 'AT+QENG="servingcell"'),
        ):
            try:
                optional[key] = self.transport.send(command, timeout=2.0)
            except (AtError, AtTimeoutError):
                optional[key] = ""
        at_ready = response_ok(ping)
        sim_ready = "+CPIN: READY" in sim
        registration_state = parse_registration_status(registration)
        network = parse_qnwinfo(optional["network"])
        engineering = parse_qeng_servingcell(optional["engineering"])
        if engineering.get("parse_status") == "ok":
            radio = dict(engineering)
            for key in ("rat", "duplex", "band", "channel"):
                if key not in radio and key in network:
                    radio[key] = network[key]
        elif network.get("parse_status") in {"ok", "no_service"}:
            radio = network
        else:
            radio = {"parse_status": "unavailable"}
        result: dict[str, object] = {
            "connected": at_ready,
            "ready": at_ready and sim_ready,
            "at_ready": at_ready,
            "sim_ready": sim_ready,
            "registered": registration_state in {"home", "roaming"},
            "registration": registration_state,
            "signal": parse_csq(optional["signal"]),
            "radio": radio,
        }
        if self._storage_usage:
            result["storage"] = {
                storage: dict(usage)
                for storage, usage in self._storage_usage.items()
                if storage in SMS_STORAGES
            }
        return result

    def probe(self) -> dict[str, object]:
        return self.health()

    def send_message(self, recipient: str, body: str) -> SendReceipt:
        return self.send_sms(recipient, body, request_status_report=True)
