import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReporterDependencyPinTests(unittest.TestCase):
    def test_plotly_pin_matches_between_reporter_tf_and_requirements_txt(self):
        """plotly is pinned exactly in two places -- the ECS task's runtime
        `pip install` in reporter.tf and reporter/requirements.txt -- because
        charts.py reads plotly's private subplot internals
        (fig._grid_ref, trace_kwargs, layout_keys). Both comments say "keep
        this in sync" but nothing enforced it; a drift would mean the ECS
        reporter silently runs a different plotly than the pin intends.
        """
        reporter_tf = (ROOT / "reporter.tf").read_text(encoding="utf-8")
        requirements_txt = (ROOT / "reporter" / "requirements.txt").read_text(encoding="utf-8")

        tf_match = re.search(r'pip install[^\n]*"plotly==([^"]+)"', reporter_tf)
        req_match = re.search(r'^plotly==(\S+)', requirements_txt, re.MULTILINE)

        self.assertIsNotNone(tf_match, "plotly pin not found in reporter.tf")
        self.assertIsNotNone(req_match, "plotly pin not found in reporter/requirements.txt")
        self.assertEqual(
            tf_match.group(1),
            req_match.group(1),
            "plotly version pin drifted between reporter.tf and reporter/requirements.txt",
        )


if __name__ == "__main__":
    unittest.main()
