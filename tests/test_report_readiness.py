import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class ReportReadinessTests(unittest.TestCase):
    def test_inspect_flags_legacy_and_missing_contract_files(self):
        from report_common import inspect_run_directory

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260227-140039"
            run_dir.mkdir()
            _write_json(
                run_dir / "results_local.json",
                {
                    "meta": {"cluster_id": "legacy-run"},
                    "benchmark": {"active_window_min": 31.2, "prefill_min": 29.3},
                    "cache_efficiency": {"first_eviction_offset_min": 0.0},
                },
            )

            inspection = inspect_run_directory(run_dir)

            warnings = "\n".join(inspection["warnings"])
            self.assertIn("Missing report_status.json.", warnings)
            self.assertIn("Missing canonical results_*.json.", warnings)
            self.assertIn("Missing memtier logs under logs/loadgen.", warnings)
            self.assertIn("Legacy relative fields detected:", warnings)
            self.assertFalse(inspection["local_ready"])

    def test_inspect_marks_local_ready_for_current_schema_with_logs(self):
        from report_common import GENERATOR_SCHEMA_VERSION, inspect_run_directory

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260520-114237"
            run_dir.mkdir()
            (run_dir / "logs" / "loadgen").mkdir(parents=True)
            (run_dir / "logs" / "loadgen" / "stream-a.txt").write_text("line\n", encoding="utf-8")

            _write_json(
                run_dir / "results_local.json",
                {
                    "meta": {
                        "cluster_id": "current-run",
                        "generator_schema_version": GENERATOR_SCHEMA_VERSION,
                        "source_mode": "local",
                        "memtier_window_source": "memtier_log_messages",
                        "artifact_source": "generated",
                        "report_start": "2026-05-20T11:42:37",
                        "report_end": "2026-05-20T12:42:37",
                    },
                    "benchmark": {},
                    "cache_efficiency": {},
                },
            )

            inspection = inspect_run_directory(run_dir)

            self.assertTrue(inspection["local_ready"])

    def test_load_run_prefers_canonical_current_schema_json(self):
        from report_common import GENERATOR_SCHEMA_VERSION, load_run

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260512-095522"
            run_dir.mkdir()
            _write_json(
                run_dir / "results_local.json",
                {
                    "meta": {"cluster_id": "local"},
                    "benchmark": {},
                },
            )
            _write_json(
                run_dir / "results_20260512-095522.json",
                {
                    "meta": {
                        "cluster_id": "canonical",
                        "generator_schema_version": GENERATOR_SCHEMA_VERSION,
                    },
                    "benchmark": {},
                },
            )

            run = load_run("baseline", str(run_dir))

            self.assertEqual(run.results_path.name, "results_20260512-095522.json")
            self.assertEqual(run.summary["meta"]["cluster_id"], "canonical")

    def test_load_run_warns_when_falling_back_to_legacy_local_json(self):
        from report_common import load_run

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260501-083934"
            run_dir.mkdir()
            _write_json(
                run_dir / "results_local.json",
                {
                    "meta": {"cluster_id": "legacy-only"},
                    "benchmark": {},
                },
            )

            run = load_run("candidate", str(run_dir))

            warning_text = "\n".join(run.warnings)
            self.assertIn("legacy/incomplete", warning_text)
            self.assertIn("Using results_local.json fallback", warning_text)


if __name__ == "__main__":
    unittest.main()
