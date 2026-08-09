import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ECS_TF = ROOT / "ecs.tf"
MAIN_TF = ROOT / "main.tf"


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

    def test_memory_catalog_includes_large_node_sizes(self):
        """The map used to stop at each family's original top entry (e.g.
        r7g.8xlarge, m5.4xlarge), so a real ElastiCache size above that
        point hit the raw 'Invalid index' below with no indication of what
        went wrong. These sizes are real, current-generation ElastiCache
        node types (docs.aws.amazon.com/AmazonElastiCache 'Supported node
        types').
        """
        memory_keys = _local_map_keys("_node_memory_bytes")

        for node_type in (
            "cache.r7g.12xlarge",
            "cache.r7g.16xlarge",
            "cache.m7g.12xlarge",
            "cache.m7g.16xlarge",
            "cache.r6g.12xlarge",
            "cache.r6g.16xlarge",
            "cache.c7gn.12xlarge",
            "cache.c7gn.16xlarge",
            "cache.m5.12xlarge",
            "cache.m5.24xlarge",
        ):
            self.assertIn(node_type, memory_keys)

    def test_node_type_precondition_names_supported_set(self):
        """A bare 'Invalid index' names neither the bad value nor what's
        valid. The replication group's precondition must run alongside it
        and spell out the actual supported node_type set so the failure is
        actionable without reading ecs.tf.
        """
        main_tf = MAIN_TF.read_text(encoding="utf-8")

        self.assertRegex(
            main_tf,
            r"contains\(keys\(local\._node_memory_bytes\),\s*var\.node_type\)",
        )
        self.assertIn("Supported types:", main_tf)
        self.assertIn("sort(keys(local._node_memory_bytes))", main_tf)

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
