import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TerraformRunIdContractTests(unittest.TestCase):
    def test_run_id_full_uses_24_hour_timestamp(self):
        main_tf = (ROOT / "main.tf").read_text(encoding="utf-8")
        match = re.search(r'run_id_full\s*=\s*formatdate\("([^"]+)"', main_tf)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "YYYYMMDDHHmmss")


if __name__ == "__main__":
    unittest.main()
