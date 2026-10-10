import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dji4g
import dji4g_gateway
import dji4g_paths


class PlatformPathTests(unittest.TestCase):
    @patch("dji4g_paths.sys.platform", "darwin")
    @patch.dict("os.environ", {"XDG_DATA_HOME": "/ignored"})
    def test_macos_path_is_unchanged(self):
        self.assertEqual(
            dji4g_paths.default_data_dir(),
            Path.home() / "Library/Application Support/dji4g",
        )

    @patch("dji4g_paths.sys.platform", "linux")
    def test_linux_defaults_and_absolute_xdg_override(self):
        for configured, expected in (
            ("", Path.home() / ".local/share/dji4g"),
            ("relative", Path.home() / ".local/share/dji4g"),
            ("/tmp/private data", Path("/tmp/private data/dji4g")),
        ):
            with self.subTest(configured=configured):
                with patch.dict("os.environ", {"XDG_DATA_HOME": configured}):
                    self.assertEqual(dji4g_paths.default_data_dir(), expected)
                    self.assertEqual(dji4g_gateway.GatewayConfig().data_dir, expected)


class LinuxNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.net = self.root / "net"
        self.net.mkdir()
        self.add_interface("enx001122334455", "2ca3", "4006")
        self.add_interface("eth0", "1234", "5678")
        self.addresses = [{"addr_info": [
            {"family": "inet", "scope": "global", "local": "192.168.225.2"},
            {"family": "inet6", "scope": "link", "local": "fe80::1"},
        ]}]
        self.routes = [{"dst": "default", "gateway": "192.168.225.1"}]

    def add_interface(self, name, vendor, product):
        device = self.root / "devices" / name
        interface = device / "1-1:1.0"
        interface.mkdir(parents=True)
        (device / "idVendor").write_text(vendor)
        (device / "idProduct").write_text(product)
        entry = self.net / name
        entry.mkdir()
        (entry / "device").symlink_to(interface)

    def outputs(self):
        return [subprocess.CompletedProcess([], 0, json.dumps(value), "")
                for value in (self.addresses, self.routes)]

    def test_identifies_by_usb_ancestry_not_interface_name(self):
        with patch("dji4g._run", side_effect=self.outputs()) as run:
            network = dji4g.detect_linux_network(self.net)
        self.assertEqual(network, dji4g.HostNetwork(
            "enx001122334455", "192.168.225.2", "192.168.225.1"
        ))
        self.assertEqual(run.call_args_list[0].args[0], [
            "ip", "-j", "-4", "address", "show", "dev", network.interface,
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "ip", "-j", "-4", "route", "show", "table", "all", "dev", network.interface,
        ])

    def test_missing_or_multiple_devices_are_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(dji4g.Dji4gError, "found 0"):
            dji4g.detect_linux_network(empty)
        self.add_interface("usb0", "2ca3", "4006")
        with self.assertRaisesRegex(dji4g.Dji4gError, "found 2"):
            dji4g.detect_linux_network(self.net)

    def test_no_dhcp_address_is_rejected(self):
        self.addresses = []
        with patch("dji4g._run", side_effect=self.outputs()):
            with self.assertRaisesRegex(dji4g.Dji4gError, "check DHCP"):
                dji4g.detect_linux_network(self.net)

    def test_missing_or_ambiguous_default_gateway_is_rejected(self):
        for routes in ([], [
            {"dst": "default", "gateway": "192.168.225.1"},
            {"dst": "default", "gateway": "192.168.225.3"},
        ]):
            self.routes = routes
            with patch("dji4g._run", side_effect=self.outputs()):
                with self.assertRaisesRegex(dji4g.Dji4gError, "default gateway"):
                    dji4g.detect_linux_network(self.net)

    def test_ip_errors_are_reported(self):
        for result, message in (
            (subprocess.CompletedProcess([], 1, "", "device missing"), "device missing"),
            (subprocess.CompletedProcess([], 0, "not json", ""), "valid JSON"),
            (subprocess.CompletedProcess([], 0, "{}", ""), "JSON list"),
        ):
            with patch("dji4g._run", return_value=result):
                with self.assertRaisesRegex(dji4g.Dji4gError, message):
                    dji4g.detect_linux_network(self.net)

    def test_platform_dispatch(self):
        for platform, detector in (("linux", "detect_linux_network"),
                                   ("darwin", "detect_mac_network")):
            with patch("dji4g.sys.platform", platform), patch(f"dji4g.{detector}") as detect:
                self.assertIs(dji4g.detect_host_network(), detect.return_value)
        with patch("dji4g.sys.platform", "win32"):
            with self.assertRaisesRegex(dji4g.Dji4gError, "macOS and Linux"):
                dji4g.detect_host_network()

    def test_macos_network_detection_is_preserved(self):
        results = [subprocess.CompletedProcess([], 0, output, "") for output in (
            "Hardware Port: Baiwang\nDevice: en10\n",
            "192.168.225.2\n",
            "router (ip_mult): {192.168.225.1}\n",
        )]
        with patch("dji4g.sys.platform", "darwin"), patch("dji4g._run", side_effect=results):
            self.assertEqual(dji4g.detect_mac_network(), dji4g.HostNetwork(
                "en10", "192.168.225.2", "192.168.225.1"
            ))

    def test_network_probe_uses_platform_ping_and_bound_dns_https(self):
        network = dji4g.HostNetwork("usb0", "192.168.225.2", "192.168.225.1")
        for platform, source_flag in (("linux", "-I"), ("darwin", "-S")):
            results = [subprocess.CompletedProcess([], 0, output, "") for output in (
                "", "203.0.113.10\n", "HTTP=200 REMOTE=203.0.113.10\n",
            )]
            with (
                patch("dji4g.sys.platform", platform),
                patch("dji4g.AtSession", MagicMock()),
                patch("dji4g.wait_for_registration"),
                patch("dji4g.print_command"),
                patch("dji4g.detect_host_network", return_value=network),
                patch("dji4g._run", side_effect=results) as run,
                patch("builtins.print"),
            ):
                dji4g.command_network_test(argparse.Namespace(wait=1, url="https://example.com/"))
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], ["ping", "-c", "2", source_flag,
                                           network.local_ip, network.gateway])
            self.assertEqual(commands[1][2:5], ["-b", network.local_ip, "@" + network.gateway])
            self.assertIn("--interface", commands[2])
            self.assertIn(network.interface, commands[2])
            self.assertIn("example.com:443:203.0.113.10", commands[2])

    def test_linux_mode_is_explicit_ecm_alias(self):
        args = dji4g.build_parser().parse_args(["set-mode", "linux"])
        self.assertFalse(args.yes)
        self.assertEqual(dji4g.MODE_VALUES[args.mode], dji4g.MODE_VALUES["mac"])
        with patch("dji4g.AtSession") as session:
            with self.assertRaisesRegex(dji4g.Dji4gError, "persistently changes"):
                dji4g.command_set_mode(args)
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
