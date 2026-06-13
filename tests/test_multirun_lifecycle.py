import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MULTIRUN = ROOT / "multirun" / "multirun.sh"


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "multirun" / "runs").mkdir(parents=True)
    (repo / "variables.tf").write_text(
        'variable "project_name" {}\nvariable "engine_type" {}\n',
        encoding="utf-8",
    )
    (repo / "terraform.tfvars").write_text(
        'project_name = "elasticache-perf-test"\nengine_type = "valkey"\n',
        encoding="utf-8",
    )
    (repo / "multirun" / "runs" / "a.tfvars").write_text(
        'run_id_discriminator = "aa"\n',
        encoding="utf-8",
    )
    return repo


def write_fake_terraform(bin_dir: Path) -> None:
    (bin_dir / "terraform").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            : "${TERRAFORM_LOG:?}"
            chdir=""
            if [[ "${1:-}" == -chdir=* ]]; then
                chdir="${1#-chdir=}"
                shift
            fi
            printf 'terraform chdir=%s args=%s\\n' "$chdir" "$*" >> "$TERRAFORM_LOG"
            cmd="${1:-}"
            shift || true
            mkdir -p "$chdir/.terraform"
            case "$cmd" in
                init)
                    [[ -d "$chdir/.tf-plugin-cache" ]] || {
                        echo "missing plugin cache" >&2
                        exit 30
                    }
                    ;;
                workspace)
                    sub="${1:-}"; name="${2:-}"
                    case "$sub" in
                        select)
                            if [[ "$name" == "default" ]]; then
                                printf default > "$chdir/.terraform/environment"
                                exit 0
                            fi
                            if [[ "${FAKE_SELECT_EXISTS:-0}" == 1 ]]; then
                                printf '%s' "$name" > "$chdir/.terraform/environment"
                                exit 0
                            fi
                            exit 1
                            ;;
                        new)
                            printf '%s' "$name" > "$chdir/.terraform/environment"
                            ;;
                        show)
                            cat "$chdir/.terraform/environment"
                            ;;
                        list)
                            printf '  default\\n'
                            if [[ "${FAKE_WORKSPACE_EXISTS:-0}" == 1 ]]; then
                                printf '* a\\n'
                            fi
                            ;;
                        delete)
                            ;;
                    esac
                    ;;
                state)
                    if [[ "${FAKE_STATE_NONEMPTY:-0}" == 1 ]]; then
                        printf 'aws_elasticache_replication_group.main\\n'
                    fi
                    ;;
                output)
                    cat <<'JSON'
            {
              "metrics_export_location": {"value": "s3://bucket/exports/"},
              "run_folder": {"value": "20260601-120000-aa"}
            }
            JSON
                    ;;
                apply|destroy)
                    if [[ "${FAKE_TERRAFORM_FAIL:-0}" == 1 ]]; then
                        exit 44
                    fi
                    ;;
                *)
                    exit 2
                    ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "terraform").chmod(0o755)


def write_fake_aws(bin_dir: Path) -> None:
    (bin_dir / "aws").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            : "${AWS_LOG:?}"
            printf 'aws args=%s\\n' "$*" >> "$AWS_LOG"
            if [[ "$*" == *"--region"* ]]; then
                echo "unexpected --region" >&2
                exit 66
            fi
            if [[ "$1" == "s3" && "$2" == "ls" ]]; then
                if [[ "${FAKE_AWS_LS_FAIL:-0}" == 1 ]]; then
                    exit 12
                fi
                if [[ "${FAKE_AWS_EMPTY:-0}" == 1 ]]; then
                    exit 0
                fi
                if [[ "${FAKE_AWS_BOOTSTRAP_ONLY:-0}" == 1 ]]; then
                    printf '2026-06-01 12:00:01          1 exports/20260601-120000-aa/cluster_details.json\\n'
                    exit 0
                fi
                if [[ "${FAKE_AWS_TWO_LISTED_ONE_COPIED:-0}" == 1 ]]; then
                    printf '2026-06-01 12:00:01          1 exports/20260601-120000-aa/report_status.json\\n'
                    printf '2026-06-01 12:00:02          1 exports/20260601-120000-aa/results_20260601-120000-aa.html\\n'
                    exit 0
                fi
                printf '2026-06-01 12:00:01          1 exports/20260601-120000-aa/results_20260601-120000-aa.html\\n'
            elif [[ "$1" == "s3" && "$2" == "cp" ]]; then
                if [[ "${FAKE_AWS_CP_FAIL:-0}" == 1 ]]; then
                    exit 13
                fi
                mkdir -p "$4"
                if [[ "${FAKE_AWS_TWO_LISTED_ONE_COPIED:-0}" == 1 ]]; then
                    printf ok > "$4/report_status.json"
                    exit 0
                fi
                printf ok > "$4/results_20260601-120000-aa.html"
            else
                exit 2
            fi
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "aws").chmod(0o755)


def run_multirun(repo: Path, bin_dir: Path, args: list[str], check: bool = True, extra=None):
    env = os.environ.copy()
    env["MULTIRUN_REPO_ROOT"] = str(repo)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TERRAFORM_LOG"] = str(repo / "terraform.log")
    env["AWS_LOG"] = str(repo / "aws.log")
    if extra:
        env.update(extra)
    return subprocess.run(
        ["bash", str(MULTIRUN), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


class MultirunLifecycleTests(unittest.TestCase):
    def test_apply_creates_cache_selects_workspace_and_passes_var_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)

            run_multirun(repo, bin_dir, ["apply", "a"])

            log = (repo / "terraform.log").read_text(encoding="utf-8")
            self.assertTrue((repo / ".tf-plugin-cache").is_dir())
            self.assertIn("args=init -input=false", log)
            self.assertIn("args=workspace new a", log)
            self.assertIn(
                "args=apply -input=false -auto-approve -var-file=multirun/runs/a.tfvars",
                log,
            )
            self.assertIn("args=workspace select default", log)

    def test_apply_refuses_non_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)

            result = run_multirun(
                repo,
                bin_dir,
                ["apply", "a"],
                check=False,
                extra={"FAKE_STATE_NONEMPTY": "1"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("destroy first", result.stderr)
            log = (repo / "terraform.log").read_text(encoding="utf-8")
            self.assertNotIn("args=apply ", log)

    def test_download_uses_exact_prefix_without_region_and_handles_empty_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)
            write_fake_aws(bin_dir)

            run_multirun(
                repo,
                bin_dir,
                ["download", "a"],
                extra={"FAKE_SELECT_EXISTS": "1"},
            )

            aws_log = (repo / "aws.log").read_text(encoding="utf-8")
            self.assertIn(
                "aws args=s3 ls s3://bucket/exports/20260601-120000-aa/ --recursive",
                aws_log,
            )
            self.assertIn(
                "aws args=s3 cp s3://bucket/exports/20260601-120000-aa/",
                aws_log,
            )
            self.assertNotIn("--region", aws_log)
            self.assertTrue(
                (repo / "results" / "20260601-120000-aa" / "results_20260601-120000-aa.html").exists()
            )

            empty_repo = make_repo(tmp_path / "empty")
            result = run_multirun(
                empty_repo,
                bin_dir,
                ["download", "a"],
                extra={"FAKE_AWS_EMPTY": "1", "FAKE_SELECT_EXISTS": "1"},
            )
            self.assertIn("no results yet", result.stdout)
            empty_log = (empty_repo / "aws.log").read_text(encoding="utf-8")
            self.assertNotIn("s3 cp", empty_log)

    def test_download_skips_bootstrap_only_prefix_without_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)
            write_fake_aws(bin_dir)

            result = run_multirun(
                repo,
                bin_dir,
                ["download", "a"],
                extra={"FAKE_SELECT_EXISTS": "1", "FAKE_AWS_BOOTSTRAP_ONLY": "1"},
            )

            self.assertIn("results not ready", result.stdout)
            aws_log = (repo / "aws.log").read_text(encoding="utf-8")
            self.assertNotIn("s3 cp", aws_log)
            self.assertFalse((repo / "results" / "20260601-120000-aa").exists())

    def test_download_fails_if_copy_downloads_fewer_files_than_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)
            write_fake_aws(bin_dir)

            result = run_multirun(
                repo,
                bin_dir,
                ["download", "a"],
                check=False,
                extra={
                    "FAKE_SELECT_EXISTS": "1",
                    "FAKE_AWS_TWO_LISTED_ONE_COPIED": "1",
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("downloaded 1 of 2 listed object", result.stdout)

    def test_download_does_not_create_missing_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)
            write_fake_aws(bin_dir)

            result = run_multirun(repo, bin_dir, ["download", "a"], check=False)

            self.assertNotEqual(result.returncode, 0)
            log = (repo / "terraform.log").read_text(encoding="utf-8")
            self.assertIn("args=workspace select a", log)
            self.assertNotIn("args=workspace new a", log)

    def test_download_listing_and_copy_failures_are_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)
            write_fake_aws(bin_dir)

            listing = run_multirun(
                repo,
                bin_dir,
                ["download", "a"],
                check=False,
                extra={"FAKE_AWS_LS_FAIL": "1", "FAKE_SELECT_EXISTS": "1"},
            )
            self.assertNotEqual(listing.returncode, 0)

            copy = run_multirun(
                repo,
                bin_dir,
                ["download", "a"],
                check=False,
                extra={"FAKE_AWS_CP_FAIL": "1", "FAKE_SELECT_EXISTS": "1"},
            )
            self.assertNotEqual(copy.returncode, 0)

    def test_destroy_deletes_workspace_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)

            run_multirun(
                repo,
                bin_dir,
                ["destroy", "a"],
                extra={"FAKE_SELECT_EXISTS": "1"},
            )
            log = (repo / "terraform.log").read_text(encoding="utf-8")
            self.assertIn(
                "args=destroy -input=false -auto-approve -var-file=multirun/runs/a.tfvars",
                log,
            )
            self.assertIn("args=workspace select default", log)
            self.assertIn("args=workspace delete a", log)
            self.assertFalse((repo / "multirun" / "runs" / "a.tfvars").exists())

            failed_repo = make_repo(tmp_path / "failed")
            failed = run_multirun(
                failed_repo,
                bin_dir,
                ["destroy", "a"],
                check=False,
                extra={"FAKE_SELECT_EXISTS": "1", "FAKE_TERRAFORM_FAIL": "1"},
            )
            self.assertNotEqual(failed.returncode, 0)
            failed_log = (failed_repo / "terraform.log").read_text(encoding="utf-8")
            self.assertNotIn("args=workspace delete a", failed_log)
            self.assertTrue((failed_repo / "multirun" / "runs" / "a.tfvars").exists())

    def test_destroy_all_batch_removes_run_configs_and_manifest_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            runs = repo / "multirun" / "runs"
            batches = repo / "multirun" / "batches"
            batches.mkdir()
            (runs / "b.tfvars").write_text(
                'run_id_discriminator = "ab"\n',
                encoding="utf-8",
            )
            (batches / "smoke.list").write_text("a\nb\n", encoding="utf-8")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)

            run_multirun(
                repo,
                bin_dir,
                ["destroy-all", "smoke"],
                extra={"FAKE_SELECT_EXISTS": "1"},
            )

            self.assertFalse((runs / "a.tfvars").exists())
            self.assertFalse((runs / "b.tfvars").exists())
            self.assertFalse((batches / "smoke.list").exists())

    def test_destroy_all_batch_keeps_configs_and_manifest_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            batches = repo / "multirun" / "batches"
            batches.mkdir()
            (batches / "smoke.list").write_text("a\n", encoding="utf-8")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)

            result = run_multirun(
                repo,
                bin_dir,
                ["destroy-all", "smoke"],
                check=False,
                extra={"FAKE_SELECT_EXISTS": "1", "FAKE_TERRAFORM_FAIL": "1"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((repo / "multirun" / "runs" / "a.tfvars").exists())
            self.assertTrue((batches / "smoke.list").exists())

    def test_summary_reports_not_initialized_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)

            result = run_multirun(repo, bin_dir, ["summary"])

            self.assertIn("a: not-initialized", result.stdout)
            self.assertIn("Terraform is not initialized", result.stdout)

    def test_apply_all_continues_after_run_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_repo(tmp_path)
            (repo / "multirun" / "runs" / "b.tfvars").write_text(
                'run_id_discriminator = "ab"\n',
                encoding="utf-8",
            )
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_terraform(bin_dir)

            result = run_multirun(
                repo,
                bin_dir,
                ["apply-all"],
                check=False,
                extra={"FAKE_TERRAFORM_FAIL": "1"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("apply summary: ok=0 failed=2", result.stdout)


if __name__ == "__main__":
    unittest.main()
