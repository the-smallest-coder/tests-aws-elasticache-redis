import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)

REPORT_CONTRACT_FIXTURES = ROOT / "tests" / "fixtures" / "report_contract"
OLD_RUN = REPORT_CONTRACT_FIXTURES / "legacy_run"
GOOD_RUN = REPORT_CONTRACT_FIXTURES / "current_run"
BAD_RUN = REPORT_CONTRACT_FIXTURES / "missing_benchmark_run"


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
            from parsers import parse_memtier_logs
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

    def test_final_totals_keep_last_record_when_timestamp_ties(self):
        try:
            from parsers import parse_memtier_final_totals
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        content = (
            "[2026-05-01T00:01:00] [memtier/task-a] Totals 100 0 0 1 0.5 2 3 10\n"
            "[2026-05-01T00:01:00] [memtier/task-a] Totals 200 0 0 2 1.5 3 4 20\n"
        )

        parsed = parse_memtier_final_totals(content)

        self.assertEqual(parsed["Ops/sec"].tolist(), [200.0])
        self.assertEqual(parsed["Latency (ms)"].tolist(), [2.0])

    def test_extra_stats_keep_oom_rejections_separate_from_evictions(self):
        try:
            from parsers import parse_memtier_extra_stats
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


class MemtierLatencyMaxTests(unittest.TestCase):
    def test_combined_minutes_keep_progress_latency_max(self):
        try:
            from memtier_etl import generate_memtier_dataframes
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        content = (
            "[2026-05-01T00:00:00] [memtier/stream-a] "
            "[RUN #1 1%, 1 secs] 100 (avg: 100) ops/sec, "
            "1KB/sec (avg: 1KB/sec), 1 (avg: 1) msec latency\n"
            "[2026-05-01T00:00:10] [memtier/stream-a] "
            "[RUN #1 2%, 2 secs] 100 (avg: 100) ops/sec, "
            "1KB/sec (avg: 1KB/sec), 9 (avg: 9) msec latency\n"
            "[2026-05-01T00:00:20] [memtier/stream-b] "
            "[RUN #1 1%, 1 secs] 100 (avg: 100) ops/sec, "
            "1KB/sec (avg: 1KB/sec), 6 (avg: 6) msec latency\n"
        )

        minutes, _totals = generate_memtier_dataframes([("legacy-log", content)])

        self.assertEqual(minutes["latency_weighted_avg"].tolist(), [5.5])
        self.assertEqual(minutes["latency_max"].tolist(), [9.0])

    def test_max_latency_outputs_use_latency_max(self):
        try:
            import pandas as pd

            from cards import stat_cards_html
            from summary import build_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        empty = pd.DataFrame()
        minutes = pd.DataFrame([
            {
                "throughput_sum": 200.0,
                "latency_weighted_avg": 5.5,
                "latency_max": 9.0,
            },
            {
                "throughput_sum": 200.0,
                "latency_weighted_avg": 6.0,
                "latency_max": 8.0,
            },
        ])
        totals = pd.DataFrame([
            {
                "throughput_avg": 100.0,
                "latency_avg_ms": 5.0,
                "total_bandwidth_kbs": 10.0,
            },
            {
                "throughput_avg": 100.0,
                "latency_avg_ms": 6.0,
                "total_bandwidth_kbs": 11.0,
            },
        ])

        report_summary = build_summary(
            empty,
            minutes,
            totals,
            empty,
            extra_stats={},
            config={},
            cluster_id="cluster-a",
            time_range="",
        )
        html = stat_cards_html(minutes, totals, empty, empty)

        self.assertEqual(report_summary["benchmark"]["max_latency_ms"], 9.0)
        self.assertIn(
            "<div class='card-label'>Max Latency</div>"
            "<div class='card-value' style='color:#e8710a'>9.00",
            html,
        )


class EvictionSeriesTests(unittest.TestCase):
    def test_aggregate_evictions_win_over_node_rows(self):
        try:
            import pandas as pd
            from helpers import cloudwatch_eviction_series, first_positive_timestamp
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
            from helpers import cloudwatch_eviction_series
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
            from helpers import cloudwatch_eviction_series
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
            import pandas as pd

            from cards import stat_cards_html
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        empty = pd.DataFrame()
        metrics = pd.DataFrame([
            EvictionSeriesTests._metric("2026-05-01T00:00:00Z", 0, "CacheClusterId=cluster-a"),
            EvictionSeriesTests._metric("2026-05-01T01:02:07Z", 1, "CacheClusterId=cluster-a"),
            EvictionSeriesTests._metric("2026-05-01T00:15:00Z", 9, "CacheClusterId=cluster-b"),
        ])
        metrics["Timestamp"] = pd.to_datetime(metrics["Timestamp"], utc=True).dt.tz_localize(None)

        html = stat_cards_html(
            empty,
            empty,
            metrics,
            empty,
            extra_stats={"first_message_ts": pd.Timestamp("2026-05-01T00:00:00Z")},
            config={},
            cluster_id="cluster-a",
        )

        self.assertIn("<div class='card-label'>First Eviction</div>", html)
        self.assertIn("1h 02m 07s", html)
        self.assertIn("Elapsed time from report start", html)
        self.assertIn("2026-05-01 01:02:07 UTC", html)


class LocalGenerateLegacyLogTests(unittest.TestCase):
    def test_reader_falls_back_to_legacy_root_loadgen_log(self):
        try:
            from report_generator import _read_local_log_contents
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp) / "logs"
            logs_dir.mkdir()
            legacy_log = logs_dir / "cluster-a.txt"
            legacy_log.write_text("legacy memtier log\n", encoding="utf-8")

            self.assertEqual(
                _read_local_log_contents(logs_dir, "cluster-a"),
                [(str(legacy_log), "legacy memtier log\n")],
            )

    def test_legacy_merged_log_keeps_per_stream_memtier_totals(self):
        try:
            from report_generator import _memtier_dfs_from_log_entries, _parse_memtier_log_entries
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        content = (
            "[2026-05-01T00:00:00] [memtier/memtier/stream-a] "
            "[RUN #1 1%, 1 secs] 100 (avg: 100) ops/sec, "
            "1KB/sec (avg: 1KB/sec), 1 (avg: 1) msec latency\n"
            "[2026-05-01T00:00:01] [memtier/memtier/stream-b] "
            "[RUN #1 1%, 1 secs] 200 (avg: 200) ops/sec, "
            "2KB/sec (avg: 2KB/sec), 2 (avg: 2) msec latency\n"
            "[2026-05-01T00:01:00] [memtier/memtier/stream-a] "
            "Totals 100 0 0 1 0.5 2 3 10\n"
            "[2026-05-01T00:01:01] [memtier/memtier/stream-b] "
            "Totals 200 0 0 2 1.5 3 4 20\n"
        )
        entries = [("/tmp/run/logs/cluster-a.txt", content)]

        _logs_df, extra_stats = _parse_memtier_log_entries(entries)
        memtier_minute_df, memtier_totals_df = _memtier_dfs_from_log_entries(entries)

        self.assertEqual(str(extra_stats["first_message_ts"]), "2026-05-01 00:00:00")
        self.assertEqual(memtier_minute_df["throughput_sum"].tolist(), [300.0])
        self.assertEqual(sorted(memtier_totals_df["source"].tolist()), [
            "memtier/memtier/stream-a",
            "memtier/memtier/stream-b",
        ])
        self.assertEqual(float(memtier_totals_df["throughput_avg"].sum()), 300.0)


if __name__ == "__main__":
    unittest.main()
