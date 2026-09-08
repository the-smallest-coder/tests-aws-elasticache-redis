"""WP6 tests 52-55, 57-62: aggregator.py (grouping, CV, cost-per-op).

Test 56 (acceptance: grouping by the full post-WP0 fingerprint must split
today's high-CV groups) needs a real post-WP0 series and can't be produced
from existing data (D10) -- see PLAN_2.md WP6 step 3.
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


def _cluster_details(**overrides):
    details = {
        "memtier": {
            "task_count": 6, "clients": 4, "threads": 2, "pipeline": 8,
            "data_size_bytes": 32, "ratio": "1:10", "key_pattern": "R:R",
            "key_maximum_total": 100000, "test_time_seconds": 3600,
        },
        "ecs": {"fargate_cpu": 512, "fargate_memory": 1024},
        "elasticache": {"cluster_mode_enabled": False, "num_cache_nodes": 1},
    }
    for path, value in overrides.items():
        section, field = path.split(".")
        details[section][field] = value
    return details


def _summary(avg_ops=None, node_type="cache.t4g.micro", engine="valkey", engine_version="9.0", **meta_overrides):
    meta = {
        "node_type": node_type, "engine_type": engine, "engine_version": engine_version,
        "node_hourly_usd": "", "node_count": "1",
        "report_start": "2026-08-10T12:00:00", "report_end": "2026-08-10T13:00:00",
    }
    meta.update(meta_overrides)
    summary = {"meta": meta, "benchmark": {}, "cache_efficiency": {}, "errors": {}}
    if avg_ops is not None:
        summary["benchmark"]["avg_ops"] = avg_ops
    return summary


def _run(folder, summary, cluster_details):
    from report_common import RunData

    return RunData(
        role=folder, results_path=Path(f"results/{folder}/results_{folder}.json"),
        folder=folder, summary=summary, cluster_details=cluster_details,
    )


class GroupingTests(unittest.TestCase):
    def _load(self):
        try:
            from aggregator import group_runs
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        return group_runs

    def test_three_matching_runs_form_one_group(self):
        group_runs = self._load()
        runs = [
            _run("run-a", _summary(avg_ops=100), _cluster_details()),
            _run("run-b", _summary(avg_ops=200), _cluster_details()),
            _run("run-c", _summary(avg_ops=300), _cluster_details()),
        ]

        groups, unmatched = group_runs(runs)

        self.assertEqual(unmatched, [])
        self.assertEqual(len(groups), 1)
        (group,) = groups.values()
        self.assertEqual(len(group["runs"]), 3)

    def test_different_pipeline_forms_a_separate_group(self):
        group_runs = self._load()
        runs = [
            _run("run-a", _summary(avg_ops=100), _cluster_details()),
            _run("run-b", _summary(avg_ops=200), _cluster_details()),
            _run("run-c", _summary(avg_ops=999), _cluster_details(**{"memtier.pipeline": 16})),
        ]

        groups, unmatched = group_runs(runs)

        self.assertEqual(unmatched, [])
        self.assertEqual(len(groups), 2)
        sizes = sorted(len(group["runs"]) for group in groups.values())
        self.assertEqual(sizes, [1, 2])


class MetricStatsTests(unittest.TestCase):
    def _load(self):
        try:
            from aggregator import metric_stats
            from report_compare import METRICS
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        return metric_stats, METRICS

    def _rows(self, metric_stats, METRICS, runs):
        try:
            return metric_stats(runs, METRICS)
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

    def test_cv_is_reported_never_an_automatic_verdict(self):
        metric_stats, METRICS = self._load()
        runs = [
            _run("run-a", _summary(avg_ops=100), _cluster_details()),
            _run("run-b", _summary(avg_ops=200), _cluster_details()),
            _run("run-c", _summary(avg_ops=300), _cluster_details()),
        ]

        rows = self._rows(metric_stats, METRICS, runs)

        avg_ops_row = next(row for row in rows if row["path"] == ["benchmark", "avg_ops"])
        self.assertIn("cv_pct", avg_ops_row)
        self.assertNotIn("fingerprint_incomplete", avg_ops_row)
        self.assertNotIn("verdict", avg_ops_row)
        self.assertEqual(avg_ops_row["n"], 3)
        self.assertEqual(avg_ops_row["median"], 200.0)

    def test_cv_uses_sample_stdev_ddof_1(self):
        metric_stats, METRICS = self._load()
        runs = [
            _run("run-a", _summary(avg_ops=100), _cluster_details()),
            _run("run-b", _summary(avg_ops=200), _cluster_details()),
            _run("run-c", _summary(avg_ops=300), _cluster_details()),
        ]

        rows = self._rows(metric_stats, METRICS, runs)
        avg_ops_row = next(row for row in rows if row["path"] == ["benchmark", "avg_ops"])

        # mean=200, sample stdev (ddof=1) of [100,200,300] is 100 -> CV 50%.
        # Population stdev (ddof=0) would give sqrt(20000/3)=81.6 -> CV 40.8%.
        self.assertAlmostEqual(avg_ops_row["cv_pct"], 50.0, places=6)


class CostPerSuccessfulOperationTests(unittest.TestCase):
    def _load(self):
        try:
            from aggregator import cost_per_successful_operation
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        return cost_per_successful_operation

    def test_no_fallback_to_redis_hourly_usd(self):
        cost = self._load()
        summary = _summary(node_hourly_usd="")
        summary["meta"]["redis_hourly_usd"] = "0.226"  # legacy, Redis-only, us-east-1 -- must be ignored
        summary["cache_efficiency"]["total_ops"] = 1000
        run = _run("run-a", summary, _cluster_details())

        result = cost(run)

        self.assertIsNone(result["value"])
        self.assertEqual(result["reason"], "node_hourly_usd_unavailable")

    def test_known_inputs_produce_the_exact_expected_value(self):
        cost = self._load()
        summary = _summary(node_hourly_usd="0.36", node_count="1")
        summary["cache_efficiency"]["total_ops"] = 3_600_000
        summary["errors"]["error_count_total"] = 0
        run = _run("run-a", summary, _cluster_details())

        result = cost(run)

        # window = 3600s (12:00->13:00), successful_ops = 3,600,000
        # ops/sec = 1000, cluster_usd_per_sec = 0.36/3600 = 0.0001
        # cost = 0.0001 / 1000 = 1e-7
        self.assertAlmostEqual(result["value"], 1e-7, places=12)
        self.assertFalse(result["errors_unknown"])

    def test_node_count_string_doubles_the_cost(self):
        cost = self._load()

        def _make(node_count):
            summary = _summary(node_hourly_usd="0.36", node_count=node_count)
            summary["cache_efficiency"]["total_ops"] = 3_600_000
            summary["errors"]["error_count_total"] = 0
            return _run("run-a", summary, _cluster_details())

        single = cost(_make("1"))
        doubled_str = cost(_make("2"))
        doubled_int = cost(_make(2))

        self.assertAlmostEqual(doubled_str["value"], single["value"] * 2, places=12)
        self.assertAlmostEqual(doubled_int["value"], single["value"] * 2, places=12)

    def test_missing_price_is_none_without_exception(self):
        cost = self._load()
        summary = _summary(node_hourly_usd="")
        summary["cache_efficiency"]["total_ops"] = 1000
        run = _run("run-a", summary, _cluster_details())

        result = cost(run)  # must not raise

        self.assertIsNone(result["value"])
        self.assertEqual(result["reason"], "node_hourly_usd_unavailable")

    def test_missing_error_count_falls_back_to_total_ops_and_flags_errors_unknown(self):
        cost = self._load()
        summary = _summary(node_hourly_usd="0.36", node_count="1")
        summary["cache_efficiency"]["total_ops"] = 3_600_000
        # errors.error_count_total deliberately absent (old, pre-WP2 run).
        run = _run("run-a", summary, _cluster_details())

        result = cost(run)

        self.assertIsNotNone(result["value"])
        self.assertTrue(result["errors_unknown"])
        # Same value as when errors are known to be exactly 0.
        summary_known_zero = _summary(node_hourly_usd="0.36", node_count="1")
        summary_known_zero["cache_efficiency"]["total_ops"] = 3_600_000
        summary_known_zero["errors"]["error_count_total"] = 0
        result_known_zero = cost(_run("run-b", summary_known_zero, _cluster_details()))
        self.assertAlmostEqual(result["value"], result_known_zero["value"], places=12)


class TotalOpsDedupTests(unittest.TestCase):
    """Test 62: cache_efficiency.total_ops (WP6 step 1) through the same D6
    dedup as everything else -- regression against the 2x duplicate-row bug."""

    def test_total_ops_is_not_double_counted_across_dimension_levels(self):
        try:
            import pandas as pd
            from summary import build_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        def _row(metric_name, value, dimensions):
            return {
                "Timestamp": "2026-08-10T00:00:00Z", "Namespace": "AWS/ElastiCache",
                "MetricName": metric_name, "Stat": "Sum", "Value": value,
                "Unit": "Count", "Dimensions": dimensions,
            }

        metrics_df = pd.DataFrame([
            _row("GetTypeCmds", 1000, "CacheClusterId=cluster-a"),
            _row("GetTypeCmds", 1000, "CacheClusterId=cluster-a;CacheNodeId=0001"),
            _row("SetTypeCmds", 100, "CacheClusterId=cluster-a"),
            _row("SetTypeCmds", 100, "CacheClusterId=cluster-a;CacheNodeId=0001"),
        ])
        metrics_df["Timestamp"] = pd.to_datetime(metrics_df["Timestamp"], utc=True).dt.tz_localize(None)

        summary = build_summary(
            metrics_df, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
            extra_stats={}, config={}, cluster_id="cluster-a", time_range="",
        )

        self.assertEqual(summary["cache_efficiency"]["total_ops"], 1100)
        self.assertEqual(summary["cache_efficiency"]["total_writes"], 100)


if __name__ == "__main__":
    unittest.main()
