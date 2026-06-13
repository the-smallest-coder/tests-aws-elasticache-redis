import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MultirunDocsTests(unittest.TestCase):
    def test_docs_avoid_rejected_designs(self):
        text = "\n".join(
            [
                (ROOT / "multirun" / "README.md").read_text(encoding="utf-8"),
                (ROOT / "multirun" / "AGENTS.md").read_text(encoding="utf-8"),
                (ROOT / "README.md").read_text(encoding="utf-8"),
            ]
        ).lower()

        forbidden = [
            "generated terraform wrapper",
            "module extraction",
            "root-level generated runs",
            "verify-state",
            "stage file",
            "stage-file",
            "output cache",
            "output-cache",
            "region-from-state",
        ]

        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
