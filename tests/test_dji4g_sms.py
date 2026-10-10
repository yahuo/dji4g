import dataclasses
import unittest
from datetime import timedelta

import dji4g_sms as sms


STATUS_REPORT_PDU = "00022A0D91683108000000F0627042329204236270423292542300"


def _semi_octets(number: str) -> bytes:
    digits = number.removeprefix("+")
    if len(digits) % 2:
        digits += "F"
    return bytes(int(digits[i + 1] + digits[i], 16) for i in range(0, len(digits), 2))


def _pack_at_bit_offset(septets: list[int], prefix: bytes, offset: int) -> bytes:
    result = bytearray(prefix)
    total_bits = offset + len(septets) * 7
    result.extend(b"\x00" * ((total_bits + 7) // 8 - len(result)))
    cursor = offset
    for septet in septets:
        for bit in range(7):
            if septet & (1 << bit):
                result[cursor // 8] |= 1 << (cursor % 8)
            cursor += 1
    return bytes(result)


def _deliver_pdu(
    sender: str,
    text: str,
    *,
    concat: tuple[int, int, int] | None = None,
    concat_16bit: bool = False,
    ucs2: bool = False,
) -> str:
    first_octet = 0x40 if concat else 0x00
    sender_bytes = _semi_octets(sender)
    toa = 0x91 if sender.startswith("+") else 0x81
    tpdu = bytearray((first_octet, len(sender.removeprefix("+")), toa))
    tpdu.extend(sender_bytes)
    tpdu.extend((0x00, 0x08 if ucs2 else 0x00))
    # 2026-07-24 12:34:56 +08:00, semi-octet swapped.
    tpdu.extend(bytes.fromhex("62704221436523"))

    header = b""
    if concat:
        reference, total, sequence = concat
        if concat_16bit:
            header = bytes((6, 8, 4, reference >> 8, reference & 0xFF, total, sequence))
        else:
            header = bytes((5, 0, 3, reference, total, sequence))

    if ucs2:
        user_data = header + text.encode("utf-16-be")
        tpdu.append(len(user_data))
        tpdu.extend(user_data)
    else:
        septets = sms.gsm7_encode(text)
        if header:
            header_septets = (len(header) * 8 + 6) // 7
            user_data = _pack_at_bit_offset(septets, header, header_septets * 7)
            tpdu.append(header_septets + len(septets))
        else:
            user_data = sms.gsm7_pack(text)
            tpdu.append(len(septets))
        tpdu.extend(user_data)
    return (b"\x00" + tpdu).hex().upper()


class ValidationTests(unittest.TestCase):
    def test_phone_number_accepts_international_and_short_codes(self):
        self.assertEqual(sms.validate_phone_number("+13800138000"), "+13800138000")
        self.assertEqual(sms.validate_phone_number("10086"), "10086")
        self.assertEqual(sms.validate_phone_number("123"), "123")

    def test_phone_number_rejects_ambiguous_or_out_of_range_values(self):
        for value in ("", "+", "12", "+12", "123 456", "1-800", "１２３", "abc", "++123"):
            with self.subTest(value=value), self.assertRaises(sms.SMSCodecError):
                sms.validate_phone_number(value)
        with self.assertRaises(sms.SMSCodecError):
            sms.validate_phone_number("1" * 16)

    def test_empty_message_and_invalid_encoding_are_rejected(self):
        with self.assertRaises(sms.SMSCodecError):
            sms.build_submit_pdus("10086", "")
        with self.assertRaises(sms.SMSCodecError):
            sms.build_submit_pdus("10086", "hello", encoding="latin1")
        with self.assertRaises(sms.SMSCodecError):
            sms.build_submit_pdus("10086", "中文", encoding="gsm7")


class GSM7Tests(unittest.TestCase):
    def test_known_packing_and_nonzero_spare_bits(self):
        self.assertEqual(sms.gsm7_pack("hello").hex().upper(), "E8329BFD06")
        padded = bytes.fromhex("E8329BFDFE")
        self.assertEqual(sms.gsm7_unpack(padded, 5), "hello")

    def test_full_extension_set_round_trips(self):
        value = "A^{}\\[~]|€Z"
        self.assertTrue(sms.gsm7_can_encode(value))
        septets = sms.gsm7_encode(value)
        self.assertGreater(len(septets), len(value))
        self.assertEqual(sms.gsm7_unpack(sms.gsm7_pack(value), len(septets)), value)

    def test_unencodable_and_malformed_escape_are_rejected(self):
        self.assertFalse(sms.gsm7_can_encode("中文"))
        with self.assertRaises(sms.SMSCodecError):
            sms.gsm7_encode("中文")
        with self.assertRaises(sms.SMSCodecError):
            sms.gsm7_decode([0x1B])
        with self.assertRaises(sms.SMSCodecError):
            sms.gsm7_unpack(b"\x00", 2)
        for extension in (-1, 0x80, None):
            with self.subTest(extension=extension), self.assertRaises(sms.SMSCodecError):
                sms.gsm7_decode([0x1B, extension])

    def test_reserved_extension_codes_follow_the_default_alphabet_fallback(self):
        self.assertEqual(sms.gsm7_decode([0x1B, 0x00]), "@")
        self.assertEqual(sms.gsm7_decode([0x1B, 0x1B]), " ")


class UCS2Tests(unittest.TestCase):
    def test_utf16be_round_trip_including_non_bmp(self):
        value = "你好😀"
        self.assertEqual(sms.encode_ucs2("你好").hex().upper(), "4F60597D")
        self.assertEqual(sms.decode_ucs2(sms.encode_ucs2(value)), value)

    def test_malformed_utf16_is_rejected(self):
        for value in (b"\x00", b"\xD8\x00"):
            with self.subTest(value=value), self.assertRaises(sms.SMSCodecError):
                sms.decode_ucs2(value)


class SubmitPduTests(unittest.TestCase):
    def test_status_report_request_changes_only_the_submit_first_octet(self):
        ordinary = sms.build_submit_pdus("10086", "hello")[0]
        requested = sms.build_submit_pdus(
            "10086", "hello", request_status_report=True
        )[0]
        self.assertEqual(bytes.fromhex(ordinary.pdu)[1], 0x01)
        self.assertEqual(bytes.fromhex(requested.pdu)[1], 0x21)
        self.assertEqual(ordinary.pdu[4:], requested.pdu[4:])

        ordinary_parts = sms.build_submit_pdus(
            "10086", "A" * 161, reference=0x42
        )
        requested_parts = sms.build_submit_pdus(
            "10086", "A" * 161, reference=0x42, request_status_report=True
        )
        self.assertTrue(all(bytes.fromhex(part.pdu)[1] == 0x41 for part in ordinary_parts))
        self.assertTrue(all(bytes.fromhex(part.pdu)[1] == 0x61 for part in requested_parts))

    def test_single_gsm_submit_has_smsc_prefix_and_cmgs_length(self):
        part = sms.build_submit_pdus("+13800138000", "hello")[0]
        raw = bytes.fromhex(part.pdu)
        self.assertEqual(raw[0], 0)
        self.assertEqual(part.tpdu_length, len(raw) - 1)
        self.assertEqual(part.encoding, "gsm7")
        decoded = sms.decode_pdu(part.pdu)
        self.assertEqual(decoded.destination, "+13800138000")
        self.assertEqual(decoded.text, "hello")

    def test_submit_vectors_are_spec_reproducible(self):
        gsm = sms.build_submit_pdus("10086", "hello")[0]
        self.assertEqual(gsm.pdu, "00010005810180F6000005E8329BFD06")
        self.assertEqual(gsm.tpdu_length, 15)
        ucs2 = sms.build_submit_pdus("10086", "中")[0]
        self.assertEqual(ucs2.pdu, "00010005810180F60008024E2D")
        self.assertEqual(ucs2.tpdu_length, 12)

    def test_short_number_uses_unknown_ton(self):
        part = sms.build_submit_pdus("10086", "balance")[0]
        raw = bytes.fromhex(part.pdu)
        # SMSC length, first octet, MR, address length, then TOA.
        self.assertEqual(raw[4], 0x81)
        self.assertEqual(sms.decode_pdu(part.pdu).destination, "10086")

    def test_explicit_smsc_is_included_but_excluded_from_cmgs_length(self):
        part = sms.build_submit_pdus(
            "10086", "hello", smsc="+8613800100500"
        )[0]
        raw = bytes.fromhex(part.pdu)
        self.assertGreater(raw[0], 0)
        self.assertEqual(part.tpdu_length, len(raw) - 1 - raw[0])
        self.assertEqual(sms.decode_pdu(part.pdu).service_center, "+8613800100500")

    def test_gsm_boundaries_and_extension_character_are_not_split(self):
        self.assertEqual(len(sms.build_submit_pdus("10086", "A" * 160)), 1)
        parts = sms.build_submit_pdus("10086", "A" * 161, reference=0x42)
        self.assertEqual([len(p.text) for p in parts], [153, 8])
        self.assertEqual([p.part_number for p in parts], [1, 2])
        self.assertTrue(all(p.concat.reference == 0x42 for p in parts))

        parts = sms.build_submit_pdus("10086", "A" * 152 + "^" + "B" * 7, reference=1)
        self.assertEqual(parts[0].text, "A" * 152)
        self.assertEqual(parts[1].text, "^" + "B" * 7)

    def test_ucs2_boundaries_do_not_split_surrogate_pairs(self):
        self.assertEqual(len(sms.build_submit_pdus("10086", "中" * 70)), 1)
        parts = sms.build_submit_pdus("10086", "中" * 71, reference=2)
        self.assertEqual([len(p.text) for p in parts], [67, 4])
        self.assertTrue(all(p.encoding == "ucs2" for p in parts))

        value = "中" * 66 + "😀" + "甲" * 3
        parts = sms.build_submit_pdus("10086", value, reference=3)
        self.assertEqual(parts[0].text, "中" * 66)
        self.assertTrue(parts[1].text.startswith("😀"))
        self.assertEqual("".join(part.text for part in parts), value)

    def test_more_than_255_parts_is_rejected(self):
        with self.assertRaises(sms.SMSCodecError):
            sms.build_submit_pdus("10086", "中" * (67 * 255 + 1), reference=1)


class DeliverPduTests(unittest.TestCase):
    def test_decode_deliver_timestamp_and_qdc_zero_padding(self):
        pdu = _deliver_pdu("+12345678901", "hello")
        message = sms.decode_pdu(pdu + "0000000000")
        self.assertEqual(message.sender, "+12345678901")
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.encoding, "gsm7")
        self.assertEqual(message.timestamp.isoformat(), "2026-07-24T12:34:56+08:00")
        self.assertEqual(message.raw_pdu, pdu)

    def test_spare_bits_are_ignored_and_canonicalized_for_deduplication(self):
        pdu = _deliver_pdu("10086", "A")
        modified = bytearray.fromhex(pdu)
        modified[-1] |= 0x80
        decoded = sms.decode_pdu(modified.hex())
        self.assertEqual(decoded.text, "A")
        self.assertEqual(decoded.raw_pdu, pdu)

    def test_udh_fill_bits_are_canonicalized_for_deduplication(self):
        canonical = "004005810180F60000627042214365230805000301020182"
        nonzero_fill = canonical[:-2] + "83"
        first = sms.decode_pdu(canonical)
        second = sms.decode_pdu(nonzero_fill)
        self.assertEqual(first.text, "A")
        self.assertEqual(second.text, "A")
        self.assertEqual(first.raw_pdu, second.raw_pdu)

    def test_real_trailing_zero_in_message_is_not_stripped_with_slot_padding(self):
        pdu = _deliver_pdu("10086", "@")
        self.assertTrue(pdu.endswith("00"))
        decoded = sms.decode_pdu(pdu + "000000")
        self.assertEqual(decoded.text, "@")
        self.assertEqual(decoded.raw_pdu, pdu)

    def test_short_sender_is_not_forced_to_international(self):
        message = sms.decode_pdu(_deliver_pdu("10690", "code"))
        self.assertEqual(message.sender, "10690")

    def test_called_bcd_service_address_and_smsc_are_decoded(self):
        message = sms.decode_pdu("00000381BAFC00006270422143652305E8329BFD06")
        self.assertEqual(message.sender, "*#a")

        with_smsc = "0381BAFC" + _deliver_pdu("10086", "hello")[2:]
        self.assertEqual(sms.decode_pdu(with_smsc).service_center, "*#a")

    def test_decode_ucs2_and_8bit_concat(self):
        pdu = _deliver_pdu("+12345678901", "你好", concat=(0x44, 2, 1), ucs2=True)
        message = sms.decode_pdu(pdu)
        self.assertEqual(message.text, "你好")
        self.assertEqual(
            message.concat,
            sms.ConcatInfo(reference=0x44, total_parts=2, part_number=1, reference_bits=8),
        )
        self.assertFalse(message.complete)

    def test_decode_16bit_concat(self):
        pdu = _deliver_pdu(
            "+12345678901", "part", concat=(0xBEEF, 3, 2), concat_16bit=True
        )
        message = sms.decode_pdu(pdu)
        self.assertEqual(message.text, "part")
        self.assertEqual(message.concat.reference, 0xBEEF)
        self.assertEqual(message.concat.reference_bits, 16)
        self.assertEqual(message.concat.part_number, 2)

    def test_decode_spec_vectors_with_mms_flag_and_alphanumeric_sender(self):
        numeric = sms.decode_pdu("0004038101F100006270425103542305E8329BFD06")
        self.assertEqual(numeric.sender, "101")
        self.assertEqual(numeric.text, "hello")
        self.assertEqual(numeric.timestamp.isoformat(), "2026-07-24T15:30:45+08:00")

        alphanumeric = sms.decode_pdu(
            "000007D0C2A0730900006270422143652305E8329BFD06"
        )
        self.assertEqual(alphanumeric.sender, "BANK")
        self.assertEqual(alphanumeric.text, "hello")

    def test_timestamp_non_decimal_nibble_falls_back_and_negative_zone_is_kept(self):
        non_decimal = sms.decode_pdu(
            "0004038101F10000A270425103542305E8329BFD06"
        )
        self.assertEqual(non_decimal.timestamp.isoformat(), "2020-07-24T15:30:45+08:00")

        negative = bytearray.fromhex(_deliver_pdu("101", "hello"))
        negative[14] = 0x2B
        self.assertEqual(
            sms.decode_pdu(negative.hex()).timestamp.utcoffset(),
            -timedelta(hours=8),
        )

    def test_decode_exact_8bit_udh_submit_vector(self):
        message = sms.decode_pdu("00410005810180F6000008050003CC020182")
        self.assertEqual(message.destination, "10086")
        self.assertEqual(message.text, "A")
        self.assertEqual(message.concat, sms.ConcatInfo(0xCC, 2, 1, 8))

    def test_compressed_dcs_is_rejected(self):
        pdu = bytearray.fromhex(_deliver_pdu("10086", "hello"))
        pdu[8] = 0x20
        with self.assertRaises(sms.SMSCodecError):
            sms.decode_pdu(pdu.hex())

    def test_reserved_dcs_values_fall_back_to_gsm7(self):
        original = bytearray.fromhex(_deliver_pdu("10086", "hello"))
        for dcs in (
            0x0C, 0x1C, 0x2C, 0x4C, 0x5C, 0x6C,
            0x80, 0x9F, 0xBF, 0xE4, 0xEC, 0xF8, 0xFC, 0xFF,
        ):
            with self.subTest(dcs=dcs):
                pdu = bytearray(original)
                pdu[8] = dcs
                message = sms.decode_pdu(pdu.hex())
                self.assertEqual(message.encoding, "gsm7")
                self.assertEqual(message.text, "hello")

    def test_decode_rejects_malformed_pdus(self):
        valid = _deliver_pdu("10086", "hello")
        bad_values = (
            "0",
            "ZZ",
            "0003",  # Reserved MTI.
            valid[:-2],
            valid + "01",  # Non-padding bytes after declared TP-UD.
        )
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(sms.SMSCodecError):
                sms.decode_pdu(value)

    def test_invalid_concat_header_is_rejected(self):
        pdu = bytearray.fromhex(_deliver_pdu("10086", "hello", concat=(1, 2, 1)))
        # Locate the UDHL and claim more header bytes than TP-UD contains.
        pdu[-11] = 0x7F
        with self.assertRaises(sms.SMSCodecError):
            sms.decode_pdu(pdu.hex())


class StatusReportPduTests(unittest.TestCase):
    def test_decode_delivered_status_report(self):
        report = sms.decode_status_report_pdu(STATUS_REPORT_PDU, tpdu_length=26)

        self.assertEqual(report.message_reference, 0x2A)
        self.assertEqual(report.recipient, "+8613800000000")
        self.assertEqual(report.status, 0x00)
        self.assertEqual(report.outcome, "delivered")
        self.assertEqual(report.service_center_time.isoformat(), "2026-07-24T23:29:40+08:00")
        self.assertEqual(report.discharge_time.isoformat(), "2026-07-24T23:29:45+08:00")
        self.assertEqual(report.raw_pdu, STATUS_REPORT_PDU)

    def test_status_classes_are_conservative(self):
        expected = {
            0x01: "unconfirmed",
            0x20: "pending",
            0x3F: "pending",
            0x40: "failed",
            0x7F: "failed",
            0x80: "unconfirmed",
            0xFF: "unconfirmed",
        }
        for status, outcome in expected.items():
            with self.subTest(status=status):
                pdu = STATUS_REPORT_PDU[:-2] + f"{status:02X}"
                self.assertEqual(sms.decode_status_report_pdu(pdu).outcome, outcome)

    def test_status_report_rejects_wrong_type_truncation_and_declared_length(self):
        wrong_type = STATUS_REPORT_PDU[:2] + "01" + STATUS_REPORT_PDU[4:]
        for pdu, length in (
            ("ZZ", None),
            (STATUS_REPORT_PDU[:-2], None),
            (wrong_type, None),
            (STATUS_REPORT_PDU, 25),
            (STATUS_REPORT_PDU, 27),
        ):
            with self.subTest(pdu=pdu[:8], length=length):
                with self.assertRaises(sms.SMSCodecError) as caught:
                    sms.decode_status_report_pdu(pdu, tpdu_length=length)
                self.assertNotIn(STATUS_REPORT_PDU, str(caught.exception))

    def test_status_report_is_immutable(self):
        report = sms.decode_status_report_pdu(STATUS_REPORT_PDU)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.status = 1


class ModemResponseParsingTests(unittest.TestCase):
    def test_parse_cmgl_pdu_crops_qdc_slots_and_adds_metadata(self):
        first = _deliver_pdu("10086", "first")
        second = _deliver_pdu("+12345678901", "第二条", ucs2=True)
        first_len = len(bytes.fromhex(first)) - 1
        second_len = len(bytes.fromhex(second)) - 1
        response = (
            f'+CMGL: 7,"REC READ",,{first_len}\r\n{first}000000\r\n'
            f"+CMGL: 9,0,,{second_len}\r\n{second}\r\n\r\nOK\r\n"
        )
        messages = sms.parse_cmgl_pdu(response, storage="ME")
        self.assertEqual([message.index for message in messages], [7, 9])
        self.assertEqual([message.text for message in messages], ["first", "第二条"])
        self.assertEqual(messages[0].status, "REC READ")
        self.assertEqual(messages[1].status, "0")
        self.assertTrue(all(message.storage == "ME" for message in messages))
        self.assertEqual(messages[0].raw_pdu, first)

    def test_parse_cmgl_pdu_handles_qdc_fixed_247_octet_slot(self):
        pdu = _deliver_pdu("10086", "@")
        raw = bytes.fromhex(pdu)
        padded = raw + b"\x00" * (1 + 247 - len(raw))
        response = f"+CMGL: 7,1,,247\n{padded.hex()}\nOK"
        message = sms.parse_cmgl_pdu(response)[0]
        self.assertEqual(message.text, "@")
        self.assertEqual(message.raw_pdu, pdu)

    def test_parse_cmgl_pdu_keeps_valid_records_around_a_corrupt_record(self):
        first = _deliver_pdu("10086", "first")
        second = _deliver_pdu("10010", "second")
        first_length = len(bytes.fromhex(first)) - 1
        second_length = len(bytes.fromhex(second)) - 1
        response = (
            f"+CMGL: 1,1,,{first_length}\n{first}\n"
            "+CMGL: 2,1,,20\nnot-hex\n"
            f"+CMGL: 3,1,,{second_length}\n{second}\nOK"
        )

        messages = sms.parse_cmgl_pdu(response, storage="ME")

        self.assertEqual([message.index for message in messages], [1, 3])
        self.assertEqual([message.text for message in messages], ["first", "second"])

    def test_parse_cmgr_pdu(self):
        pdu = _deliver_pdu("10086", "one")
        length = len(bytes.fromhex(pdu)) - 1
        message = sms.parse_cmgr_pdu(
            f'+CMGR: "REC UNREAD",,{length}\n{pdu}\nOK', index=4, storage="SM"
        )
        self.assertEqual((message.index, message.storage, message.status), (4, "SM", "REC UNREAD"))
        self.assertEqual(message.text, "one")

    def test_parse_text_cmgl_preserves_multiline_body(self):
        response = (
            '+CMGL: 1,"REC READ","+12345678901","","26/07/24,12:34:56+32"\r\n'
            "line one\r\nline two\r\n"
            '+CMGL: 2,"REC UNREAD","10690","","26/07/24,12:35:00+32"\r\n'
            "验证码 123456\r\nOK\r\n"
        )
        messages = sms.parse_cmgl_text(response, storage="SM")
        self.assertEqual([message.index for message in messages], [1, 2])
        self.assertEqual(messages[0].text, "line one\nline two")
        self.assertEqual(messages[1].text, "验证码 123456")
        self.assertEqual(messages[1].sender, "10690")
        self.assertEqual(messages[1].timestamp.isoformat(), "2026-07-24T12:35:00+08:00")

    def test_parse_text_cmgl_decodes_explicit_ucs2_charset(self):
        sender = "服务号".encode("utf-16-be").hex().upper()
        body = "验证码 123456".encode("utf-16-be").hex().upper()
        response = (
            f'+CMGL: 3,"REC UNREAD","{sender}","","26/07/24,12:35:00+32"\r\n'
            f"{body}\r\nOK\r\n"
        )
        message = sms.parse_cmgl_text(response, storage="ME", charset="UCS2")[0]
        self.assertEqual(message.sender, "服务号")
        self.assertEqual(message.text, "验证码 123456")

    def test_parse_text_cmgl_decodes_ucs2_encoded_short_code(self):
        sender = "123".encode("utf-16-be").hex().upper()
        body = "test".encode("utf-16-be").hex().upper()
        response = (
            f'+CMGL: 4,"REC UNREAD","{sender}","","26/07/24,12:35:00+32"\n'
            f"{body}\nOK"
        )
        message = sms.parse_cmgl_text(response, charset="UCS2")[0]
        self.assertEqual(message.sender, "123")

    def test_parse_text_cmgl_rejects_invalid_ucs2_hex(self):
        response = (
            '+CMGL: 3,"REC UNREAD","10086","","26/07/24,12:35:00+32"\n'
            "NOT-HEX\nOK"
        )
        with self.assertRaises(sms.SMSCodecError):
            sms.parse_cmgl_text(response, charset="UCS2")

    def test_malformed_modem_records_are_rejected(self):
        with self.assertRaises(sms.SMSCodecError):
            sms.parse_cmgl_pdu("+CMGL: 1,0,,20\nnot-hex\nOK")
        with self.assertRaises(sms.SMSCodecError):
            sms.parse_cmgr_pdu("OK")
        with self.assertRaises(sms.SMSCodecError):
            sms.parse_cmgl_text('+CMGL: invalid\nbody\nOK')


class ReassemblyTests(unittest.TestCase):
    def test_reassembles_out_of_order_and_deduplicates_identical_parts(self):
        part1 = sms.decode_pdu(
            _deliver_pdu("10086", "hello ", concat=(9, 2, 1), ucs2=True)
        )
        part2 = sms.decode_pdu(
            _deliver_pdu("10086", "world", concat=(9, 2, 2), ucs2=True)
        )
        result = sms.reassemble_messages([part2, part1, part2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, "hello world")
        self.assertTrue(result[0].complete)
        self.assertEqual(result[0].parts_received, (1, 2))
        self.assertEqual(result[0].concat.part_number, None)
        self.assertEqual(result[0].raw_pdus, (part1.raw_pdu, part2.raw_pdu))

    def test_incomplete_group_does_not_expose_partial_text_as_complete(self):
        part = sms.decode_pdu(
            _deliver_pdu("10086", "middle", concat=(10, 3, 2), ucs2=True)
        )
        result = sms.reassemble_messages([part])[0]
        self.assertFalse(result.complete)
        self.assertIsNone(result.text)
        self.assertEqual(result.parts_received, (2,))

    def test_conflicting_sequence_starts_a_separate_incomplete_instance(self):
        part_a = sms.decode_pdu(
            _deliver_pdu("10086", "A", concat=(11, 2, 1), ucs2=True)
        )
        part_b = sms.decode_pdu(
            _deliver_pdu("10086", "B", concat=(11, 2, 1), ucs2=True)
        )
        result = sms.reassemble_messages([part_a, part_b])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(not message.complete for message in result))

    def test_reference_reuse_can_form_multiple_instances(self):
        first_a = sms.decode_pdu(
            _deliver_pdu("10086", "first ", concat=(12, 2, 1), ucs2=True)
        )
        first_b = sms.decode_pdu(
            _deliver_pdu("10086", "second ", concat=(12, 2, 1), ucs2=True)
        )
        last_a = sms.decode_pdu(
            _deliver_pdu("10086", "message", concat=(12, 2, 2), ucs2=True)
        )
        last_b = sms.decode_pdu(
            _deliver_pdu("10086", "note", concat=(12, 2, 2), ucs2=True)
        )
        result = sms.reassemble_messages([first_a, first_b, last_a, last_b])
        self.assertEqual([message.text for message in result], ["first message", "second note"])

    def test_dataclasses_are_immutable(self):
        message = sms.decode_pdu(_deliver_pdu("10086", "hello"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            message.text = "changed"


if __name__ == "__main__":
    unittest.main()
