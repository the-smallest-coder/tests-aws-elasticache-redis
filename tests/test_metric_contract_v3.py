"""WP1 -- dedup helper, network/latency fixes, deprecation, schema v3.

See PLAN_2.md WP1. Legacy keys are frozen (D1): every test that touches one
must prove the OLD code path is untouched, while the new key gets the fixed
math.
"""

import json
import sys
import unittest
from datetime import date
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

    def test_avg_out_kbs_legacy_value_is_bit_for_bit_unchanged(self):
        try:
            summary = self._build_summary([
                _metric_row("2026-08-10T00:00:00Z", 60 * 1024, "CacheClusterId=cluster-a", "NetworkBytesOut"),
                _metric_row("2026-08-10T00:00:00Z", 60 * 1024, "CacheClusterId=cluster-a;CacheNodeId=0001", "NetworkBytesOut"),
            ])
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        # Legacy field: same old (buggy) code -- sums both duplicate rows,
        # divides by 1024 only. 2 * 60*1024 bytes / 1024 = 120 "KB/min".
        self.assertEqual(summary["network"]["cache"]["avg_out_kbs"], 120.0)

    def test_bw_out_exceeded_count_is_half_of_the_duplicated_legacy_total(self):
        try:
            summary = self._build_summary([
                _metric_row("2026-08-10T00:00:00Z", 9, "CacheClusterId=cluster-a", "NetworkBandwidthOutAllowanceExceeded"),
                _metric_row("2026-08-10T00:00:00Z", 9, "CacheClusterId=cluster-a;CacheNodeId=0001", "NetworkBandwidthOutAllowanceExceeded"),
            ])
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        legacy_total = summary["network"]["throttling"]["bw_out_exceeded_total"]
        new_count = summary["network"]["throttling"]["bw_out_exceeded_count"]
        self.assertEqual(legacy_total, 18)
        self.assertEqual(new_count, legacy_total / 2)
        self.assertEqual(new_count, 9)


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


class DeprecatedFieldsManifestTests(unittest.TestCase):
    def test_meta_deprecated_fields_lists_exactly_the_eleven_frozen_names(self):
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

        self.assertEqual(
            set(summary["meta"]["deprecated_fields"]),
            {
                "avg_in_kbs",
                "avg_out_kbs",
                "bw_in_exceeded_total",
                "bw_out_exceeded_total",
                "pps_exceeded_total",
                "p50_ms",
                "p99_ms",
                "p999_ms",
                "worst_stream_p99_ms",
                "worst_stream_p999_ms",
                "avg_bandwidth_kbs",
            },
        )
        self.assertEqual(len(summary["meta"]["deprecated_fields"]), 11)


class ReportCompareLegacyFallbackTests(unittest.TestCase):
    def test_baseline_missing_new_field_falls_back_to_legacy_with_warning_tone(self):
        try:
            from report_common import RunData
            from report_compare import metric_rows
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        baseline = RunData(
            role="Baseline",
            results_path=Path("results/baseline/results_baseline.json"),
            folder="baseline",
            summary={"client_latency": {"p99_ms": 5.0}},
            cluster_details=None,
        )
        candidate = RunData(
            role="Candidate",
            results_path=Path("results/candidate/results_candidate.json"),
            folder="candidate",
            summary={"client_latency": {"p99_ms": 5.5, "task_median_p99_ms": 4.8}},
            cluster_details=None,
        )

        rows = metric_rows(baseline, candidate)
        row = next(r for r in rows if r["label"].startswith("ECS Task Latency p99") and "p99.9" not in r["label"])

        self.assertEqual(row["label"], "ECS Task Latency p99 (legacy)")
        self.assertEqual(row["tone"], "warning")
        self.assertEqual(row["path"], ("client_latency", "p99_ms"))


class D1aLegacyKeyExpiryTests(unittest.TestCase):
    """Test 18: D1a's sunset clause must be enforced by a test, not a comment."""

    def test_legacy_keys_are_due_for_removal_once_v3_has_enough_runs_or_the_date_passes(self):
        try:
            from report_common import GENERATOR_SCHEMA_VERSION
        except ModuleNotFoundError as exc:
            _skip_if_no_pandas(self, exc)
            return

        results_root = ROOT / "results"
        v3_count = 0
        if results_root.is_dir():
            for entry in results_root.iterdir():
                if not entry.is_dir():
                    continue
                for json_path in entry.glob("results_*.json"):
                    if json_path.name == "results_local.json":
                        continue
                    try:
                        data = json.loads(json_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(data, dict) and data.get("meta", {}).get("generator_schema_version") == GENERATOR_SCHEMA_VERSION:
                        v3_count += 1

        cutoff = date(2027, 1, 1)
        today = date.today()
        if v3_count >= 10 or today >= cutoff:
            self.fail(
                "legacy keys are due for removal: "
                f"{v3_count} schema-v3 canonical report(s) in results/ (threshold 10); "
                f"today is {today}, cutoff is {cutoff} (D1a)"
            )


if __name__ == "__main__":
    unittest.main()
