import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RunUniquenessContractTests(unittest.TestCase):
    def test_run_id_full_uses_24_hour_timestamp(self):
        main_tf = (ROOT / "main.tf").read_text(encoding="utf-8")
        match = re.search(r'run_id_full\s*=\s*formatdate\("([^"]+)"', main_tf)
        self.assertIsNotNone(match, "run_id_full formatdate not found in main.tf")
        self.assertEqual(match.group(1), "YYYYMMDDHHmmss")

    def test_cluster_id_and_run_folder_reference_discriminator_suffix(self):
        main_tf = (ROOT / "main.tf").read_text(encoding="utf-8")
        self.assertRegex(main_tf, r'cluster_id\s*=.*run_disc_suffix')
        self.assertRegex(main_tf, r'run_folder\s*=.*run_disc_suffix')

    def test_elasticache_replication_group_has_40_char_precondition(self):
        main_tf = (ROOT / "main.tf").read_text(encoding="utf-8")
        self.assertIn("precondition", main_tf)
        self.assertIn("40-character", main_tf)

    def test_variable_and_outputs_define_run_id_discriminator(self):
        variables_tf = (ROOT / "variables.tf").read_text(encoding="utf-8")
        outputs_tf = (ROOT / "outputs.tf").read_text(encoding="utf-8")
        self.assertIn('variable "run_id_discriminator"', variables_tf)
        self.assertRegex(variables_tf, r'\[a-z0-9\]\{0,8\}')
        self.assertIn('output "run_id_discriminator"', outputs_tf)

    def test_cluster_details_records_run_id_discriminator(self):
        node_details_tf = (ROOT / "node_details.tf").read_text(encoding="utf-8")
        self.assertIn("run_id_discriminator = var.run_id_discriminator", node_details_tf)

    def test_reporter_scripts_are_scoped_by_cluster_id(self):
        reporter_tf = (ROOT / "reporter.tf").read_text(encoding="utf-8")
        self.assertIn('reporter_scripts_prefix = "scripts/${local.cluster_id}/"', reporter_tf)
        self.assertIn('key    = "${local.reporter_scripts_prefix}${each.value}"', reporter_tf)
        self.assertRegex(
            reporter_tf,
            r'download_file\(bucket,\s*f"\$\{local\.reporter_scripts_prefix\}\{mod\}"',
        )
        self.assertNotIn('"scripts/${each.value}"', reporter_tf)
        self.assertNotIn('f"scripts/{mod}"', reporter_tf)


if __name__ == "__main__":
    unittest.main()
