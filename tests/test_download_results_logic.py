import json
import shlex
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "download_results_lib.sh"


def bash(command: str, stdin: str = "") -> str:
    completed = subprocess.run(
        ["bash", "-c", f"source {LIB!s}; {command}"],
        input=stdin,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


class DownloadResultsLogicTests(unittest.TestCase):
    def test_current_run_uses_run_folder_output_directly(self):
        tf_output = json.dumps(
            {
                "run_folder": {"value": "20260518-213000"},
                "run_timestamp": {"value": "20260518093000"},
            }
        )

        self.assertEqual(
            bash(f"_current_run_from_tf_output {shlex.quote(tf_output)}"),
            "20260518-213000",
        )

    def test_current_run_falls_back_to_legacy_run_timestamp(self):
        tf_output = json.dumps({"run_timestamp": {"value": "20260518093000"}})

        self.assertEqual(
            bash(f"_current_run_from_tf_output {shlex.quote(tf_output)}"),
            "20260518-093000",
        )

    def test_runs_are_ordered_by_latest_object_upload(self):
        listing = "\n".join(
            [
                "2026-05-18 10:00:00 1 exports/20260518-090000/cluster_details.json",
                "2026-05-18 10:05:00 1 exports/20260517-090000/results.html",
                "2026-05-18 10:03:00 1 exports/20260518-090000/report_status.json",
            ]
        )

        self.assertEqual(
            bash("_run_timestamps_by_recency", listing).splitlines(),
            ["20260517-090000", "20260518-090000"],
        )

    def test_ready_requires_complete_status_outputs_and_existing_objects(self):
        status = json.dumps(
            {
                "complete": True,
                "report": "s3://bucket/exports/20260518-090000/results.html",
                "summary": "s3://bucket/exports/20260518-090000/results.json",
            }
        )
        keys = "\n".join(
            [
                "exports/20260518-090000/report_status.json",
                "exports/20260518-090000/results.html",
                "exports/20260518-090000/results.json",
            ]
        )

        self.assertEqual(
            bash(
                f"_report_status_ready {shlex.quote(status)} {shlex.quote(keys)} && printf ready"
            ),
            "ready",
        )

    def test_ready_rejects_legacy_complete_status_without_report_fields(self):
        status = json.dumps({"complete": True})
        keys = "exports/20260518-090000/report_status.json"

        self.assertEqual(
            bash(
                f"_report_status_ready {shlex.quote(status)} {shlex.quote(keys)} || printf not-ready"
            ),
            "not-ready",
        )

    def test_ready_rejects_missing_referenced_object(self):
        status = json.dumps(
            {
                "complete": True,
                "report": "s3://bucket/exports/20260518-090000/results.html",
                "summary": "s3://bucket/exports/20260518-090000/results.json",
            }
        )
        keys = "\n".join(
            [
                "exports/20260518-090000/report_status.json",
                "exports/20260518-090000/results.html",
            ]
        )

        self.assertEqual(
            bash(
                f"_report_status_ready {shlex.quote(status)} {shlex.quote(keys)} || printf not-ready"
            ),
            "not-ready",
        )

    def test_current_run_phases_cover_required_states(self):
        cases = [
            ("starting", '_classify_current_run creating 1 0 1 0 "" false false false'),
            ("running", '_classify_current_run available 1 1 0 0 "" false false false'),
            ("stopping/cleanup", '_classify_current_run deleting 0 0 0 0 "" false false false'),
            ("report not started", '_classify_current_run "" 0 0 0 0 "" false false false'),
            ("report running", '_classify_current_run available 0 0 0 1 RUNNING false false false'),
            ("report running", '_classify_current_run available 0 0 0 1 RUNNING true false false'),
            ("export failed", '_classify_current_run available 0 0 0 1 STOPPED true false false'),
            ("report failed", '_classify_current_run available 0 0 0 1 STOPPED true true false'),
            ("known fatal reporter error: boom", '_classify_current_run available 0 0 0 1 STOPPED true true false boom'),
        ]

        for expected, command in cases:
            with self.subTest(expected=expected):
                self.assertEqual(bash(command), expected)


if __name__ == "__main__":
    unittest.main()
