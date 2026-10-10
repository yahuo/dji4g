import unittest

import dji4g


class ParsingTests(unittest.TestCase):
    def test_parse_usbnet(self):
        self.assertEqual(dji4g.parse_usbnet('+QCFG: "usbnet",1\n\nOK'), 1)
        self.assertIsNone(dji4g.parse_usbnet("ERROR"))

    def test_parse_registration(self):
        self.assertEqual(dji4g.parse_registration("+CEREG: 0,1\nOK"), 1)
        self.assertEqual(dji4g.parse_registration("+CGREG: 0,5\nOK"), 5)
        self.assertIsNone(dji4g.parse_registration("ERROR"))

    def test_parse_hardware_port(self):
        output = """
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 00:00:00:00:00:00

Hardware Port: Baiwang
Device: en10
Ethernet Address: 11:22:33:44:55:66
"""
        self.assertEqual(dji4g.parse_hardware_port(output, "Baiwang"), "en10")
        self.assertIsNone(dji4g.parse_hardware_port(output, "Missing"))

    def test_parse_gateway(self):
        packet = "router (ip_mult): {192.168.225.1}\n"
        self.assertEqual(dji4g.parse_gateway(packet), "192.168.225.1")
        self.assertIsNone(dji4g.parse_gateway("router (ip_mult): {invalid}"))

    def test_parse_first_ipv4(self):
        self.assertEqual(
            dji4g.parse_first_ipv4("alias.example.\n172.67.74.152\n2606:4700::1\n"),
            "172.67.74.152",
        )
        self.assertIsNone(dji4g.parse_first_ipv4("alias.example.\n"))

    def test_response_ok_requires_complete_line(self):
        self.assertTrue(dji4g.response_ok("AT\nOK"))
        self.assertFalse(dji4g.response_ok("NOT OKAY"))


if __name__ == "__main__":
    unittest.main()
