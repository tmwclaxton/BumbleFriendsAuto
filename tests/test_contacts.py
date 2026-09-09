"""Pixel contact display names always end with LGS."""

from __future__ import annotations

import unittest

from src.contacts import with_lgs_suffix


class LgsSuffixTests(unittest.TestCase):
    def test_appends(self):
        self.assertEqual(with_lgs_suffix("Krishna"), "Krishna LGS")

    def test_keeps_disambiguator(self):
        self.assertEqual(with_lgs_suffix("Dan (London)"), "Dan (London) LGS")

    def test_no_double(self):
        self.assertEqual(with_lgs_suffix("Pete LGS"), "Pete LGS")
        self.assertEqual(with_lgs_suffix("Pete LGS LGS"), "Pete LGS")

    def test_empty(self):
        self.assertEqual(with_lgs_suffix("  "), "")
