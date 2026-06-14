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
    def test_memory_and_price_maps_have_same_node_types(self):
        memory_keys = _local_map_keys("_node_memory_bytes")
        price_keys = _local_map_keys("_node_redis_hourly_usd")

        self.assertEqual(price_keys, memory_keys)

    def test_unknown_node_types_do_not_fall_back_to_t4g_micro(self):
        ecs_tf = ECS_TF.read_text(encoding="utf-8")

        self.assertIn("_advertised_bytes = local._node_memory_bytes[var.node_type]", ecs_tf)
        self.assertNotIn("lookup(local._node_memory_bytes", ecs_tf)
        self.assertNotIn('local._node_memory_bytes["cache.t4g.micro"])', ecs_tf)

    def test_m5_large_has_explicit_catalog_entry(self):
        memory_keys = _local_map_keys("_node_memory_bytes")
        price_keys = _local_map_keys("_node_redis_hourly_usd")

        self.assertIn("cache.m5.large", memory_keys)
        self.assertIn("cache.m5.large", price_keys)


if __name__ == "__main__":
    unittest.main()
