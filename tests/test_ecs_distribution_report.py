import re
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

            from charts import build_infra_figure, build_infra_panels
            from report_generator import _infra_panels_html
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

        figure = build_infra_figure(
            ecs_df,
            pd.DataFrame(),
            "cluster-a",
            {},
            task_az_map={"a": "us-east-1e", "b": "us-east-1f"},
        )
        trace_names = {trace.name for trace in figure.data}
        subplot_titles = {annotation.text for annotation in figure.layout.annotations}

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
        self.assertIn("ECS Tasks — CPU Across Tasks (%)", subplot_titles)
        self.assertIn("us-east-1e — 1 tasks", subplot_titles)
        self.assertIn("us-east-1f — 1 tasks", subplot_titles)
        self.assertIn("1", trace_names)
        self.assertIn("2", trace_names)
        task_a = next(trace for trace in figure.data if trace.name == "1")
        task_b = next(trace for trace in figure.data if trace.name == "2")
        self.assertEqual(task_a.legend, "legend2")
        self.assertEqual(task_b.legend, "legend3")
        self.assertFalse(figure.layout.showlegend)
        reference_levels = [shape.y0 for shape in figure.layout.shapes]
        self.assertEqual(reference_levels.count(85), 3)
        self.assertEqual(reference_levels.count(100), 3)

        panels = build_infra_panels(
            ecs_df,
            pd.DataFrame(),
            "cluster-a",
            {},
            task_az_map={"a": "us-east-1e", "b": "us-east-1f"},
        )
        self.assertEqual(len(panels), 7)
        self.assertTrue(all(panel["figure"].layout.height == 320 for panel in panels))
        self.assertTrue(all(panel["figure"].layout.showlegend is False for panel in panels))
        self.assertEqual(
            [item["name"] for item in panels[1]["legend_items"]],
            ["1"],
        )
        self.assertEqual(
            [item["name"] for item in panels[2]["legend_items"]],
            ["2"],
        )
        panels_html = _infra_panels_html(panels)
        self.assertEqual(panels_html.count("class='infra-panel'"), 7)
        self.assertEqual(panels_html.count("class='infra-legend'"), 7)
        self.assertIn("class='infra-legend-item'", panels_html)

    def test_infrastructure_component_defines_every_emitted_class(self):
        try:
            from report_generator import _infra_panels_html
        except ModuleNotFoundError as exc:
            if exc.name in {"pandas", "plotly"}:
                self.skipTest(f"{exc.name} is not installed in this environment")
            raise

        class StubFigure:
            @staticmethod
            def to_html(**_kwargs):
                return "<div></div>"

        rendered = _infra_panels_html([{
            "title": "us-east-1e — 1 task",
            "figure": StubFigure(),
            "legend_items": [
                {"name": "solid", "color": "#123456", "dash": "solid"},
                {"name": "dashed", "color": "#abcdef", "dash": "dash"},
                {"name": "dotted", "color": "#654321", "dash": "dot"},
                {"name": "dash-dot", "color": "#fedcba", "dash": "dashdot"},
            ],
        }])
        style_match = re.search(
            r"<style data-component='infra-panels'>(.*?)</style>",
            rendered,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(style_match, "Infrastructure component CSS is missing")
        css = style_match.group(1)
        emitted_classes = {
            class_name
            for class_attr in re.findall(r"class='([^']*)'", rendered)
            for class_name in class_attr.split()
            if class_name.startswith("infra-") or class_name in {"dash", "dot", "dashdot"}
        }
        missing_rules = sorted(
            class_name
            for class_name in emitted_classes
            if not re.search(rf"\.{re.escape(class_name)}(?![-\w])", css)
        )
        self.assertEqual(missing_rules, [], f"Missing component CSS rules: {missing_rules}")

    def test_engine_cpu_selects_cluster_dimension_for_single_node(self):
        try:
            import pandas as pd

            from charts import build_infra_figure
            from helpers import select_mem_dims
        except ModuleNotFoundError as exc:
            if exc.name in {"pandas", "plotly"}:
                self.skipTest(f"{exc.name} is not installed in this environment")
            raise

        aggregate = "CacheClusterId=cluster-a"
        cache_node = "CacheClusterId=cluster-a;CacheNodeId=0001"
        self.assertEqual(select_mem_dims([aggregate, cache_node], 1), [aggregate])
        shard_a = "CacheClusterId=cluster-a;NodeGroupId=0001"
        shard_b = "CacheClusterId=cluster-a;NodeGroupId=0002"
        self.assertEqual(select_mem_dims([shard_a, shard_b], None), [shard_a, shard_b])

        metrics_df = pd.DataFrame([
            self._metric_row(
                "2026-05-01T00:00:00Z",
                "EngineCPUUtilization",
                "Average",
                value,
                dimensions,
            )
            for value, dimensions in ((50.0, aggregate), (50.0, cache_node))
        ])
        metrics_df["Namespace"] = "AWS/ElastiCache"
        metrics_df["Timestamp"] = pd.to_datetime(
            metrics_df["Timestamp"], utc=True
        ).dt.tz_localize(None)

        figure = build_infra_figure(
            pd.DataFrame(), metrics_df, "cluster-a", {"node_count": 1}
        )
        engine_cpu_traces = [
            trace for trace in figure.data if trace.name.startswith("EngineCPU")
        ]
        self.assertEqual(len(engine_cpu_traces), 1)

    def test_infra_panels_carry_empty_state_annotations_for_missing_data(self):
        """Regression: build_infra_panels copies each empty-state message
        ("No ..." annotations on the combined figure) into the matching
        per-panel figure. The original defect survived because the old test
        only ever checked the combined figure - which never stopped having
        the annotation - not whether it made it into the split-out panel
        the report actually renders.
        """
        try:
            import pandas as pd

            from charts import build_infra_panels
        except ModuleNotFoundError as exc:
            if exc.name in {"pandas", "plotly"}:
                self.skipTest(f"{exc.name} is not installed in this environment")
            raise

        ecs_df = pd.DataFrame([
            self._metric_row(
                "2026-05-01T00:00:00Z",
                "TaskCpuUtilization",
                "Average",
                80.0,
                "ClusterName=c;ServiceName=s;TaskId=a;AvailabilityZone=us-east-1e",
            )
        ])
        ecs_df["Timestamp"] = pd.to_datetime(
            ecs_df["Timestamp"], utc=True
        ).dt.tz_localize(None)

        # metrics_df is empty and there's no NetworkTxBytes/latency EMF data,
        # so every panel besides "CPU Across Tasks" and the one AZ panel
        # should fall back to its empty-state message.
        panels = build_infra_panels(ecs_df, pd.DataFrame(), "cluster-a", {})
        panels_by_title = {panel["title"]: panel for panel in panels}

        expected = {
            "ECS Tasks — Network TX (KB/min)": "No Network TX Metrics",
            "ECS Tasks — Memory (MB)": "No ECS Memory Metrics",
            "ElastiCache Memory Usage (%)": "No CloudWatch Metrics",
            "ECS Task Latency (ms)": "No ECS task latency datapoints",
        }
        for title, expected_text in expected.items():
            self.assertIn(title, panels_by_title, f"missing panel: {title}")
            annotation_texts = [
                annotation.text
                for annotation in (panels_by_title[title]["figure"].layout.annotations or ())
            ]
            self.assertIn(
                expected_text,
                annotation_texts,
                f"panel {title!r} did not receive its empty-state annotation",
            )

    def test_infra_figure_omits_az_panels_when_task_az_is_unknown(self):
        try:
            import pandas as pd

            from charts import build_infra_figure
        except ModuleNotFoundError as exc:
            if exc.name in {"pandas", "plotly"}:
                self.skipTest(f"{exc.name} is not installed in this environment")
            raise

        ecs_df = pd.DataFrame([
            self._metric_row(
                "2026-05-01T00:00:00Z",
                "TaskCpuUtilization",
                "Average",
                80.0,
                "ClusterName=c;ServiceName=s;TaskId=a",
            )
        ])
        ecs_df["Timestamp"] = pd.to_datetime(
            ecs_df["Timestamp"], utc=True
        ).dt.tz_localize(None)

        figure = build_infra_figure(ecs_df, pd.DataFrame(), "cluster-a", {})
        subplot_titles = [annotation.text for annotation in figure.layout.annotations]

        self.assertEqual(len([title for title in subplot_titles if "tasks" in title]), 0)


if __name__ == "__main__":
    unittest.main()
