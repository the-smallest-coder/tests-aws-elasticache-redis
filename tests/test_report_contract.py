import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD_RUN = ROOT / "results" / "20260227-140039"
GOOD_RUN = ROOT / "results" / "20260501-063922"
BAD_RUN = ROOT / "results" / "20260501-083934"


def card_labels(run_dir: Path) -> set[str]:
    html = (run_dir / "results_local.html").read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"<div class='card-label'>(.*?)</div>", html))


def summary(run_dir: Path) -> dict:
    return json.loads((run_dir / "results_local.json").read_text(encoding="utf-8"))


class ReportContractTests(unittest.TestCase):
    def test_good_report_keeps_old_card_contract(self):
        old_labels = card_labels(OLD_RUN)
        good_labels = card_labels(GOOD_RUN)

        self.assertLessEqual(old_labels, good_labels)

    def test_good_report_keeps_old_json_contract(self):
        old = summary(OLD_RUN)
        good = summary(GOOD_RUN)

        for section, values in old.items():
            if not isinstance(values, dict):
                continue
            self.assertIn(section, good)
            self.assertLessEqual(set(values), set(good[section]), section)

    def test_missing_benchmark_fixture_shows_current_failure(self):
        bad = summary(BAD_RUN)

        self.assertFalse(bad["benchmark"])
        self.assertFalse((BAD_RUN / "logs").exists())
        self.assertNotIn("Avg Throughput", card_labels(BAD_RUN))


class MemtierParserTests(unittest.TestCase):
    def test_parser_handles_carriage_return_progress_records(self):
        try:
            from reporter.parsers import parse_memtier_logs
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        content = (
            "[2026-05-01T00:00:10] [memtier/task-a] "
            "[RUN #1 10%, 1 secs] 100.00 (avg: 100.00) ops/sec, "
            "1.00 (avg: 1.00) msec latency, 1.00KB/sec (avg: 1.00KB/sec)\r"
            "[RUN #1 20%, 2 secs] 200.00 (avg: 150.00) ops/sec, "
            "2.00 (avg: 1.50) msec latency, 2.00KB/sec (avg: 1.50KB/sec)\n"
        )

        parsed = parse_memtier_logs(content)

        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed["Ops/sec"].tolist(), [100.0, 150.0])
        self.assertEqual(parsed["Latency (ms)"].tolist(), [1.0, 1.5])

    def test_extra_stats_keep_oom_rejections_separate_from_evictions(self):
        try:
            from reporter.parsers import parse_memtier_extra_stats
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        stats = parse_memtier_extra_stats(
            "[2026-05-01T00:00:10] [memtier/task-a] -OOM command not allowed\n"
            "[2026-05-01T00:00:20] [memtier/task-a] -OOM command not allowed\n"
        )

        self.assertNotIn("first_eviction_ts", stats)
        self.assertEqual(str(stats["first_oom_rejection_ts"]), "2026-05-01 00:00:10")
        self.assertEqual(int(stats["oom_df"]["OOM_events"].sum()), 2)


class EvictionSeriesTests(unittest.TestCase):
    def test_aggregate_evictions_win_over_node_rows(self):
        try:
            import pandas as pd
            from reporter.helpers import cloudwatch_eviction_series, first_positive_timestamp
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        metrics = pd.DataFrame([
            self._metric("2026-05-01T00:00:00Z", 0, "CacheClusterId=cluster-a"),
            self._metric("2026-05-01T00:01:00Z", 3, "CacheClusterId=cluster-a"),
            self._metric("2026-05-01T00:00:00Z", 0, "CacheClusterId=cluster-a;CacheNodeId=0001"),
            self._metric("2026-05-01T00:01:00Z", 3, "CacheClusterId=cluster-a;CacheNodeId=0001"),
            self._metric("2026-05-01T00:01:00Z", 4, "CacheClusterId=cluster-a;CacheNodeId=0002"),
        ])
        metrics["Timestamp"] = pd.to_datetime(metrics["Timestamp"], utc=True).dt.tz_localize(None)

        selected = cloudwatch_eviction_series(metrics, "cluster-a")

        self.assertEqual(selected["Value"].tolist(), [0, 3])
        self.assertEqual(str(first_positive_timestamp(selected)), "2026-05-01 00:01:00")

    def test_node_evictions_are_summed_when_aggregate_rows_are_absent(self):
        try:
            import pandas as pd
            from reporter.helpers import cloudwatch_eviction_series
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        metrics = pd.DataFrame([
            self._metric("2026-05-01T00:00:00Z", 1, "CacheClusterId=cluster-a;CacheNodeId=0001"),
            self._metric("2026-05-01T00:00:00Z", 2, "CacheClusterId=cluster-a;CacheNodeId=0002"),
        ])
        metrics["Timestamp"] = pd.to_datetime(metrics["Timestamp"], utc=True).dt.tz_localize(None)

        selected = cloudwatch_eviction_series(metrics, "cluster-a")

        self.assertEqual(selected["Value"].tolist(), [3])

    def test_replication_group_id_selects_suffixed_non_cluster_node_rows(self):
        try:
            import pandas as pd
            from reporter.helpers import cloudwatch_eviction_series
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        metrics = pd.DataFrame([
            self._metric("2026-05-01T00:00:00Z", 7, "CacheClusterId=cluster-a-001;CacheNodeId=0001"),
        ])
        metrics["Timestamp"] = pd.to_datetime(metrics["Timestamp"], utc=True).dt.tz_localize(None)

        selected = cloudwatch_eviction_series(metrics, "cluster-a")

        self.assertEqual(selected["Value"].tolist(), [7])

    @staticmethod
    def _metric(timestamp: str, value: int, dimensions: str) -> dict:
        return {
            "Timestamp": timestamp,
            "Namespace": "AWS/ElastiCache",
            "MetricName": "Evictions",
            "Stat": "Sum",
            "Value": value,
            "Unit": "Count",
            "Dimensions": dimensions,
        }


class CardRenderingTests(unittest.TestCase):
    def test_first_eviction_card_shows_elapsed_time_from_report_start(self):
        try:
            import sys

            import pandas as pd

            sys.path.insert(0, str(ROOT / "reporter"))
            from cards import stat_cards_html
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise
        finally:
            if str(ROOT / "reporter") in sys.path:
                sys.path.remove(str(ROOT / "reporter"))

        empty = pd.DataFrame()
        metrics = pd.DataFrame([
            EvictionSeriesTests._metric("2026-05-01T00:00:00Z", 0, "CacheClusterId=cluster-a"),
            EvictionSeriesTests._metric("2026-05-01T01:02:07Z", 1, "CacheClusterId=cluster-a"),
        ])
        metrics["Timestamp"] = pd.to_datetime(metrics["Timestamp"], utc=True).dt.tz_localize(None)

        html = stat_cards_html(
            empty,
            empty,
            metrics,
            empty,
            extra_stats={"first_message_ts": pd.Timestamp("2026-05-01T00:00:00Z")},
            config={"cluster_id": "cluster-a"},
        )

        self.assertIn("<div class='card-label'>First Eviction</div>", html)
        self.assertIn("1h 02m 07s", html)
        self.assertIn("Elapsed time from report start", html)
        self.assertIn("2026-05-01 01:02:07 UTC", html)


if __name__ == "__main__":
    unittest.main()
