"""CRM pipeline client."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.crm import create_lead


class CrmSettingsTests(unittest.TestCase):
    def test_missing_token(self):
        env = {"LGS_PIPELINE_TOKEN": "", "LGS_API_URL": "http://127.0.0.1:8099"}
        with patch.dict("os.environ", env, clear=False):
            with patch("src.crm.load_config", return_value={"lgs": {"api_url": "", "pipeline_token": ""}}):
                result = create_lead({"name": "Sam", "phone": "07123456789"})
        self.assertFalse(result["ok"])
        self.assertIn("LGS_PIPELINE_TOKEN", result["error"])


if __name__ == "__main__":
    unittest.main()
