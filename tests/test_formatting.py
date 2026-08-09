import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


class FormattingTests(unittest.TestCase):
    def test_format_usd_hour_trims_trailing_zeros_without_losing_the_leading_digit(self):
        from formatting import format_usd_hour

        self.assertEqual(format_usd_hour(0), "$0")
        self.assertEqual(format_usd_hour(0.158), "$0.158")
        self.assertEqual(format_usd_hour(100), "$100")
        self.assertEqual(format_usd_hour(1000), "$1,000")
        self.assertIsNone(format_usd_hour(None))
        self.assertIsNone(format_usd_hour("not-a-number"))

    def test_format_gib_trims_trailing_zeros_without_losing_the_leading_digit(self):
        from formatting import format_gib

        self.assertEqual(format_gib(0), "0")
        self.assertEqual(format_gib(6850472837), "6.38")
        self.assertIsNone(format_gib(None))
        self.assertIsNone(format_gib("not-a-number"))

    def test_module_is_importable_without_pandas(self):
        """cards.header_pills() must stay usable without pandas installed
        (see cards.py); it depends on this module, so this module must never
        gain a pandas import, transitive or otherwise.
        """
        module_path = ROOT / "reporter" / "formatting.py"
        import_lines = [
            line.strip() for line in module_path.read_text(encoding="utf-8").splitlines()
            if line.startswith(("import ", "from "))
        ]
        self.assertEqual(import_lines, [])


if __name__ == "__main__":
    unittest.main()
