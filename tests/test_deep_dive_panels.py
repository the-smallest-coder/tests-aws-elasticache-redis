"""Readable single-run Deep-Dive panels keep the exported metrics visible."""

import sys
import unittest
from pathlib import Path


REPORTER_DIR = str(Path(__file__).resolve().parents[1] / 'reporter')
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


class DeepDivePanelTests(unittest.TestCase):
    def test_metric_panels_have_independent_space_legends_and_exact_hover_time(self):
        import pandas as pd

        from charts import build_elasticache_deep_dive_panels

        start = pd.Timestamp('2026-09-09T10:10:00.123456Z')
        end = pd.Timestamp('2026-09-09T11:10:00.654321Z')
        rows = []
        for timestamp, value in ((start, 1.0), (end, 2.0)):
            for dimensions in ('CacheClusterId=cluster-a',
                               'CacheClusterId=cluster-a;CacheNodeId=0001'):
                for stat in ('Average', 'Maximum', 'SampleCount'):
                    rows.append({
                        'Timestamp': timestamp, 'Namespace': 'AWS/ElastiCache',
                        'MetricName': 'ActiveDefragHits', 'Stat': stat,
                        'Value': 7.0 if stat == 'SampleCount' else value,
                        'Unit': 'Count', 'Dimensions': dimensions,
                    })
            rows.append({
                'Timestamp': timestamp, 'Namespace': 'AWS/ElastiCache',
                'MetricName': 'NetworkBandwidthInAllowanceExceeded', 'Stat': 'Sum',
                'Value': 0.0, 'Unit': 'Count', 'Dimensions': 'CacheClusterId=cluster-a',
            })
        panels = build_elasticache_deep_dive_panels(
            pd.DataFrame(rows), 'cluster-a', x_min=start, x_max=end,
        )
        by_title = {panel['title']: panel for panel in panels}
        supplemental = by_title['ActiveDefragHits']
        self.assertEqual(supplemental['group'], 'Memory and keys')
        self.assertEqual(supplemental['figure'].layout.height, 320)
        self.assertEqual(len(supplemental['figure'].data), 1)
        self.assertEqual(len(supplemental['legend_items']), 1)
        self.assertEqual(supplemental['legend_items'][0]['name'], 'Average')
        self.assertEqual(
            list(supplemental['figure'].data[0].customdata[0]),
            ['2026-09-09 10:10:00.123456 UTC', '7'],
        )
        self.assertIn('Samples: %{customdata[1]}',
                      supplemental['figure'].data[0].hovertemplate)
        self.assertEqual(
            list(supplemental['figure'].layout.xaxis.ticktext),
            ['2026-09-09<br>10:10:00 UTC', '2026-09-09<br>11:10:00 UTC'],
        )
        throttle = by_title['Network Throttling Events / min']
        self.assertEqual(len(throttle['figure'].data), 1)
        self.assertEqual(throttle['legend_items'][0]['name'], 'BW In exceeded')

    def test_multiple_nodes_keep_short_distinct_legend_labels(self):
        import pandas as pd

        from charts import build_elasticache_deep_dive_panels

        timestamp = pd.Timestamp('2026-09-09T10:10:00Z')
        rows = [{
            'Timestamp': timestamp, 'Namespace': 'AWS/ElastiCache',
            'MetricName': 'CacheHits', 'Stat': 'Average',
            'Value': value, 'Unit': 'Count',
            'Dimensions': f'CacheClusterId=cluster-a-{cache};CacheNodeId=0001',
        } for cache, value in (('001', 5), ('002', 9))]
        panels = build_elasticache_deep_dive_panels(pd.DataFrame(rows), 'cluster-a')
        cache_hits = next(panel for panel in panels if panel['title'] == 'CacheHits')
        self.assertEqual(
            [item['name'] for item in cache_hits['legend_items']],
            ['Average — cache 001 / node 0001', 'Average — cache 002 / node 0001'],
        )

    def test_empty_metrics_has_a_renderable_panel(self):
        import pandas as pd

        from charts import build_elasticache_deep_dive_panels

        panels = build_elasticache_deep_dive_panels(pd.DataFrame(), 'cluster-a')
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]['figure'].layout.annotations[0].text,
                         'No ElastiCache metrics available')


if __name__ == '__main__':
    unittest.main()
