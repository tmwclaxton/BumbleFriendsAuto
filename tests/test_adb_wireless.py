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


class OnlineTests(unittest.TestCase):
    def test_pixel_usb_online(self):
        from src.phones import phone_is_online

        blob = "List of devices attached\n29081FDH200GZ8         device usb:1-9\n"
        with patch("src.adb_wireless.devices_text", return_value=blob):
            self.assertTrue(
                phone_is_online({"id": "toby", "device": "pixel", "serial": "29081FDH200GZ8"})
            )
            self.assertFalse(
                phone_is_online(
                    {"id": "archie", "device": "galaxy", "adb": "192.168.0.168:5555"}
                )
            )

    def test_galaxy_wireless_online(self):
        from src.phones import phone_is_online

        blob = "192.168.0.168:5555     device product:heroltexx\n"
        with patch("src.adb_wireless.devices_text", return_value=blob):
            self.assertTrue(
                phone_is_online(
                    {"id": "archie", "device": "galaxy", "serial": "ce0117", "adb": "192.168.0.168:5555"}
                )
            )

    def test_public_phones_ready_from_env_adb(self):
        from src.phones import public_phones

        rows = [
            {"id": "toby", "label": "Toby", "device": "pixel", "serial": "29081FDH200GZ8"},
            {"id": "archie", "label": "Archie", "device": "galaxy", "serial": "", "adb": "192.168.0.168:5555"},
        ]
        blob = "List of devices attached\n29081FDH200GZ8         device usb:1-9\n"
        with patch("src.phones.list_phones", return_value=rows):
            with patch("src.adb_wireless.devices_text", return_value=blob):
                phones = {p["id"]: p for p in public_phones()}
        self.assertTrue(phones["archie"]["ready"])
        self.assertFalse(phones["archie"]["online"])
        self.assertTrue(phones["toby"]["online"])


class EndpointTests(unittest.TestCase):
    def test_config_adb(self):
        self.assertEqual(
            wireless_endpoint({"adb": "192.168.0.168:49414"}),
            "192.168.0.168:49414",
        )

    def test_env_wins(self):
        with patch.dict(os.environ, {"ARCHIE_ADB": "10.0.0.2:1111"}):
            self.assertEqual(
                wireless_endpoint({"id": "archie", "adb": "192.168.0.168:1"}),
                "10.0.0.2:1111",
            )

    def test_env_does_not_steal_pixel(self):
        with patch.dict(os.environ, {"ARCHIE_ADB": "10.0.0.2:1111"}):
            self.assertEqual(
                wireless_endpoint({"id": "toby", "device": "pixel", "serial": "29081FDH200GZ8"}),
                "",
            )


if __name__ == "__main__":
    unittest.main()
