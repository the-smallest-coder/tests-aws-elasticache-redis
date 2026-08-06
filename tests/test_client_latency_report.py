import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


class ClientLatencyReportTests(unittest.TestCase):
    @staticmethod
    def _latency_row(timestamp, stat, value, dimensions):
        return {
            "Timestamp": timestamp,
            "Namespace": "ElastiCache/LoadGenerator",
            "MetricName": "ClientLatency",
            "Stat": stat,
            "Value": value,
            "Unit": "Milliseconds",
            "Dimensions": dimensions,
        }

    def test_client_latency_series_and_summary_use_timestamped_emf_rows(self):
        try:
            import pandas as pd

            from helpers import client_latency_series
            from summary import build_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        ecs_df = pd.DataFrame([
            self._latency_row("2026-05-01T00:00:00Z", "p50", 1.0, "ClusterName=c;ServiceName=s;TaskId=a"),
            self._latency_row("2026-05-01T00:00:00Z", "p99", 8.0, "ClusterName=c;ServiceName=s;TaskId=a"),
            self._latency_row("2026-05-01T00:00:00Z", "p99.9", 18.0, "ClusterName=c;ServiceName=s;TaskId=a"),
            self._latency_row("2026-05-01T00:00:00Z", "p50", 3.0, "ClusterName=c;ServiceName=s;TaskId=b"),
            self._latency_row("2026-05-01T00:00:00Z", "p99", 12.0, "ClusterName=c;ServiceName=s;TaskId=b"),
            self._latency_row("2026-05-01T00:00:00Z", "p99.9", 28.0, "ClusterName=c;ServiceName=s;TaskId=b"),
            self._latency_row("2026-05-01T00:01:00Z", "p50", 5.0, "ClusterName=c;ServiceName=s;TaskId=a"),
            self._latency_row("2026-05-01T00:01:00Z", "p99", 15.0, "ClusterName=c;ServiceName=s;TaskId=a"),
            self._latency_row("2026-05-01T00:01:00Z", "p99.9", 35.0, "ClusterName=c;ServiceName=s;TaskId=a"),
        ])
        ecs_df["Timestamp"] = pd.to_datetime(ecs_df["Timestamp"], utc=True).dt.tz_localize(None)

        series = client_latency_series(ecs_df)
        report_summary = build_summary(
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            ecs_df,
            extra_stats={},
            config={},
            cluster_id="cluster-a",
            time_range="",
        )

        self.assertEqual(series["p50_ms"].tolist(), [2.0, 5.0])
        self.assertEqual(series["p99_ms"].tolist(), [10.0, 15.0])
        self.assertEqual(series["worst_stream_p99_ms"].tolist(), [12.0, 15.0])
        self.assertEqual(
            report_summary["client_latency"],
            {
                "p50_ms": 3.5,
                "p99_ms": 12.5,
                "p999_ms": 29.0,
                "worst_stream_p99_ms": 15.0,
                "worst_stream_p999_ms": 35.0,
            },
        )

    def test_ecs_task_latency_is_the_final_infrastructure_row(self):
        try:
            import pandas as pd

            from charts import build_infra_figure
        except ModuleNotFoundError as exc:
            if exc.name in {"pandas", "plotly"}:
                self.skipTest(f"{exc.name} is not installed in this environment")
            raise

        ecs_df = pd.DataFrame([
            self._latency_row("2026-05-01T00:00:00Z", "p50", 1.0, "ClusterName=c;ServiceName=s;TaskId=a"),
            self._latency_row("2026-05-01T00:00:00Z", "p99", 9.0, "ClusterName=c;ServiceName=s;TaskId=a"),
            self._latency_row("2026-05-01T00:00:00Z", "p99.9", 19.0, "ClusterName=c;ServiceName=s;TaskId=a"),
        ])
        ecs_df["Timestamp"] = pd.to_datetime(ecs_df["Timestamp"], utc=True).dt.tz_localize(None)

        figure = build_infra_figure(ecs_df, pd.DataFrame(), "cluster-a", {})
        empty_figure = build_infra_figure(pd.DataFrame(), pd.DataFrame(), "cluster-a", {})

        self.assertEqual(
            {trace.name for trace in figure.data},
            {
                "ECS task p50",
                "ECS task p99",
                "ECS task p99.9",
                "Worst ECS task p99",
                "Worst ECS task p99.9",
            },
        )
        self.assertIn(
            "ECS Task Latency (ms)",
            {annotation.text for annotation in figure.layout.annotations},
        )
        self.assertIn(
            "No ECS task latency datapoints",
            {annotation.text for annotation in empty_figure.layout.annotations},
        )

    def test_rendered_report_does_not_add_latency_cards(self):
        try:
            import pandas as pd

            from cards import stat_cards_html
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        ecs_df = pd.DataFrame([
            self._latency_row("2026-05-01T00:00:00Z", "p50", 1.0, "ClusterName=c;ServiceName=s"),
        ])

        html = stat_cards_html(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), ecs_df)

        self.assertNotIn("Client Latency", html)
        self.assertNotIn("p99.9", html)


if __name__ == "__main__":
    unittest.main()
