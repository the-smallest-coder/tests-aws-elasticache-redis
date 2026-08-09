import sys
import unittest
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


class SingleReportEscapingTests(unittest.TestCase):
    def test_render_html_escapes_plain_text_header_fields(self):
        from template import render_html

        cluster_id = "rg<bad>&\"'"
        suffix = "run<one>&\"'"
        id_label = "ID<label>&\"'"
        time_range = "2026-05-01 < 2026-05-02 & \"quoted\""

        page = render_html(
            cluster_id=cluster_id,
            suffix=suffix,
            id_label=id_label,
            time_range=time_range,
            pills_html="",
            cards_html="",
            chart_memtier_html="",
            chart_infra_html="",
            chart_deep_dive_html="",
        )

        self.assertIn(escape(cluster_id, quote=True), page)
        self.assertIn(
            f"{escape(id_label, quote=True)}: {escape(cluster_id, quote=True)}",
            page,
        )
        self.assertIn(f"Run: {escape(suffix, quote=True)}", page)
        self.assertIn(escape(time_range, quote=True), page)
        self.assertNotIn(cluster_id, page)
        self.assertNotIn(suffix, page)
        self.assertNotIn(time_range, page)

    def test_header_pills_escape_config_values(self):
        from cards import header_pills

        html = header_pills(
            {
                "engine_type": "redis<script>",
                "engine_version": "7&\"'",
                "node_type": "cache.m7g.large</span>",
                "node_memory_bytes": "6850472837",
                "node_hourly_usd": "0.158",
                "node_count": "3",
                "cluster_mode": "true",
            }
        )

        self.assertIn("redis&lt;script&gt;", html)
        self.assertIn("7&amp;&quot;&#x27;", html)
        self.assertIn("cache.m7g.large&lt;/span&gt;", html)
        self.assertIn("Node memory: <span>6.38 GiB</span>", html)
        self.assertIn("hourly: <span>$0.158</span>", html)
        self.assertNotIn("redis<script>", html)
        self.assertNotIn("cache.m7g.large</span>", html)

    def test_validity_is_inside_infrastructure_without_separate_latency_section(self):
        from template import render_html

        page = render_html(
            cluster_id="cluster-a",
            suffix="run-a",
            id_label="Cluster",
            time_range="2026-05-01 00:00:00 UTC - 2026-05-01 01:00:00 UTC",
            pills_html="",
            cards_html="",
            chart_memtier_html="MEMTIER_CHART",
            chart_infra_html="INFRA_WITH_ECS_TASK_LATENCY",
            chart_deep_dive_html="DEEP_DIVE_CHART",
            loadgen_quality_html="LOADGEN_VALIDITY",
        )

        infrastructure_start = page.index("<h2>Infrastructure</h2>")
        infrastructure_end = page.index("<h2>ElastiCache Deep-Dive</h2>")
        infrastructure_group_start = page.rfind(
            '<div class="chart-group">', 0, infrastructure_start
        )
        next_group_start = page.find('<div class="chart-group">', infrastructure_start)
        infrastructure_group = page[infrastructure_group_start:next_group_start]
        self.assertIn("INFRA_WITH_ECS_TASK_LATENCY", infrastructure_group)
        self.assertIn("LOADGEN_VALIDITY", infrastructure_group)
        self.assertIn(
            '<div class="chart-wrap">INFRA_WITH_ECS_TASK_LATENCYLOADGEN_VALIDITY</div>',
            infrastructure_group,
        )
        self.assertNotIn("<h2>ECS Load-Generator Latency</h2>", page)

    def test_stat_cards_escape_titles_and_text(self):
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        from cards import stat_cards_html

        metrics_df = pd.DataFrame(
            [
                {
                    "Timestamp": pd.Timestamp("2026-05-01T00:00:00"),
                    "Namespace": "AWS/ElastiCache",
                    "MetricName": "CacheHitRate",
                    "Stat": "Average",
                    "Value": 65.0,
                    "Unit": "Percent",
                    "Dimensions": "CacheClusterId=cluster-a",
                }
            ]
        )

        html = stat_cards_html(
            memtier_minute_df=pd.DataFrame(),
            memtier_totals_df=pd.DataFrame(),
            metrics_df=metrics_df,
            ecs_df=pd.DataFrame(),
            config={
                "node_memory_bytes": "6850472837",
                "node_hourly_usd": "0.158",
                "engine_type": "redis",
            },
        )

        self.assertIn("<div class='card-label'>Node Memory</div>", html)
        self.assertIn(">6.38<span class='card-unit'>GiB</span>", html)
        self.assertIn("<div class='card-label'>Redis Cost</div>", html)
        self.assertIn(">$0.158<span class='card-unit'>/h</span>", html)
        self.assertIn("&lt;70% = significant", html)
        self.assertNotIn("<70% = significant", html)


if __name__ == "__main__":
    unittest.main()
