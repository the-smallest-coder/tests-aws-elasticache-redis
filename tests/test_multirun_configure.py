import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MULTIRUN = ROOT / "multirun" / "multirun.sh"


VARIABLES_TF = """\
variable "project_name" { default = "elasticache-perf-test" }
variable "engine_type" { default = "valkey" }
variable "engine_version" { default = "8.1" }
variable "node_type" { default = "cache.t4g.micro" }
variable "loadgen_task_count" { default = 1 }
variable "run_id_discriminator" { default = "" }
"""


TFVARS = """\
project_name = "elasticache-perf-test"
engine_type = "valkey" # comment is allowed
"""


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "variables.tf").write_text(VARIABLES_TF, encoding="utf-8")
    (repo / "terraform.tfvars").write_text(TFVARS, encoding="utf-8")
    return repo


def run_multirun(repo: Path, args: list[str], check: bool = True):
    env = os.environ.copy()
    env["MULTIRUN_REPO_ROOT"] = str(repo)
    return subprocess.run(
        ["bash", str(MULTIRUN), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


class MultirunConfigureTests(unittest.TestCase):
    def test_configure_writes_scalar_hcl_and_escapes_template_openers(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))

            run_multirun(
                repo,
                [
                    "configure",
                    "a",
                    "--var",
                    "node_type=cache.t3.micro",
                    "--var",
                    "engine_version=${bad}-%{also_bad}",
                ],
            )

            tfvars = (repo / "multirun" / "runs" / "a.tfvars").read_text(
                encoding="utf-8"
            )
            self.assertIn('node_type = "cache.t3.micro"', tfvars)
            self.assertIn('engine_version = "$${bad}-%%{also_bad}"', tfvars)
            self.assertIn('run_id_discriminator = "aa"', tfvars)

    def test_invalid_run_name_and_unknown_variable_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))

            bad_name = run_multirun(repo, ["configure", "default"], check=False)
            self.assertNotEqual(bad_name.returncode, 0)
            self.assertIn("default", bad_name.stderr)

            bad_var = run_multirun(
                repo, ["configure", "a", "--var", "unknown=value"], check=False
            )
            self.assertNotEqual(bad_var.returncode, 0)
            self.assertIn("unknown Terraform variable", bad_var.stderr)

    def test_discriminator_sequence_skips_existing_and_explicit_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))

            run_multirun(repo, ["configure", "first"])
            run_multirun(
                repo,
                [
                    "configure-batch",
                    "smoke",
                    "--run",
                    "explicit,run_id_discriminator=ab",
                    "--run",
                    "auto,node_type=cache.t4g.micro",
                ],
            )

            explicit = (repo / "multirun" / "runs" / "explicit.tfvars").read_text(
                encoding="utf-8"
            )
            auto = (repo / "multirun" / "runs" / "auto.tfvars").read_text(
                encoding="utf-8"
            )
            self.assertIn('run_id_discriminator = "ab"', explicit)
            self.assertIn('run_id_discriminator = "ac"', auto)

    def test_duplicate_discriminator_fails_before_writing_batch_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))

            result = run_multirun(
                repo,
                [
                    "configure-batch",
                    "bad",
                    "--run",
                    "a,run_id_discriminator=zz",
                    "--run",
                    "b,run_id_discriminator=zz",
                ],
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate run_id_discriminator", result.stderr)
            self.assertFalse((repo / "multirun" / "runs" / "a.tfvars").exists())
            self.assertFalse((repo / "multirun" / "batches" / "bad.list").exists())

    def test_batch_precedence_and_refuses_existing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))

            run_multirun(
                repo,
                [
                    "configure-batch",
                    "smoke",
                    "--var",
                    "loadgen_task_count=3",
                    "--run",
                    "a,node_type=cache.t3.micro",
                    "--run",
                    "b,node_type=cache.t4g.micro,loadgen_task_count=7",
                ],
            )

            a = (repo / "multirun" / "runs" / "a.tfvars").read_text(encoding="utf-8")
            b = (repo / "multirun" / "runs" / "b.tfvars").read_text(encoding="utf-8")
            manifest = (repo / "multirun" / "batches" / "smoke.list").read_text(
                encoding="utf-8"
            )
            self.assertIn('loadgen_task_count = "3"', a)
            self.assertIn('loadgen_task_count = "7"', b)
            self.assertEqual(manifest.splitlines(), ["a", "b"])

            again = run_multirun(repo, ["configure-batch", "smoke"], check=False)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("already exists", again.stderr)

    def test_ambiguous_budget_inputs_require_explicit_vars(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            (repo / "terraform.tfvars").write_text(
                textwrap.dedent(
                    """\
                    project_name = var.not_a_scalar
                    engine_type = "valkey"
                    """
                ),
                encoding="utf-8",
            )

            result = run_multirun(repo, ["configure", "a"], check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot infer project_name", result.stderr)


if __name__ == "__main__":
    unittest.main()
