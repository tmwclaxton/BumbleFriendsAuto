"""Wireless ADB helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.adb_wireless import line_is_online, wireless_endpoint


class LineOnlineTests(unittest.TestCase):
    def test_usb_pixel(self):
        line = "29081FDH200GZ8         device usb:1-9 product:panther"
        self.assertTrue(line_is_online(line, "29081FDH200GZ8"))

    def test_wireless_ip(self):
        line = "192.168.0.168:49414    device product:heroltexx"
        self.assertTrue(line_is_online(line, "192.168.0.168:49414"))
        self.assertFalse(line_is_online(line, "ce01171185a334920c"))

    def test_mdns_has_serial(self):
        line = "adb-ce01171185a334920c-WQda3P._adb-tls-connect._tcp device product:heroltexx"
        self.assertTrue(line_is_online(line, "ce01171185a334920c"))

    def test_offline(self):
        line = "192.168.0.168:49414    offline"
        self.assertFalse(line_is_online(line, "192.168.0.168:49414"))


class EndpointTests(unittest.TestCase):
    def test_config_adb(self):
        self.assertEqual(
            wireless_endpoint({"adb": "192.168.0.168:49414"}),
            "192.168.0.168:49414",
        )

    def test_env_wins(self):
        with patch.dict(os.environ, {"ARCHIE_ADB": "10.0.0.2:1111"}):
            self.assertEqual(wireless_endpoint({"adb": "192.168.0.168:1"}), "10.0.0.2:1111")


if __name__ == "__main__":
    unittest.main()
