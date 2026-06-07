import json
import os
import shlex
import subprocess
import tempfile
import textwrap
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

    def test_latest_does_not_download_previous_ready_run_when_current_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            terraform = bin_dir / "terraform"
            aws = bin_dir / "aws"

            terraform.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    if [[ "$1" == "output" && "$2" == "-json" ]]; then
                        cat <<'JSON'
                    {
                      "aws_region": {"value": "us-east-1"},
                      "metrics_export_location": {"value": "s3://bucket/exports/"},
                      "elasticache_cluster_id": {"value": "cluster-current"},
                      "loadgen_cluster_name": {"value": "ecs-cluster"},
                      "loadgen_service_name": {"value": "loadgen-service"},
                      "loadgen_log_group_name": {"value": "/aws/ecs/current"},
                      "run_folder": {"value": "20260525-052629"}
                    }
                    JSON
                    else
                        exit 2
                    fi
                    """
                ),
                encoding="utf-8",
            )
            aws.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    if [[ "$1" == "s3" && "$2" == "ls" ]]; then
                        cat <<'LISTING'
                    2026-05-25 05:30:00          1 exports/20260525-052629/report_status.json
                    2026-05-25 04:00:00          1 exports/20260525-030112/report_status.json
                    2026-05-25 04:01:00          1 exports/20260525-030112/results_20260525-030112.html
                    2026-05-25 04:01:00          1 exports/20260525-030112/results_20260525-030112.json
                    LISTING
                    elif [[ "$1" == "s3" && "$2" == "cp" ]]; then
                        case "$3" in
                            s3://bucket/exports/20260525-052629/report_status.json)
                                printf '{"complete": true}\\n'
                                ;;
                            s3://bucket/exports/20260525-030112/report_status.json)
                                printf '{"complete": true, "report": "s3://bucket/exports/20260525-030112/results_20260525-030112.html", "summary": "s3://bucket/exports/20260525-030112/results_20260525-030112.json"}\\n'
                                ;;
                            *)
                                exit 2
                                ;;
                        esac
                    elif [[ "$1" == "elasticache" && "$2" == "describe-replication-groups" ]]; then
                        printf 'available\\n'
                    elif [[ "$1" == "ecs" && "$2" == "describe-services" ]]; then
                        printf '{"desiredCount":0,"runningCount":0,"pendingCount":0}\\n'
                    elif [[ "$1" == "ecs" && "$2" == "list-tasks" ]]; then
                        printf '["arn:aws:ecs:region:account:task/cluster/reporter-task"]\\n'
                    elif [[ "$1" == "ecs" && "$2" == "describe-tasks" ]]; then
                        printf 'RUNNING\\n'
                    else
                        exit 2
                    fi
                    """
                ),
                encoding="utf-8",
            )
            terraform.chmod(0o755)
            aws.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "download_results.sh"), "--latest"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        output = completed.stdout + completed.stderr
        self.assertIn("Current Terraform run 20260525-052629 is not ready: report running.", output)
        self.assertIn(
            "Refusing to download older results while current Terraform run 20260525-052629 is not ready.",
            output,
        )
        self.assertNotIn("Latest completed run is 20260525-030112", output)
        self.assertNotIn("=== Downloading ===", output)

    def test_latest_runs_downloads_missing_selected_runs_by_s3_recency(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            output_dir = tmp_path / "results"
            bin_dir.mkdir()
            output_dir.mkdir()
            (output_dir / "20260525-052629").mkdir()
            (output_dir / "manually-named-old-run").mkdir()

            terraform = bin_dir / "terraform"
            aws = bin_dir / "aws"

            terraform.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    if [[ "$1" == "output" && "$2" == "-json" ]]; then
                        cat <<'JSON'
                    {
                      "aws_region": {"value": "us-east-1"},
                      "metrics_export_location": {"value": "s3://bucket/exports/"},
                      "elasticache_cluster_id": {"value": "cluster-current"},
                      "loadgen_cluster_name": {"value": "ecs-cluster"},
                      "loadgen_service_name": {"value": "loadgen-service"},
                      "loadgen_log_group_name": {"value": "/aws/ecs/current"},
                      "run_folder": {"value": "20260525-052629"}
                    }
                    JSON
                    else
                        exit 2
                    fi
                    """
                ),
                encoding="utf-8",
            )
            aws.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    if [[ "$1" == "s3" && "$2" == "ls" ]]; then
                        cat <<'LISTING'
                    2026-05-25 06:30:00          1 exports/20260525-052629/report_status.json
                    2026-05-25 06:25:00          1 exports/20260524-180000/results.html
                    2026-05-25 06:20:00          1 exports/20260523-120000/results.html
                    LISTING
                    elif [[ "$1" == "s3" && "$2" == "cp" ]]; then
                        mkdir -p "$(dirname "$4")"
                        printf 'downloaded %s\\n' "$3" > "$4"
                    else
                        exit 2
                    fi
                    """
                ),
                encoding="utf-8",
            )
            terraform.chmod(0o755)
            aws.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            completed = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "download_results.sh"),
                    "--latest-runs",
                    "2",
                    "--output-dir",
                    str(output_dir),
                    "--parallel",
                    "1",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            output = completed.stdout + completed.stderr
            self.assertIn("SKIP  20260525-052629 already exists locally.", output)
            self.assertIn("ADD   20260524-180000", output)
            self.assertTrue((output_dir / "20260524-180000" / "results.html").exists())
            self.assertFalse((output_dir / "20260523-120000").exists())


if __name__ == "__main__":
    unittest.main()
