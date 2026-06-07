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
            chart_client_latency_html="",
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
                "node_count": "3",
                "cluster_mode": "true",
            }
        )

        self.assertIn("redis&lt;script&gt;", html)
        self.assertIn("7&amp;&quot;&#x27;", html)
        self.assertIn("cache.m7g.large&lt;/span&gt;", html)
        self.assertNotIn("redis<script>", html)
        self.assertNotIn("cache.m7g.large</span>", html)

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
        )

        self.assertIn("&lt;70% = significant", html)
        self.assertNotIn("<70% = significant", html)


if __name__ == "__main__":
    unittest.main()
