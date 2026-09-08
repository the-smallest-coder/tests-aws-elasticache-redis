"""WP1 -- dedup helper, network/latency fixes, schema v3.

See PLAN_2.md WP1. The D1 double-write/freeze scaffolding these tests
originally also covered (DEPRECATED_FIELDS, legacy_path fallback rendering,
the D1a sunset test) was removed once the schema-v4 cleanup retired it; see
that commit for why. What's left here is the dedup helper and the
network/latency math fixes themselves.
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


def _skip_if_no_pandas(test_case, exc):
    if exc.name == "pandas":
        test_case.skipTest("pandas is not installed in this environment")
    raise exc


def _metric_row(timestamp, value, dimensions, metric_name="GetTypeCmds", stat="Sum"):
    return {
        "Timestamp": timestamp,
        "Namespace": "AWS/ElastiCache",
        "MetricName": metric_name,
        "Stat": stat,
        "Value": value,
        "Unit": "Count",
        "Dimensions": dimensions,
    }


class SelectNodeDimensionRowsTests(unittest.TestCase):
    """Tests 7-10: the extracted dedup helper (helpers.select_node_dimension_rows)."""

    def _load(self):
        try:
            import pandas as pd
            from helpers import select_node_dimension_rows
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
        return pd, select_node_dimension_rows

    def test_aggregate_and_node_duplicate_rows_are_not_both_summed(self):
        pd, select_node_dimension_rows = self._load()

        df = pd.DataFrame([
            {"Dimensions": "CacheClusterId=cluster-a", "Value": 168860450},
            {"Dimensions": "CacheClusterId=cluster-a;CacheNodeId=0001", "Value": 168860450},
        ])

        selected = select_node_dimension_rows(df, "cluster-a")

        self.assertEqual(float(selected["Value"].sum()), 168860450)

    def test_node_only_rows_are_summed_when_aggregate_is_absent(self):
        pd, select_node_dimension_rows = self._load()

        df = pd.DataFrame([
            {"Dimensions": "CacheClusterId=cluster-a;CacheNodeId=0001", "Value": 100},
            {"Dimensions": "CacheClusterId=cluster-a;CacheNodeId=0002", "Value": 50},
        ])

        selected = select_node_dimension_rows(df, "cluster-a")

        self.assertEqual(float(selected["Value"].sum()), 150)

    def test_multi_node_bare_ids_fall_back_to_summing_all_node_rows(self):
        pd, select_node_dimension_rows = self._load()

        df = pd.DataFrame([
            {"Dimensions": "CacheClusterId=cluster-a-001", "Value": 999},  # bare aggregate-looking, but 2 distinct IDs
            {"Dimensions": "CacheClusterId=cluster-a-002", "Value": 999},
            {"Dimensions": "CacheClusterId=cluster-a-001;CacheNodeId=0001", "Value": 10},
            {"Dimensions": "CacheClusterId=cluster-a-002;CacheNodeId=0001", "Value": 20},
        ])

        selected = select_node_dimension_rows(df, "cluster-a")

        # Two distinct bare IDs -> no single aggregate; falls back to node rows.
        self.assertEqual(float(selected["Value"].sum()), 30)

    def test_suffixed_replication_group_id_selects_bare_row(self):
        pd, select_node_dimension_rows = self._load()

        df = pd.DataFrame([
            {"Dimensions": "CacheClusterId=cluster-a-001", "Value": 42},
        ])

        # This is the real production path: meta.cluster_id is the bare
        # replication group id, the dimension carries a "-001" node suffix.
        selected = select_node_dimension_rows(df, "cluster-a")

        self.assertEqual(float(selected["Value"].sum()), 42)


class NetworkFixTests(unittest.TestCase):
    """Tests 11-14: the 120x network bug (2x duplicate rows, 60x missing /60)."""

    def _build_summary(self, metrics_rows, ecs_df=None):
        import pandas as pd
        from summary import build_summary

        metrics_df = pd.DataFrame(metrics_rows) if metrics_rows else pd.DataFrame()
        if not metrics_df.empty:
            metrics_df["Timestamp"] = pd.to_datetime(metrics_df["Timestamp"], utc=True).dt.tz_localize(None)
        return build_summary(
            metrics_df,
            pd.DataFrame(),
            pd.DataFrame(),
            ecs_df if ecs_df is not None else pd.DataFrame(),
            extra_stats={},
            config={},
            cluster_id="cluster-a",
            time_range="",
        )

    def _load(self):
        try:
            return self._build_summary
        except ModuleNotFoundError as exc:  # pragma: no cover - defensive
            _skip_if_no_pandas(self, exc)

    def test_out_kib_per_sec_is_not_double_counted_from_aggregate_and_node_rows(self):
        try:
            summary = self._build_summary([
                _metric_row("2026-08-10T00:00:00Z", 3600 * 1024, "CacheClusterId=cluster-a", "NetworkBytesOut"),
                _metric_row("2026-08-10T00:00:00Z", 3600 * 1024, "CacheClusterId=cluster-a;CacheNodeId=0001", "NetworkBytesOut"),
            ])
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        # 3600*1024 bytes over the 60s bucket, deduplicated (not summed
        # twice): (3600*1024 / 60) / 1024 = 60 KiB/s.
        self.assertEqual(summary["network"]["cache"]["out_kib_per_sec"], 60.0)

    def test_known_byte_sum_converts_exactly_via_division_by_60(self):
        try:
            summary = self._build_summary([
                _metric_row("2026-08-10T00:00:00Z", 60 * 1024, "CacheClusterId=cluster-a", "NetworkBytesOut"),
            ])
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        # 60*1024 bytes over a 60s bucket -> exactly 1024 bytes/sec -> 1 KiB/s.
        self.assertEqual(summary["network"]["cache"]["out_kib_per_sec"], 1.0)

    def test_bw_out_exceeded_count_is_deduplicated_not_double_counted(self):
        try:
            summary = self._build_summary([
                _metric_row("2026-08-10T00:00:00Z", 9, "CacheClusterId=cluster-a", "NetworkBandwidthOutAllowanceExceeded"),
                _metric_row("2026-08-10T00:00:00Z", 9, "CacheClusterId=cluster-a;CacheNodeId=0001", "NetworkBandwidthOutAllowanceExceeded"),
            ])
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        # Same duplicate-dimension input the 120x network bug came from
        # (D6): aggregate and CacheNodeId rows carry the same value, only
        # one should be counted.
        self.assertEqual(summary["network"]["throttling"]["bw_out_exceeded_count"], 9)


class ClientLatencyTaskMedianTests(unittest.TestCase):
    def test_task_median_p99_comes_from_canonical_totals_column(self):
        try:
            import pandas as pd
            from summary import build_summary
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        # Two tasks' final Totals: canonical p99_latency_ms values 4.0 and 6.0.
        totals_df = pd.DataFrame([
            {"source": "task-a", "p50_latency_ms": 1.0, "p99_latency_ms": 4.0, "p999_latency_ms": 8.0},
            {"source": "task-b", "p50_latency_ms": 2.0, "p99_latency_ms": 6.0, "p999_latency_ms": 10.0},
        ])
        # A per-minute EMF series with a deliberately different p99 (99.0) to
        # prove the new field is NOT sourced from this legacy path.
        ecs_rows = [
            {
                "Timestamp": "2026-08-10T00:00:00Z",
                "Namespace": "ElastiCache/LoadGenerator",
                "MetricName": "ClientLatency",
                "Stat": stat,
                "Value": 99.0,
                "Unit": "Milliseconds",
                "Dimensions": "ClusterName=cluster-a;ServiceName=svc;TaskId=task-a",
            }
            for stat in ("p50", "p99", "p99.9")
        ]
        ecs_df = pd.DataFrame(ecs_rows)
        ecs_df["Timestamp"] = pd.to_datetime(ecs_df["Timestamp"], utc=True).dt.tz_localize(None)

        summary = build_summary(
            pd.DataFrame(), pd.DataFrame(), totals_df, ecs_df,
            extra_stats={}, config={}, cluster_id="cluster-a", time_range="",
        )

        self.assertEqual(summary["client_latency"]["task_median_p99_ms"], 5.0)
        self.assertNotEqual(summary["client_latency"]["task_median_p99_ms"], 99.0)

    def test_missing_memtier_totals_df_yields_none_without_exception(self):
        try:
            import pandas as pd
            from summary import build_summary
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        summary = build_summary(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
            extra_stats={}, config={}, cluster_id="cluster-a", time_range="",
        )

        self.assertIsNone(summary["client_latency"].get("task_median_p99_ms"))


if __name__ == "__main__":
    unittest.main()
