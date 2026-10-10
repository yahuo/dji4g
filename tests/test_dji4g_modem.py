import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dji4g_modem


STATUS_REPORT_PDU = "00022A0D91683108000000F0627042329204236270423292542300"


class FakeTransport:
    def __init__(self):
        self.commands = []
        self.prompt_commands = []
        self.closed = False
        self.unsolicited = ['+CMTI: "ME",7\r\n']

    def send(self, command, timeout=3.0, completion=None):
        self.commands.append(command)
        responses = {
            "AT": "AT\nOK",
            "AT+CMGF?": "+CMGF: 1\nOK",
            "AT+CPMS?": '+CPMS: "ME",0,23,"ME",0,23,"ME",0,23\nOK',
            "AT+CSCS?": '+CSCS: "GSM"\nOK',
            "AT+CNMI?": "+CNMI: 2,1,0,0,0\nOK",
            "AT+CPIN?": "+CPIN: READY\nOK",
            "AT+CEREG?": "+CEREG: 0,1\nOK",
            "AT+CSQ": "+CSQ: 24,99\nOK",
            "AT+QNWINFO": '+QNWINFO: "FDD LTE","46011","LTE BAND 3",1850\nOK',
            'AT+QENG="servingcell"': (
                '+QENG: "servingcell","NOCONN","LTE","FDD",460,11,'
                "1A2B3C4,413,1850,3,5,5,5D6E,-69,-6,-42,20,58\nOK"
            ),
            'AT+CPMS="SM","SM","SM"': "+CPMS: 1,10,1,10,1,10\nOK",
            'AT+CPMS="ME","ME","ME"': "+CPMS: 4,23,4,23,4,23\nOK",
            "AT+CMGL=4": "+CMGL: 1,1,,2\n0001\nOK",
            'AT+CMGL="ALL"': '+CMGL: 1,"REC READ","00310030003000380036","","24/07/24,15:30:00+32"\n00680069\nOK',
        }
        return responses.get(command, "OK")

    def send_with_prompt(self, command, follow_up, timeout=30.0):
        self.prompt_commands.append((command, follow_up, timeout))
        return "+CMGS: 17\nOK"

    def read_unsolicited(self, timeout=0.25):
        return self.unsolicited.pop(0) if self.unsolicited else ""

    def close(self):
        self.closed = True


class RestoreFailsAfterSubmitTransport(FakeTransport):
    def send(self, command, timeout=3.0, completion=None):
        if command == "AT+CMGF=1" and self.prompt_commands:
            self.commands.append(command)
            return "ERROR"
        return super().send(command, timeout=timeout, completion=completion)


class ParserTests(unittest.TestCase):
    def test_parse_current_sms_settings(self):
        self.assertEqual(dji4g_modem.parse_cmgf("+CMGF: 0\nOK"), 0)
        self.assertEqual(
            dji4g_modem.parse_cpms_storages(
                '+CPMS: "ME",0,23,"SM",1,10,"ME",0,23\nOK'
            ),
            ("ME", "SM", "ME"),
        )
        self.assertEqual(dji4g_modem.parse_cscs('+CSCS: "UCS2"\nOK'), "UCS2")
        self.assertEqual(dji4g_modem.parse_cnmi("+CNMI: 2, 1, 0, 0, 0\nOK"), "2,1,0,0,0")

    def test_parse_cpms_usage_and_registration_states(self):
        self.assertEqual(
            dji4g_modem.parse_cpms_usage(
                '+CPMS: "ME",0,23,"SM",1,10,"ME",0,23\nOK'
            ),
            {"ME": {"used": 0, "total": 23}, "SM": {"used": 1, "total": 10}},
        )
        self.assertEqual(
            dji4g_modem.parse_cpms_usage(
                "+CPMS: 4,23,4,23,4,23\nOK", selected_storage="ME"
            ),
            {"ME": {"used": 4, "total": 23}},
        )
        for response, expected in (
            ("+CEREG: 0,1\nOK", "home"),
            ("+CEREG: 5\nOK", "roaming"),
            ("+CEREG: 0,2\nOK", "searching"),
            ("+CEREG: 0,3\nOK", "denied"),
            ("+CEREG: 0,0\nOK", "not_registered"),
            ("ERROR", "unknown"),
        ):
            with self.subTest(response=response):
                self.assertEqual(
                    dji4g_modem.parse_registration_status(response), expected
                )
        self.assertEqual(
            dji4g_modem.parse_registration_status(
                "+CEREG: 2\n+CEREG: 2,1\nOK"
            ),
            "home",
        )
        self.assertEqual(
            dji4g_modem.parse_registration_status(
                '+CEREG: 2,1\n+CEREG: 1,"1234","00A1047B",7\nOK'
            ),
            "home",
        )

    def test_parse_csq_qnwinfo_and_qeng_without_identifiers(self):
        self.assertEqual(dji4g_modem.parse_csq("+CSQ: 24,99\nOK"), 24)
        self.assertIsNone(dji4g_modem.parse_csq("+CSQ: 99,99\nOK"))
        self.assertEqual(
            dji4g_modem.parse_qnwinfo("+QNWINFO: No Service\nOK"),
            {"parse_status": "no_service", "service": "no_service"},
        )
        network = dji4g_modem.parse_qnwinfo(
            '+QNWINFO: "FDD LTE","46001","LTE BAND 8",3740\nOK'
        )
        self.assertEqual(
            network,
            {
                "parse_status": "ok",
                "service": "registered",
                "rat": "LTE",
                "duplex": "FDD",
                "band": 8,
                "channel": 3740,
            },
        )
        for access in ("GPRS", "HSDPA", "HSUPA", "HSPA+", "TDSCDMA", "CAT-M1"):
            with self.subTest(access=access):
                parsed = dji4g_modem.parse_qnwinfo(
                    f'+QNWINFO: "{access}","46001","LTE BAND 8",3740\nOK'
                )
                self.assertEqual(parsed["parse_status"], "ok")
                self.assertEqual(parsed["rat"], access)
        radio = dji4g_modem.parse_qeng_servingcell(
            '+QENG: "servingcell","NOCONN","LTE","FDD",460,11,'
            "1A2B3C4,413,1850,3,5,5,5D6E,-69,-6,-42,20,58\nOK"
        )
        self.assertEqual(
            radio,
            {
                "parse_status": "ok",
                "service": "registered",
                "state": "idle",
                "rat": "LTE",
                "duplex": "FDD",
                "channel": 1850,
                "band": 3,
                "rsrp_dbm": -69,
                "rsrq_db": -6,
                "rssi_dbm": -42,
                "sinr_db": 20,
            },
        )
        encoded = repr(radio)
        for private_value in ("460", "1A2B3C4", "413", "5D6E"):
            self.assertNotIn(private_value, encoded)
        partial = dji4g_modem.parse_qeng_servingcell(
            '+QENG: "servingcell","NOCONN","LTE","FDD",460,11,'
            "1A2B3C4,413,1850,3,5,5,5D6E,-69,-6,-42,169,58\nOK"
        )
        self.assertEqual(partial["parse_status"], "ok")
        self.assertEqual(partial["rsrp_dbm"], -69)
        self.assertNotIn("sinr_db", partial)

    def test_parse_cmti_rejects_non_numeric_index(self):
        response = '+CMTI: "ME",7\n+CMTI: "SM",bad\n'
        self.assertEqual(dji4g_modem.parse_cmti(response), [("ME", 7)])


class SmsModemTests(unittest.TestCase):
    def test_list_scans_sm_and_me_without_deleting_and_restores_settings(self):
        transport = FakeTransport()
        modem = dji4g_modem.SmsModem(transport)
        decoded = SimpleNamespace(raw_pdu="0011")

        with patch("dji4g_modem.parse_cmgl_pdu", return_value=[decoded]):
            messages = modem.list_messages()

        self.assertEqual(messages, [decoded])
        self.assertIn('AT+CPMS="SM","SM","SM"', transport.commands)
        self.assertIn('AT+CPMS="ME","ME","ME"', transport.commands)
        self.assertIn("AT+CMGF=1", transport.commands)
        self.assertFalse(any("CMGD" in command for command in transport.commands))
        self.assertEqual(
            modem.health()["storage"],
            {
                "SM": {"used": 1, "total": 10},
                "ME": {"used": 4, "total": 23},
            },
        )

    def test_pdu_send_uses_prompt_and_ctrl_z(self):
        transport = FakeTransport()
        modem = dji4g_modem.SmsModem(transport)
        part = SimpleNamespace(
            pdu="001122",
            tpdu_length=2,
            encoding="gsm7",
        )

        with patch("dji4g_modem.build_submit_pdus", return_value=[part]):
            receipt = modem.send_sms("10086", "hello")

        self.assertEqual(receipt.segment_count, 1)
        self.assertEqual(receipt.status, "accepted_by_modem")
        command, payload, _timeout = transport.prompt_commands[0]
        self.assertEqual(command, "AT+CMGS=2")
        self.assertEqual(payload, b"001122\x1a")

    def test_gateway_send_requests_status_reports(self):
        transport = FakeTransport()
        modem = dji4g_modem.SmsModem(transport)
        part = SimpleNamespace(pdu="001122", tpdu_length=2, encoding="gsm7")

        with patch(
            "dji4g_modem.build_submit_pdus", return_value=[part]
        ) as builder:
            modem.send_message("10086", "hello")

        builder.assert_called_once_with(
            "10086", "hello", encoding="auto", request_status_report=True
        )

    def test_configure_delivery_reports_preserves_cnmi_fields(self):
        transport = FakeTransport()
        modem = dji4g_modem.SmsModem(transport)

        modem.configure_delivery_reports()

        self.assertIn("AT+CMGF=0", transport.commands)
        self.assertIn("AT+CNMI=2,1,0,1,0", transport.commands)

    def test_read_delivery_reports_keeps_cmti_separate(self):
        transport = FakeTransport()
        transport.unsolicited = [
            f'+CMTI: "ME",7\r\n+CDS: 26\r\n{STATUS_REPORT_PDU}\r\n'
        ]
        modem = dji4g_modem.SmsModem(transport)

        reports = modem.read_delivery_reports()

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].message_reference, 0x2A)
        self.assertEqual(reports[0].outcome, "delivered")
        self.assertEqual(modem.read_notifications(timeout=0), [("ME", 7)])

    def test_delivery_report_header_survives_an_unsolicited_read_boundary(self):
        transport = FakeTransport()
        transport.unsolicited = ["+CDS: 26\r\n", STATUS_REPORT_PDU + "\r\n"]
        modem = dji4g_modem.SmsModem(transport)

        self.assertEqual(modem.read_delivery_reports(), [])
        reports = modem.read_delivery_reports()

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].message_reference, 0x2A)

    def test_delivery_report_pdu_survives_an_unsolicited_read_boundary(self):
        transport = FakeTransport()
        split = len(STATUS_REPORT_PDU) // 2
        transport.unsolicited = [
            "+CDS: 26\r\n" + STATUS_REPORT_PDU[:split],
            STATUS_REPORT_PDU[split:] + "\r\n",
        ]
        modem = dji4g_modem.SmsModem(transport)

        self.assertEqual(modem.read_delivery_reports(), [])
        reports = modem.read_delivery_reports()

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].outcome, "delivered")

    def test_delivery_report_header_survives_a_mid_line_read_boundary(self):
        transport = FakeTransport()
        transport.unsolicited = [
            "+CD",
            "S: 26\r\n" + STATUS_REPORT_PDU + "\r\n",
        ]
        modem = dji4g_modem.SmsModem(transport)

        self.assertEqual(modem.read_delivery_reports(), [])
        reports = modem.read_delivery_reports()

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].message_reference, 0x2A)

    def test_delivery_report_length_survives_a_mid_number_read_boundary(self):
        transport = FakeTransport()
        transport.unsolicited = [
            "+CDS: 2",
            "6\r\n" + STATUS_REPORT_PDU + "\r\n",
        ]
        modem = dji4g_modem.SmsModem(transport)

        self.assertEqual(modem.read_delivery_reports(), [])
        reports = modem.read_delivery_reports()

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].outcome, "delivered")

    def test_partial_common_urc_prefix_does_not_swallow_cmti(self):
        transport = FakeTransport()
        transport.unsolicited = ["+C", 'MTI: "ME",7\r\n']
        modem = dji4g_modem.SmsModem(transport)

        self.assertEqual(modem.read_delivery_reports(), [])

        self.assertEqual(modem.read_notifications(), [("ME", 7)])

    def test_pdu_send_marks_post_submit_restore_failure_uncertain(self):
        transport = RestoreFailsAfterSubmitTransport()
        modem = dji4g_modem.SmsModem(transport)
        part = SimpleNamespace(pdu="001122", tpdu_length=2, encoding="gsm7")

        with patch("dji4g_modem.build_submit_pdus", return_value=[part]):
            with self.assertRaises(dji4g_modem.SmsSendError) as caught:
                modem.send_sms("10086", "hello")

        self.assertTrue(caught.exception.submitted)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.sent_parts, 1)

    def test_text_send_marks_post_submit_restore_failure_uncertain(self):
        transport = RestoreFailsAfterSubmitTransport()
        modem = dji4g_modem.SmsModem(transport)
        part = SimpleNamespace(encoding="gsm7")

        with patch("dji4g_modem.build_submit_pdus", return_value=[part]):
            with self.assertRaises(dji4g_modem.SmsSendError) as caught:
                modem.send_sms("10086", "hello", mode="text")

        self.assertTrue(caught.exception.submitted)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.sent_parts, 1)

    def test_text_list_uses_ucs2_and_restores_settings(self):
        transport = FakeTransport()
        modem = dji4g_modem.SmsModem(transport)
        decoded = SimpleNamespace(
            raw_pdu="",
            concat=None,
            storage="ME",
            index=1,
            sender="10086",
            timestamp="2026-07-24T15:30:00+08:00",
            text="one",
        )
        decoded_two = SimpleNamespace(
            raw_pdu="",
            concat=None,
            storage="ME",
            index=2,
            sender="10086",
            timestamp="2026-07-24T15:31:00+08:00",
            text="two",
        )
        with patch(
            "dji4g_modem.parse_cmgl_text", return_value=[decoded, decoded_two]
        ) as parser:
            messages = modem.list_messages("ME", mode="text")
        self.assertEqual(messages, [decoded, decoded_two])
        self.assertIn("AT+CMGF=1", transport.commands)
        self.assertIn('AT+CSCS="UCS2"', transport.commands)
        parser.assert_called_once_with(
            unittest.mock.ANY, storage="ME", charset="UCS2"
        )

    def test_text_mode_rejects_long_message(self):
        modem = dji4g_modem.SmsModem(FakeTransport())
        parts = [
            SimpleNamespace(encoding="ucs2"),
            SimpleNamespace(encoding="ucs2"),
        ]
        with patch("dji4g_modem.build_submit_pdus", return_value=parts):
            with self.assertRaisesRegex(dji4g_modem.SmsModemError, "one segment"):
                modem.send_sms("10086", "中文" * 100, mode="text")

    def test_notifications_and_health_are_sanitized(self):
        modem = dji4g_modem.SmsModem(FakeTransport())
        self.assertEqual(modem.read_notifications(), [("ME", 7)])
        self.assertEqual(
            modem.health(),
            {
                "connected": True,
                "ready": True,
                "at_ready": True,
                "sim_ready": True,
                "registered": True,
                "registration": "home",
                "signal": 24,
                "radio": {
                    "parse_status": "ok",
                    "service": "registered",
                    "state": "idle",
                    "rat": "LTE",
                    "duplex": "FDD",
                    "channel": 1850,
                    "band": 3,
                    "rsrp_dbm": -69,
                    "rsrq_db": -6,
                    "rssi_dbm": -42,
                    "sinr_db": 20,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
