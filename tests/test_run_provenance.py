"""WP3 tests 30-33: provenance (git SHA, engine_version_actual, pinned images)
must be verified against an actual downloaded artifact, not HCL text -- a
regex over the jsonencode() block would pass even if the real S3 object
never carries these fields. See PLAN_2.md WP3.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)

GIT_SHA_SCRIPT = ROOT / "scripts" / "git_sha.sh"

# A stand-in for what node_details.tf writes to cluster_details.json after
# WP0 + WP3 -- rich, with the new provenance fields populated.
RICH_CLUSTER_DETAILS_WITH_PROVENANCE = {
    "run": {
        "cluster_id": "elasticache-perf-test-valkey-20260810-al",
        "git_sha": "abc123def456",
        "git_dirty": "false",
        "terraform_workspace": "default",
    },
    "elasticache": {
        "engine": "valkey",
        "engine_version_configured": "7.1",
        "engine_version_actual": "7.1.0",
        "node_type": "cache.t4g.micro",
    },
    "ecs": {
        "loadgen_image": "redislabs/memtier_benchmark@sha256:5f15b74f657fd30ee73453af9caa1781de1614f4d934d46feee711dc19b758af",
    },
    "reporter": {
        "image": "python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534",
    },
}


class ClusterDetailsProvenanceFieldsTests(unittest.TestCase):
    """Test 30: assert against a downloaded-artifact-shaped fixture."""

    def test_config_from_cluster_details_surfaces_git_sha_and_engine_version_actual(self):
        try:
            from report_generator import _config_from_cluster_details
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        config = _config_from_cluster_details(RICH_CLUSTER_DETAILS_WITH_PROVENANCE)

        self.assertEqual(config["git_sha"], "abc123def456")
        self.assertEqual(config["engine_version_actual"], "7.1.0")
        self.assertEqual(config["loadgen_image"], RICH_CLUSTER_DETAILS_WITH_PROVENANCE["ecs"]["loadgen_image"])


class EnrichSummaryMetaProvenanceTests(unittest.TestCase):
    def test_engine_version_actual_reaches_meta_without_overwriting_engine_version(self):
        try:
            from report_common import enrich_summary_meta
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        summary = {"meta": {"engine_version": "7.1"}}

        enrich_summary_meta(summary, RICH_CLUSTER_DETAILS_WITH_PROVENANCE)

        self.assertEqual(summary["meta"]["engine_version"], "7.1")
        self.assertEqual(summary["meta"]["engine_version_actual"], "7.1.0")
        self.assertEqual(summary["meta"]["git_sha"], "abc123def456")

    def test_old_cluster_details_without_new_keys_does_not_raise_and_leaves_fields_absent(self):
        try:
            from report_common import enrich_summary_meta
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        old_style_cluster_details = {
            "run": {"cluster_id": "cluster-a"},
            "elasticache": {"engine": "valkey", "node_type": "cache.t4g.micro"},
        }
        summary = {"meta": {}}

        enrich_summary_meta(summary, old_style_cluster_details)  # must not raise

        self.assertEqual(summary["meta"].get("engine_version_actual"), "")
        self.assertEqual(summary["meta"].get("git_sha"), "")


class GitShaScriptTests(unittest.TestCase):
    def test_git_sha_is_not_unknown_inside_this_repo(self):
        """The repo running this test suite is itself a git checkout, so a
        normal `apply` from it must resolve a real SHA -- "unknown" here
        would mean the script's repo-detection regressed.
        """
        completed = subprocess.run(
            ["bash", str(GIT_SHA_SCRIPT)],
            input="{}",
            cwd=str(ROOT),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertNotEqual(result["sha"], "unknown")
        self.assertRegex(result["sha"], r"^[0-9a-f]{40}$")
        self.assertIn(result["dirty"], ("true", "false"))

    def test_missing_git_repo_reports_unknown_and_still_exits_zero(self):
        """git_sha.sh must never fail terraform apply/destroy (D-none;
        modeled on fetch_elasticache_price.sh's exit-0 contract) -- run it
        from a directory with no .git and no git binary on PATH.
        """
        import tempfile

        bash_path = shutil.which("bash")
        self.assertIsNotNone(bash_path, "bash not found; cannot exercise git_sha.sh")

        with tempfile.TemporaryDirectory() as empty_dir, tempfile.TemporaryDirectory() as fake_bin:
            # bash itself is invoked by absolute path; PATH is emptied only
            # so `command -v git` (inside the script) fails regardless of
            # whether the host actually has git installed.
            env = os.environ.copy()
            env["PATH"] = fake_bin
            completed = subprocess.run(
                [bash_path, str(GIT_SHA_SCRIPT)],
                input="{}",
                cwd=empty_dir,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result, {"sha": "unknown", "dirty": "unknown"})


if __name__ == "__main__":
    unittest.main()
