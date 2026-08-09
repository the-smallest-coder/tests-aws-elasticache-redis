import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ECS_TF = ROOT / "ecs.tf"


def _local_map_keys(map_name: str) -> set[str]:
    text = ECS_TF.read_text(encoding="utf-8")
    match = re.search(
        rf"{re.escape(map_name)}\s*=\s*\{{(?P<body>.*?)\n\s*\}}",
        text,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError(f"{map_name} map not found")
    return set(re.findall(r'"(cache\.[^"]+)"', match.group("body")))


class ElastiCacheNodeCatalogTests(unittest.TestCase):
    def test_unknown_node_types_do_not_fall_back_to_t4g_micro(self):
        ecs_tf = ECS_TF.read_text(encoding="utf-8")

        self.assertIn("_advertised_bytes = local._node_memory_bytes[var.node_type]", ecs_tf)
        self.assertNotIn("lookup(local._node_memory_bytes", ecs_tf)
        self.assertNotIn('local._node_memory_bytes["cache.t4g.micro"])', ecs_tf)

    def test_m5_large_has_explicit_memory_catalog_entry(self):
        memory_keys = _local_map_keys("_node_memory_bytes")

        self.assertIn("cache.m5.large", memory_keys)

    def test_node_price_is_fetched_live_not_from_a_hardcoded_table(self):
        """Pricing used to be a hardcoded, single-region, Redis-only USD table
        (``_node_redis_hourly_usd``), so a Valkey run or a non-us-east-1 run
        silently got the wrong number under a "Redis hourly" label. Pricing
        must instead be looked up per node_type/engine_type/aws_region via
        the AWS Price List API at apply time, so it stays correct for any
        combination without needing a maintained table.
        """
        ecs_tf = ECS_TF.read_text(encoding="utf-8")

        self.assertNotIn("_node_redis_hourly_usd", ecs_tf)
        self.assertIn('data "external" "node_price"', ecs_tf)
        self.assertRegex(ecs_tf, r"node_type\s*=\s*var\.node_type")
        self.assertRegex(ecs_tf, r"engine_type\s*=\s*var\.engine_type")
        self.assertRegex(ecs_tf, r"aws_region\s*=\s*var\.aws_region")

    def test_node_price_script_never_fails_terraform_apply_on_lookup_errors(self):
        """A pricing lookup failure (missing IAM permission, no network, no
        matching SKU) must degrade to an 'unavailable' result, never crash
        `terraform apply` -- cost reporting is a side value, not something
        that should block spinning up the benchmark infrastructure.
        """
        script = (ROOT / "scripts" / "fetch_elasticache_price.sh").read_text(encoding="utf-8")

        self.assertIn("source: \"unavailable\"", script)
        self.assertIn("exit 0", script)
        self.assertNotIn("set -e", script)

    def test_node_price_script_is_executable(self):
        script_path = ROOT / "scripts" / "fetch_elasticache_price.sh"
        self.assertTrue(script_path.exists())
        if os.name != "nt":
            self.assertTrue(os.access(script_path, os.X_OK))


if __name__ == "__main__":
    unittest.main()
