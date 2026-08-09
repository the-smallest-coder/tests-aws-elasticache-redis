"""
Fetch ElastiCache CacheClusterId-dimension metrics missing from the existing CSV
and append them to it.

Usage:
    python reporter/fetch_missing_metrics.py \
        --csv results/20260227-140039/metrics/elasticache-perf-test-redis-27125324.csv \
        --cluster-id elasticache-perf-test-redis-27125324-001 \
        --start 2026-02-27T12:57:00 \
        --end   2026-02-27T13:59:00
"""

import argparse
import boto3
import csv
import sys
from datetime import datetime, timezone

STATISTICS = ['Average', 'Sum', 'Maximum', 'Minimum']
NAMESPACE   = 'AWS/ElastiCache'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv',        required=True,  help='Path to existing metrics CSV')
    parser.add_argument('--cluster-id', required=True,  help='CacheClusterId value (e.g. …-001)')
    parser.add_argument('--start',      required=True,  help='ISO start time UTC (e.g. 2026-02-27T12:57:00)')
    parser.add_argument('--end',        required=True,  help='ISO end time UTC (e.g. 2026-02-27T13:59:00)')
    args = parser.parse_args()

    start_time = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_time   = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    dimensions = [{'Name': 'CacheClusterId', 'Value': args.cluster_id}]
    dims_str   = f'CacheClusterId={args.cluster_id}'

    cw = boto3.client('cloudwatch')

    # List all metrics for this CacheClusterId
    metric_names = []
    paginator = cw.get_paginator('list_metrics')
    for page in paginator.paginate(Namespace=NAMESPACE, Dimensions=dimensions):
        for m in page['Metrics']:
            metric_names.append(m['MetricName'])
    metric_names = sorted(set(metric_names))
    print(f"Found {len(metric_names)} metrics for {dims_str}: {metric_names}")

    rows = []
    for metric_name in metric_names:
        try:
            response = cw.get_metric_statistics(
                Namespace=NAMESPACE,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=STATISTICS
            )
        except Exception as e:
            print(f"  SKIP {metric_name}: {e}", file=sys.stderr)
            continue

        for dp in sorted(response.get('Datapoints', []), key=lambda d: d['Timestamp']):
            ts   = dp['Timestamp'].astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
            unit = dp.get('Unit', 'None')
            for stat in STATISTICS:
                if stat in dp:
                    rows.append([ts, NAMESPACE, metric_name, stat, dp[stat], unit, dims_str])

        print(f"  {metric_name}: {len(response.get('Datapoints',[]))} datapoints")

    # Append to existing CSV (no header - it already has one)
    with open(args.csv, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"\nAppended {len(rows)} rows to {args.csv}")


if __name__ == '__main__':
    main()
