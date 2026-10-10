import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dji4g


def args(**overrides):
    values = {
        "to": "+8613800000000",
        "text": "hello",
        "text_file": None,
        "encoding": "auto",
        "execute": False,
        "storage": "all",
        "mode": "pdu",
        "json": False,
        "show_sender": False,
        "poll": 0.01,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def message(*, sender="+8613912345678", text="code 123456", raw_pdu="0011", index=7):
    return SimpleNamespace(
        sender=sender,
        text=text,
        encoding="ucs2",
        timestamp=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
        storage="ME",
        index=index,
        status="REC READ",
        complete=True,
        parts_received=(),
        raw_pdu=raw_pdu,
        raw_pdus=(),
    )


class ParserTests(unittest.TestCase):
    def test_sms_subcommands_and_safe_defaults(self):
        parser = dji4g.build_parser()

        listed = parser.parse_args(["sms", "list"])
        self.assertIs(listed.handler, dji4g.command_sms_list)
        self.assertEqual(listed.storage, "all")
        self.assertEqual(listed.mode, "pdu")
        self.assertFalse(listed.json)
        self.assertFalse(listed.show_sender)

        sent = parser.parse_args(["sms", "send", "--to", "10086", "--text", "hello"])
        self.assertIs(sent.handler, dji4g.command_sms_send)
        self.assertFalse(sent.execute)
        self.assertEqual(sent.encoding, "auto")
        self.assertEqual(sent.mode, "auto")

        watched = parser.parse_args(["sms", "watch", "--storage", "SM"])
        self.assertIs(watched.handler, dji4g.command_sms_watch)
        self.assertEqual(watched.storage, "SM")

        for removed in (["sms", "serve"], ["telegram", "serve"], ["sms", "list", "--direct"]):
            with self.subTest(argv=removed), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(removed)

    def test_send_requires_exactly_one_text_source_and_has_no_yes_bypass(self):
        parser = dji4g.build_parser()
        invalid = (
            ["sms", "send", "--to", "10086"],
            [
                "sms",
                "send",
                "--to",
                "10086",
                "--text",
                "hello",
                "--text-file",
                "body.txt",
            ],
            ["sms", "send", "--to", "10086", "--text", "hello", "--yes"],
        )
        for command in invalid:
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(command)


class SafetyTests(unittest.TestCase):
    @patch("dji4g.build_submit_pdus")
    def test_send_without_execute_only_prints_masked_local_preview(self, build_pdus):
        build_pdus.return_value = [SimpleNamespace(encoding="ucs2")]
        output = io.StringIO()

        with (
            patch("dji4g._confirm_sms_send") as confirm,
            patch("dji4g._direct_send") as direct_send,
            redirect_stdout(output),
        ):
            dji4g.command_sms_send(args())

        rendered = output.getvalue()
        self.assertNotIn("+8613800000000", rendered)
        self.assertIn("encoding=ucs2", rendered)
        self.assertIn("segments=1", rendered)
        self.assertIn("preview only", rendered.lower())
        confirm.assert_not_called()
        direct_send.assert_not_called()

    @patch("dji4g.build_submit_pdus")
    def test_execute_still_does_nothing_when_tty_confirmation_is_not_exact(self, build_pdus):
        build_pdus.return_value = [SimpleNamespace(encoding="gsm7")]
        with (
            patch("dji4g._confirm_sms_send", return_value=False),
            patch("dji4g._direct_send") as direct_send,
            self.assertRaisesRegex(dji4g.Dji4gError, "not confirmed"),
        ):
            dji4g.command_sms_send(args(execute=True))
        direct_send.assert_not_called()

    def test_confirmation_is_read_from_dev_tty_and_requires_exact_send(self):
        handle = Mock()
        handle.__enter__ = Mock(return_value=handle)
        handle.__exit__ = Mock(return_value=False)
        handle.readline.return_value = "SEND\n"
        with patch("builtins.open", return_value=handle) as opened:
            self.assertTrue(dji4g._confirm_sms_send())
        opened.assert_called_once_with("/dev/tty", "r+", encoding="utf-8", buffering=1)

        handle.readline.return_value = "send\n"
        with patch("builtins.open", return_value=handle):
            self.assertFalse(dji4g._confirm_sms_send())

    @patch("dji4g.build_submit_pdus")
    def test_confirmed_direct_send_uses_the_already_previewed_input(self, build_pdus):
        build_pdus.return_value = [SimpleNamespace(encoding="gsm7")]
        receipt = SimpleNamespace(status="accepted_by_modem", segment_count=1)
        with (
            patch("dji4g._confirm_sms_send", return_value=True),
            patch("dji4g._direct_send", return_value=receipt) as direct_send,
            redirect_stdout(io.StringIO()),
        ):
            dji4g.command_sms_send(args(execute=True))

        direct_send.assert_called_once_with(
            "+8613800000000", "hello", encoding="auto", mode="pdu"
        )

    def test_dash_text_file_reads_stdin_instead_of_the_filesystem(self):
        arguments = args(text=None, text_file=Path("-"))
        with (
            patch("dji4g.sys.stdin", io.StringIO("stdin body")),
            patch("pathlib.Path.read_text") as read_text,
        ):
            self.assertEqual(dji4g._read_sms_text(arguments), "stdin body")
        read_text.assert_not_called()

class ListAndWatchTests(unittest.TestCase):
    def test_text_mode_watch_is_rejected(self):
        with (
            patch("dji4g._watch_direct") as direct,
            self.assertRaisesRegex(dji4g.Dji4gError, "requires PDU mode"),
        ):
            dji4g.command_sms_watch(args(mode="text"))
        direct.assert_not_called()

    def test_list_reads_the_modem_and_masks_the_sender(self):
        secret = "+8613912345678"
        output = io.StringIO()
        with (
            patch("dji4g._direct_list", return_value=[message(sender=secret)]) as direct,
            redirect_stdout(output),
        ):
            dji4g.command_sms_list(args())

        direct.assert_called_once_with("all", "pdu")
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("**", output.getvalue())

    def test_json_list_masks_sender_and_contains_no_raw_pdu(self):
        secret = "+8613912345678"
        output = io.StringIO()
        with (
            patch("dji4g._direct_list", return_value=[message(sender=secret)]),
            redirect_stdout(output),
        ):
            dji4g.command_sms_list(args(json=True))

        payload = json.loads(output.getvalue())
        self.assertNotEqual(payload[0]["sender"], secret)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("raw_pdu", payload[0])

    def test_direct_watch_uses_initial_fingerprints_and_never_deletes(self):
        old = message(text="old", raw_pdu="0011", index=1)
        new = message(text="new", raw_pdu="0022", index=2)

        class FakeModem:
            def __init__(self):
                self.results = [[old], [old, new]]
                self.closed = False

            def list_complete_messages(self, storages, *, mode):
                return self.results.pop(0)

            def close(self):
                self.closed = True

        modem = FakeModem()
        output = io.StringIO()
        with (
            patch("dji4g.SmsModem.open", return_value=modem),
            patch("dji4g.time.sleep", side_effect=[None, KeyboardInterrupt]),
            redirect_stdout(output),
            self.assertRaises(KeyboardInterrupt),
        ):
            dji4g.command_sms_watch(args())

        self.assertTrue(modem.closed)
        self.assertNotIn("old", output.getvalue())
        self.assertIn("new", output.getvalue())
        self.assertFalse(hasattr(modem, "delete_message"))

    def test_busy_modem_points_at_the_running_sms_engine(self):
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["dji4g.py", "sms", "list"]),
            patch("dji4g._direct_list", side_effect=dji4g.AtBusyError("owned")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(dji4g.main(), 1)
        self.assertIn("menu bar app", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
