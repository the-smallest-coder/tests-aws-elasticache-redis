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

    def test_boto3_pin_matches_between_reporter_tf_and_requirements_txt(self):
        reporter_tf = (ROOT / "reporter.tf").read_text(encoding="utf-8")
        requirements_txt = (ROOT / "reporter" / "requirements.txt").read_text(encoding="utf-8")

        tf_match = re.search(r'pip install[^\n]*"boto3==([^"]+)"', reporter_tf)
        req_match = re.search(r'^boto3==(\S+)', requirements_txt, re.MULTILINE)

        self.assertIsNotNone(tf_match, "exact boto3 pin not found in reporter.tf")
        self.assertIsNotNone(req_match, "exact boto3 pin not found in reporter/requirements.txt")
        self.assertEqual(
            tf_match.group(1),
            req_match.group(1),
            "boto3 version pin drifted between reporter.tf and reporter/requirements.txt",
        )

    def test_pandas_pin_matches_between_reporter_tf_and_requirements_txt(self):
        reporter_tf = (ROOT / "reporter.tf").read_text(encoding="utf-8")
        requirements_txt = (ROOT / "reporter" / "requirements.txt").read_text(encoding="utf-8")

        tf_match = re.search(r'pip install[^\n]*"pandas==([^"]+)"', reporter_tf)
        req_match = re.search(r'^pandas==(\S+)', requirements_txt, re.MULTILINE)

        self.assertIsNotNone(tf_match, "exact pandas pin not found in reporter.tf")
        self.assertIsNotNone(req_match, "exact pandas pin not found in reporter/requirements.txt")
        self.assertEqual(
            tf_match.group(1),
            req_match.group(1),
            "pandas version pin drifted between reporter.tf and reporter/requirements.txt",
        )

    def test_reporter_base_image_digest_matches_between_variables_tf_and_dockerfile(self):
        """reporter.tf's ECS task uses var.reporter_image (variables.tf), not
        the Dockerfile FROM line -- the Dockerfile is for the optional
        pre-built-image path mentioned in reporter.tf's own comment. Both
        should still name the same base image so that path isn't stale.
        """
        variables_tf = (ROOT / "variables.tf").read_text(encoding="utf-8")
        dockerfile = (ROOT / "reporter" / "Dockerfile").read_text(encoding="utf-8")

        var_match = re.search(
            r'variable "reporter_image".*?default\s*=\s*"python@(sha256:[0-9a-f]+)"',
            variables_tf,
            re.DOTALL,
        )
        dockerfile_match = re.search(r'^FROM python@(sha256:[0-9a-f]+)', dockerfile, re.MULTILINE)

        self.assertIsNotNone(var_match, "digest-pinned reporter_image default not found in variables.tf")
        self.assertIsNotNone(dockerfile_match, "digest-pinned FROM line not found in reporter/Dockerfile")
        self.assertEqual(
            var_match.group(1),
            dockerfile_match.group(1),
            "reporter base image digest drifted between variables.tf and reporter/Dockerfile",
        )


if __name__ == "__main__":
    unittest.main()
