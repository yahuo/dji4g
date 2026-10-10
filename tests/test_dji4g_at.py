import os
import stat
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import dji4g_at


STATUS_REPORT_PDU = "00022A0D91683108000000F0627042329204236270423292542300"


class FakeUsbTimeout(Exception):
    errno = 110


class FakeUsbFailure(Exception):
    pass


class FakeUsbInterface(list):
    def __init__(self, endpoints, number=0, interface_class=0xFF):
        super().__init__(endpoints)
        self.bInterfaceNumber = number
        self.bInterfaceClass = interface_class


class FakeInEndpoint:
    def __init__(self, chunks=()):
        self.chunks = deque(chunks)

    def read(self, _size, timeout):
        if self.chunks:
            item = self.chunks.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        raise FakeUsbTimeout(f"timed out after {timeout} ms")


class FakeOutEndpoint:
    def __init__(self, on_write=None):
        self.writes = []
        self.on_write = on_write

    def write(self, data, timeout):
        raw = bytes(data)
        self.writes.append((raw, timeout))
        if self.on_write is not None:
            self.on_write(raw)
        return len(raw)


class ResponseTests(unittest.TestCase):
    def test_response_complete_requires_a_terminal_line(self):
        for response in (
            "AT\nOK",
            "ERROR",
            "+CME ERROR: 10",
            "+CMS ERROR: 500",
        ):
            with self.subTest(response=response):
                self.assertTrue(dji4g_at.response_complete(response))

        for response in ("NOT OKAY", "+CMS ERRORISH", "prefix ERROR suffix"):
            with self.subTest(response=response):
                self.assertFalse(dji4g_at.response_complete(response))

    def test_response_ok_requires_an_exact_ok_line(self):
        self.assertTrue(dji4g_at.response_ok("AT\nOK"))
        self.assertFalse(dji4g_at.response_ok("NOT OKAY"))


class TransportTests(unittest.TestCase):
    def make_transport(self, chunks=()):
        in_endpoint = FakeInEndpoint(chunks)
        out_endpoint = FakeOutEndpoint()
        transport = dji4g_at.AtTransport(
            out_endpoint=out_endpoint,
            in_endpoint=in_endpoint,
            use_lock=False,
        )
        self.addCleanup(transport.close)
        return transport, out_endpoint, in_endpoint

    def test_send_reassembles_fragmented_response(self):
        transport, out_endpoint, _ = self.make_transport(
            (
                b"AT+C",
                b"SQ\r\r\n+CSQ: 20,99\r\n",
                b"\r\nO",
                b"K\r\n",
            )
        )

        response = transport.send("AT+CSQ", timeout=0.2)

        self.assertEqual(out_endpoint.writes[0][0], b"AT+CSQ\r\n")
        self.assertIn("+CSQ: 20,99", response)
        self.assertTrue(response.endswith("OK"))

    def test_send_stops_on_all_error_forms(self):
        for terminal in (b"ERROR\r\n", b"+CME ERROR: 10\r\n", b"+CMS ERROR: 500\r\n"):
            with self.subTest(terminal=terminal):
                transport, _, in_endpoint = self.make_transport((terminal, b"unread"))
                response = transport.send("AT+FAIL", timeout=0.2)
                self.assertIn(terminal.decode().strip(), response)
                self.assertEqual(list(in_endpoint.chunks), [b"unread"])
                transport.close()

    def test_send_accepts_custom_completion_without_stopping_at_ok(self):
        transport, _, _ = self.make_transport(
            (b"OK\r\n", b"+QPING: 0,1,1,12\r\n", b"+QPING: 0,4,1,12\r\n")
        )

        response = transport.send(
            'AT+QPING=1,"1.1.1.1",4,1',
            timeout=0.2,
            completion=lambda value: "+QPING: 0,4," in value,
        )

        self.assertIn("+QPING: 0,4,", response)

    def test_send_rejects_invalid_command_and_timeout(self):
        transport, out_endpoint, _ = self.make_transport()

        for command in ("", "   ", "AT\r+CSQ", "AT\n+CSQ"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    transport.send(command)
        with self.assertRaises(ValueError):
            transport.send("AT", timeout=0)
        self.assertEqual(out_endpoint.writes, [])

    def test_send_timeout_exposes_partial_response_as_not_submitted(self):
        transport, _, _ = self.make_transport((b"AT+SLOW\r\npartial",))

        with self.assertRaises(dji4g_at.AtTimeoutError) as raised:
            transport.send("AT+SLOW", timeout=0.01)

        self.assertIn("partial", raised.exception.partial_response)
        self.assertFalse(raised.exception.follow_up_sent)
        self.assertFalse(raised.exception.submitted)

    def test_command_with_prompt_writes_follow_up_verbatim(self):
        transport, out_endpoint, _ = self.make_transport(
            (
                b"AT+CMGS=10\r\r\n",
                b"> ",
                b"\r\n+CMGS: 7\r\n",
                b"OK\r\n",
            )
        )
        follow_up = b"0011000B915155550521F30000AA02C834\x1a"

        response = transport.execute(
            dji4g_at.CommandWithPrompt(
                command="AT+CMGS=10",
                follow_up=follow_up,
                prompt_timeout=0.2,
                timeout=0.2,
            )
        )

        self.assertEqual(
            [data for data, _ in out_endpoint.writes],
            [b"AT+CMGS=10\r\n", follow_up],
        )
        self.assertIn("+CMGS: 7", response)
        self.assertTrue(response.endswith("OK"))

    def test_prompt_can_be_fragmented(self):
        transport, out_endpoint, _ = self.make_transport(
            (b"AT+CMGS=1\r\n", b"\r\n", b">", b" ", b"OK\r\n")
        )

        response = transport.send_with_prompt(
            "AT+CMGS=1", b"00\x1a", prompt_timeout=0.2, timeout=0.2
        )

        self.assertEqual(out_endpoint.writes[1][0], b"00\x1a")
        self.assertTrue(response.endswith("OK"))

    def test_error_before_prompt_does_not_send_follow_up(self):
        transport, out_endpoint, _ = self.make_transport((b"+CMS ERROR: 302\r\n",))

        response = transport.send_with_prompt(
            "AT+CMGS=1", b"00\x1a", prompt_timeout=0.2, timeout=0.2
        )

        self.assertIn("+CMS ERROR: 302", response)
        self.assertEqual([data for data, _ in out_endpoint.writes], [b"AT+CMGS=1\r\n"])

    def test_prompt_timeout_sends_escape_and_is_not_submitted(self):
        transport, out_endpoint, _ = self.make_transport((b"AT+CMGS=1\r\n",))

        with self.assertRaises(dji4g_at.AtTimeoutError) as raised:
            transport.send_with_prompt(
                "AT+CMGS=1", b"00\x1a", prompt_timeout=0.01, timeout=0.2
            )

        self.assertEqual(out_endpoint.writes[-1][0], b"\x1b")
        self.assertFalse(raised.exception.submitted)

    def test_timeout_after_follow_up_sends_escape_and_is_submitted(self):
        transport, out_endpoint, _ = self.make_transport((b"> ", b"+CMGS: 7\r\n"))

        with self.assertRaises(dji4g_at.AtTimeoutError) as raised:
            transport.send_with_prompt(
                "AT+CMGS=1", b"00\x1a", prompt_timeout=0.2, timeout=0.01
            )

        self.assertEqual(
            [data for data, _ in out_endpoint.writes],
            [b"AT+CMGS=1\r\n", b"00\x1a", b"\x1b"],
        )
        self.assertIn("+CMGS: 7", raised.exception.partial_response)
        self.assertTrue(raised.exception.follow_up_sent)
        self.assertTrue(raised.exception.submitted)

    def test_follow_up_write_timeout_is_treated_as_submitted(self):
        in_endpoint = FakeInEndpoint((b"> ",))
        write_count = 0

        def on_write(_data):
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                raise FakeUsbTimeout("write timed out")

        out_endpoint = FakeOutEndpoint(on_write)
        transport = dji4g_at.AtTransport(
            out_endpoint=out_endpoint,
            in_endpoint=in_endpoint,
            use_lock=False,
        )
        self.addCleanup(transport.close)

        with self.assertRaises(dji4g_at.AtTimeoutError) as raised:
            transport.send_with_prompt(
                "AT+CMGS=1", b"00\x1a", prompt_timeout=0.2, timeout=0.2
            )

        self.assertTrue(raised.exception.submitted)
        self.assertEqual(out_endpoint.writes[-1][0], b"\x1b")

    def test_prompt_rejects_non_bytes_follow_up(self):
        transport, out_endpoint, _ = self.make_transport()
        with self.assertRaises(TypeError):
            transport.send_with_prompt("AT+CMGS=1", "00\x1a")
        self.assertEqual(out_endpoint.writes, [])

    def test_read_unsolicited_reassembles_available_chunks(self):
        transport, _, _ = self.make_transport((b'\r\n+CMT', b'I: "ME",4\r\n'))

        response = transport.read_unsolicited(timeout=0.01)

        self.assertEqual(response, '+CMTI: "ME",4')

    def test_read_unsolicited_keeps_an_incomplete_line_for_the_next_call(self):
        transport, _, _ = self.make_transport(
            (
                b"+CDS: 2",
                FakeUsbTimeout("boundary"),
                ("6\r\n" + STATUS_REPORT_PDU + "\r\n").encode("ascii"),
            )
        )

        self.assertEqual(transport.read_unsolicited(timeout=0.01), "")
        self.assertEqual(
            transport.read_unsolicited(timeout=0.01),
            f"+CDS: 26\n{STATUS_REPORT_PDU}",
        )

    def test_delivery_report_interleaved_with_command_is_queued(self):
        report = f"+CDS: 26\r\n{STATUS_REPORT_PDU}\r\n"
        transport, _, _ = self.make_transport(
            (("AT\r\n" + report + "OK\r\n").encode("ascii"),)
        )

        response = transport.send("AT", timeout=0.2)

        self.assertEqual(response, "AT\n\nOK")
        self.assertEqual(
            transport.read_unsolicited(timeout=0.01),
            f"+CDS: 26\n{STATUS_REPORT_PDU}",
        )

    def test_fragmented_delivery_report_is_not_queued_until_the_pdu_is_complete(self):
        split = len(STATUS_REPORT_PDU) // 2
        transport, _, _ = self.make_transport(
            (
                ("AT\r\n+CDS: 26\r\n" + STATUS_REPORT_PDU[:split]).encode("ascii"),
                (STATUS_REPORT_PDU[split:] + "\r\nOK\r\n").encode("ascii"),
            )
        )

        response = transport.send("AT", timeout=0.2)

        self.assertEqual(response, "AT\n\nOK")
        self.assertEqual(
            transport.read_unsolicited(timeout=0.01),
            f"+CDS: 26\n{STATUS_REPORT_PDU}",
        )

    def test_cdsi_and_text_mode_cds_are_not_swallowed(self):
        transport, _, _ = self.make_transport(
            (b'AT\r\n+CDSI: "MT",3\r\n+CDS: 2,10,"masked"\r\nOK\r\n',)
        )

        response = transport.send("AT", timeout=0.2)

        self.assertIn('+CDSI: "MT",3', response)
        self.assertIn('+CDS: 2,10,"masked"', response)
        self.assertEqual(transport.read_unsolicited(timeout=0.01), "")

    def test_commands_are_serialized_within_the_process(self):
        state = {"active": False, "concurrent": False}
        chunks = deque()

        def on_write(data):
            if data == b"\x1b":
                return
            if state["active"]:
                state["concurrent"] = True
            state["active"] = True
            chunks.append(data.rstrip(b"\r\n") + b"\r\nOK\r\n")

        class SlowInEndpoint:
            def read(self, _size, timeout):
                if not chunks:
                    raise FakeUsbTimeout(f"timed out after {timeout} ms")
                time.sleep(0.02)
                response = chunks.popleft()
                state["active"] = False
                return response

        out_endpoint = FakeOutEndpoint(on_write)
        transport = dji4g_at.AtTransport(
            out_endpoint=out_endpoint,
            in_endpoint=SlowInEndpoint(),
            use_lock=False,
        )
        self.addCleanup(transport.close)
        barrier = threading.Barrier(3)
        responses = []

        def worker(command):
            barrier.wait()
            responses.append(transport.send(command, timeout=0.2))

        threads = [
            threading.Thread(target=worker, args=("AT+ONE",)),
            threading.Thread(target=worker, args=("AT+TWO",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertFalse(state["concurrent"])
        self.assertEqual(len(responses), 2)

    def test_closed_transport_rejects_operations(self):
        transport, _, _ = self.make_transport()
        transport.close()
        transport.close()

        with self.assertRaises(dji4g_at.AtClosedError):
            transport.send("AT")
        with self.assertRaises(dji4g_at.AtClosedError):
            transport.read_unsolicited()


class LockTests(unittest.TestCase):
    def test_lock_is_nonblocking_and_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "private" / "at.lock"
            first = dji4g_at.InterProcessLock(lock_path)
            second = dji4g_at.InterProcessLock(lock_path)
            self.addCleanup(first.close)
            self.addCleanup(second.close)

            first.acquire()

            self.assertEqual(stat.S_IMODE(lock_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            with self.assertRaises(dji4g_at.AtBusyError):
                second.acquire()

            first.close()
            second.acquire()
            self.assertTrue(second.acquired)

    def test_transport_close_releases_its_process_lock(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "private" / "at.lock"
            first = dji4g_at.AtTransport(
                out_endpoint=FakeOutEndpoint(),
                in_endpoint=FakeInEndpoint(),
                lock_path=lock_path,
            )
            self.addCleanup(first.close)

            with self.assertRaises(dji4g_at.AtBusyError):
                dji4g_at.AtTransport(
                    out_endpoint=FakeOutEndpoint(),
                    in_endpoint=FakeInEndpoint(),
                    lock_path=lock_path,
                )

            first.close()
            second = dji4g_at.AtTransport(
                out_endpoint=FakeOutEndpoint(),
                in_endpoint=FakeInEndpoint(),
                lock_path=lock_path,
            )
            second.close()


class EndpointDiscoveryTests(unittest.TestCase):
    class UsbUtil:
        ENDPOINT_OUT = 0x00
        ENDPOINT_IN = 0x80
        ENDPOINT_TYPE_BULK = 0x02
        disposed = []

        @staticmethod
        def endpoint_direction(address):
            return address & 0x80

        @staticmethod
        def endpoint_type(attributes):
            return attributes & 0x03

        @classmethod
        def dispose_resources(cls, device):
            cls.disposed.append(device)

    class Endpoint:
        def __init__(self, address, attributes, reader=None):
            self.bEndpointAddress = address
            self.bmAttributes = attributes
            self.reader = reader
            self.writes = []

        def write(self, data, timeout):
            raw = bytes(data)
            self.writes.append(raw)
            self.reader.chunks.append(b"OK\r\n" if self.bEndpointAddress == 0x03 else b"ERROR\r\n")
            return len(raw)

        def read(self, size, timeout):
            return self.reader.read(size, timeout)

    class Device:
        def __init__(self, configurations):
            self.configurations = [
                [interface if isinstance(interface, FakeUsbInterface)
                 else FakeUsbInterface(interface, number)
                 for number, interface in enumerate(configuration)]
                for configuration in configurations
            ]
            self.kernel_interfaces = set()
            self.kernel_checks = []

        def is_kernel_driver_active(self, number):
            self.kernel_checks.append(number)
            return number in self.kernel_interfaces

        def __iter__(self):
            return iter(self.configurations)

        def get_active_configuration(self):
            return self.configurations[0]

    class UsbCore:
        def __init__(self, device):
            self.device = device
            self.find_calls = []

        def find(self, **kwargs):
            self.find_calls.append(kwargs)
            return self.device

    def test_discovery_probes_only_bulk_pairs_and_selects_responsive_pair(self):
        self.UsbUtil.disposed.clear()
        bad_in = FakeInEndpoint()
        good_in = FakeInEndpoint()
        interrupt_in = FakeInEndpoint()
        bad_out = self.Endpoint(0x01, 0x02, bad_in)
        good_out = self.Endpoint(0x03, 0x02, good_in)
        non_bulk_out = self.Endpoint(0x05, 0x03, interrupt_in)
        device = self.Device(
            [
                [
                    [bad_out, self.Endpoint(0x81, 0x02, bad_in)],
                    [non_bulk_out, self.Endpoint(0x85, 0x03, interrupt_in)],
                    [good_out, self.Endpoint(0x83, 0x02, good_in)],
                ]
            ]
        )

        usb_core = self.UsbCore(device)
        transport = dji4g_at.AtTransport(
            usb_core_module=usb_core,
            usb_util_module=self.UsbUtil,
            use_lock=False,
            probe_timeout=0.1,
        )

        self.assertEqual(
            usb_core.find_calls,
            [{"idVendor": dji4g_at.VID, "idProduct": dji4g_at.PID}],
        )
        self.assertIs(transport.out_endpoint, good_out)
        self.assertEqual(non_bulk_out.writes, [])
        transport.close()
        self.assertEqual(self.UsbUtil.disposed, [device])

    @patch("dji4g_at.sys.platform", "linux")
    def test_linux_skips_network_and_kernel_owned_interfaces(self):
        reader = FakeInEndpoint()
        network_out = self.Endpoint(0x03, 0x02, reader)
        owned_out = self.Endpoint(0x03, 0x02, reader)
        good_out = self.Endpoint(0x03, 0x02, reader)
        device = self.Device([[
            FakeUsbInterface([network_out, self.Endpoint(0x83, 0x02, reader)], 0, 0x0A),
            FakeUsbInterface([owned_out, self.Endpoint(0x83, 0x02, reader)], 1),
            FakeUsbInterface([good_out, self.Endpoint(0x83, 0x02, reader)], 2),
        ]])
        device.kernel_interfaces.add(1)
        with dji4g_at.AtTransport(
            device=device, usb_core_module=self.UsbCore(device),
            usb_util_module=self.UsbUtil, use_lock=False,
        ) as transport:
            self.assertIs(transport.out_endpoint, good_out)
        self.assertEqual(network_out.writes, [])
        self.assertEqual(owned_out.writes, [])
        self.assertEqual(device.kernel_checks, [1, 2])

    @patch("dji4g_at.sys.platform", "linux")
    def test_linux_reports_kernel_driver_ownership(self):
        device = self.Device([[FakeUsbInterface([], 0)]])
        device.kernel_interfaces.add(0)
        with self.assertRaisesRegex(dji4g_at.AtError, "kernel driver; not detached"):
            dji4g_at.AtTransport(
                device=device, usb_core_module=self.UsbCore(device),
                usb_util_module=self.UsbUtil, use_lock=False,
            )

    @patch("dji4g_at.sys.platform", "darwin")
    def test_macos_does_not_query_linux_kernel_driver(self):
        reader = FakeInEndpoint()
        device = self.Device([[[
            self.Endpoint(0x03, 0x02, reader), self.Endpoint(0x83, 0x02, reader),
        ]]])
        with dji4g_at.AtTransport(
            device=device, usb_core_module=self.UsbCore(device),
            usb_util_module=self.UsbUtil, use_lock=False,
        ):
            pass
        self.assertEqual(device.kernel_checks, [])

    def test_discovery_errors_are_wrapped(self):
        with patch.object(self.UsbCore, "find", side_effect=FakeUsbFailure("no backend")):
            with self.assertRaisesRegex(dji4g_at.AtIoError, "check libusb"):
                dji4g_at.AtTransport(
                    usb_core_module=self.UsbCore(None),
                    usb_util_module=self.UsbUtil, use_lock=False,
                )

    def test_direct_endpoint_injection_requires_a_complete_pair(self):
        with self.assertRaises(ValueError):
            dji4g_at.AtTransport(out_endpoint=FakeOutEndpoint(), use_lock=False)

    def test_initialization_failure_releases_process_lock(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "private" / "at.lock"
            missing_core = self.UsbCore(None)

            with self.assertRaises(dji4g_at.AtError):
                dji4g_at.AtTransport(
                    usb_core_module=missing_core,
                    usb_util_module=self.UsbUtil,
                    lock_path=lock_path,
                )

            lock = dji4g_at.InterProcessLock(lock_path)
            lock.acquire()
            lock.close()


if __name__ == "__main__":
    unittest.main()
