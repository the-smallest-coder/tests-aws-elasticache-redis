import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


class EcsDistributionReportTests(unittest.TestCase):
    @staticmethod
    def _metric_row(timestamp, metric, stat, value, dimensions):
        return {
            "Timestamp": timestamp,
            "Namespace": "ECS/ContainerInsights",
            "MetricName": metric,
            "Stat": stat,
            "Value": value,
            "Unit": "None",
            "Dimensions": dimensions,
        }

    def test_task_distribution_dedupes_dimension_sets_and_counts_sources(self):
        try:
            import pandas as pd

            from helpers import ecs_task_metric_distribution
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        rows = [
            self._metric_row("2026-05-01T00:00:00Z", "TaskCpuUtilization", "Average", 20.0,
                             "ClusterName=c;ServiceName=s;TaskId=a"),
            self._metric_row("2026-05-01T00:00:00Z", "TaskCpuUtilization", "Average", 80.0,
                             "ClusterName=c;ServiceName=s;TaskId=b"),
            self._metric_row("2026-05-01T00:00:00Z", "TaskCpuUtilization", "Average", 999.0,
                             "ClusterName=c;TaskDefinitionFamily=f;TaskId=a"),
            self._metric_row("2026-05-01T00:00:00Z", "TaskCpuUtilization", "Average", 999.0,
                             "ClusterName=c"),
            self._metric_row("2026-05-01T00:01:00Z", "TaskCpuUtilization", "Average", 30.0,
                             "ClusterName=c;TaskDefinitionFamily=f;TaskId=a"),
            self._metric_row("2026-05-01T00:01:00Z", "TaskCpuUtilization", "Average", 70.0,
                             "ClusterName=c;ServiceName=s;TaskId=b"),
        ]
        ecs_df = pd.DataFrame(rows)
        ecs_df["Timestamp"] = pd.to_datetime(ecs_df["Timestamp"], utc=True).dt.tz_localize(None)

        dist = ecs_task_metric_distribution(ecs_df, "TaskCpuUtilization", "Average")

        self.assertEqual(dist["source_count"].tolist(), [2, 2])
        self.assertEqual(dist["avg"].tolist(), [50.0, 50.0])
        self.assertEqual(dist["median"].tolist(), [50.0, 50.0])
        self.assertEqual(dist["min"].tolist(), [20.0, 30.0])
        self.assertEqual(dist["max"].tolist(), [80.0, 70.0])
        self.assertEqual(dist["sum"].tolist(), [100.0, 100.0])

    def test_infra_figure_exposes_cpu_and_network_distribution_traces(self):
        try:
            import pandas as pd

            from charts import build_infra_figure
        except ModuleNotFoundError as exc:
            if exc.name in {"pandas", "plotly"}:
                self.skipTest(f"{exc.name} is not installed in this environment")
            raise

        rows = []
        for task_id, cpu, tx_bytes in (("a", 20.0, 1024.0), ("b", 80.0, 3072.0)):
            dims = f"ClusterName=c;ServiceName=s;TaskId={task_id}"
            rows.append(self._metric_row("2026-05-01T00:00:00Z", "TaskCpuUtilization", "Average", cpu, dims))
            rows.append(self._metric_row("2026-05-01T00:00:00Z", "NetworkTxBytes", "Sum", tx_bytes, dims))
        ecs_df = pd.DataFrame(rows)
        ecs_df["Timestamp"] = pd.to_datetime(ecs_df["Timestamp"], utc=True).dt.tz_localize(None)

        figure = build_infra_figure(ecs_df, pd.DataFrame(), "cluster-a", {})
        trace_names = {trace.name for trace in figure.data}

        self.assertLessEqual(
            {
                "CPU avg/task",
                "CPU median/task",
                "CPU min task",
                "CPU max task",
                "CPU source count",
                "Network TX avg/task",
                "Network TX median/task",
                "Network TX min task",
                "Network TX max task",
                "Network TX source count",
            },
            trace_names,
        )


if __name__ == "__main__":
    unittest.main()
