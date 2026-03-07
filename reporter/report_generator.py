"""ElastiCache performance report generator — main entry-point.

Usage (local):
  python report_generator.py \
    --metrics-csv results/.../metrics/cluster.csv \
    --ecs-metrics-csv results/.../metrics/cluster-ecs.csv \
    --logs-txt results/.../logs/cluster.txt \
    --output results/.../report.html \
    --cluster-id my-cluster

All flags fall back to environment variables for ECS/Fargate mode.
"""

import argparse
import json
import os
import re
import sys

import pandas as pd

from helpers import read_file_content, resample_logs
from parsers import parse_memtier_logs, parse_memtier_extra_stats, parse_metrics_csv
from charts import build_memtier_figure, build_infra_figure, build_elasticache_deep_dive_figure
from cards import header_pills, stat_cards_html
from template import render_html
from summary import build_summary


# ------------------------------------------------------------------ #
#  Report orchestration                                                #
# ------------------------------------------------------------------ #

def create_report(metrics_df, logs_df, cluster_id, suffix,
                  ecs_metrics_df=None, config=None, extra_stats=None):
    """Build the full HTML report string from parsed DataFrames."""
    ecs_df = ecs_metrics_df if ecs_metrics_df is not None else pd.DataFrame()
    config = config or {}
    extra_stats = extra_stats or {}

    # Resample dense log data to 1-minute averages
    logs_resampled = resample_logs(logs_df)

    # Benchmark time window
    if not logs_resampled.empty:
        x_min = logs_resampled['Timestamp'].min()
        x_max = logs_resampled['Timestamp'].max()
    else:
        x_min = x_max = None

    oom_df = extra_stats.get('oom_df', pd.DataFrame())

    # Build chart figures
    fig_m = build_memtier_figure(logs_resampled, oom_df, metrics_df, x_min, x_max)
    fig_i = build_infra_figure(ecs_df, metrics_df, cluster_id, config)
    fig_d = build_elasticache_deep_dive_figure(metrics_df, cluster_id, config)

    chart_memtier    = fig_m.to_html(include_plotlyjs='cdn', full_html=False)
    chart_infra      = fig_i.to_html(include_plotlyjs=False, full_html=False)
    chart_deep_dive  = fig_d.to_html(include_plotlyjs=False, full_html=False)

    # Compute time range string from all data sources
    time_range = _compute_time_range(logs_df, metrics_df, ecs_df)

    cluster_mode = str(config.get('cluster_mode', 'false')).lower() == 'true'
    id_label = 'Cluster' if cluster_mode else 'Replication Group'

    summary = build_summary(metrics_df, logs_df, ecs_df, extra_stats, config, cluster_id, time_range)
    summary_json = json.dumps(summary, indent=2, default=str)

    html = render_html(
        cluster_id=cluster_id,
        suffix=suffix,
        id_label=id_label,
        time_range=time_range,
        pills_html=header_pills(config),
        cards_html=stat_cards_html(logs_df, metrics_df, ecs_df, extra_stats, config),
        chart_memtier_html=chart_memtier,
        chart_infra_html=chart_infra,
        chart_deep_dive_html=chart_deep_dive,
    )
    return html, summary_json


def _compute_time_range(logs_df, metrics_df, ecs_df):
    """Return a human-readable time-range string spanning all data sources."""
    all_timestamps = []
    for df in (logs_df, metrics_df, ecs_df):
        if not df.empty and 'Timestamp' in df.columns:
            ts_min = df['Timestamp'].min()
            ts_max = df['Timestamp'].max()
            if hasattr(ts_min, 'tzinfo') and ts_min.tzinfo is not None:
                ts_min = ts_min.replace(tzinfo=None)
                ts_max = ts_max.replace(tzinfo=None)
            all_timestamps.extend([ts_min, ts_max])
    if not all_timestamps:
        return ''
    t0 = min(all_timestamps)
    t1 = max(all_timestamps)
    duration_min = int((t1 - t0).total_seconds() / 60)
    return f"{t0.strftime('%Y-%m-%d %H:%M')} – {t1.strftime('%H:%M')} ({duration_min} min)"


# ------------------------------------------------------------------ #
#  CLI                                                                 #
# ------------------------------------------------------------------ #

def parse_args():
    """Parse CLI arguments, falling back to environment variables for ECS usage."""
    parser = argparse.ArgumentParser(
        description='Generate HTML performance report from ElastiCache test results.',
        epilog=(
            'Local example:\n'
            '  python report_generator.py \\\n'
            '    --metrics-csv results/20260227-140039/metrics/cluster.csv \\\n'
            '    --ecs-metrics-csv results/20260227-140039/metrics/cluster-ecs.csv \\\n'
            '    --logs-txt results/20260227-140039/logs/cluster.txt \\\n'
            '    --output results/20260227-140039/report.html \\\n'
            '    --cluster-id my-cluster'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--metrics-csv', default=os.environ.get('METRICS_CSV'),
                        help='ElastiCache metrics CSV (local path or s3:// URI). Env: METRICS_CSV')
    parser.add_argument('--ecs-metrics-csv', default=os.environ.get('ECS_METRICS_CSV', ''),
                        help='ECS metrics CSV (local path or s3:// URI, optional). Env: ECS_METRICS_CSV')
    parser.add_argument('--logs-txt', default=os.environ.get('LOGS_TXT'),
                        help='Memtier log file (local path or s3:// URI). Env: LOGS_TXT')
    parser.add_argument('--output', default=None,
                        help='Local output HTML path. If omitted, uploads to S3 using OUTPUT_BUCKET.')
    parser.add_argument('--output-bucket', default=os.environ.get('OUTPUT_BUCKET'),
                        help='S3 bucket for report upload (ECS mode). Env: OUTPUT_BUCKET')
    parser.add_argument('--output-prefix', default=os.environ.get('OUTPUT_PREFIX', ''),
                        help='S3 key prefix. Env: OUTPUT_PREFIX')
    parser.add_argument('--suffix', default=os.environ.get('SUFFIX', 'report'),
                        help='Report filename suffix. Env: SUFFIX')
    parser.add_argument('--cluster-id', default=os.environ.get('CLUSTER_ID', 'Unknown'),
                        help='ElastiCache replication group ID. Env: CLUSTER_ID')
    parser.add_argument('--cluster-mode', default=os.environ.get('CLUSTER_MODE', 'false'),
                        help='Whether cluster mode is enabled (true/false). Env: CLUSTER_MODE')
    parser.add_argument('--engine-type', default=os.environ.get('ENGINE_TYPE', ''),
                        help='Engine type (e.g. redis). Env: ENGINE_TYPE')
    parser.add_argument('--engine-version', default=os.environ.get('ENGINE_VERSION', ''),
                        help='Engine version. Env: ENGINE_VERSION')
    parser.add_argument('--node-type', default=os.environ.get('NODE_TYPE', ''),
                        help='ElastiCache node type. Env: NODE_TYPE')
    parser.add_argument('--node-count', default=os.environ.get('NODE_COUNT', ''),
                        help='Number of nodes. Env: NODE_COUNT')
    return parser.parse_args()


def main():
    try:
        args = parse_args()

        if not args.metrics_csv:
            print("Error: --metrics-csv (or METRICS_CSV env) is required")
            sys.exit(1)
        if not args.logs_txt:
            print("Error: --logs-txt (or LOGS_TXT env) is required")
            sys.exit(1)
        if not args.output and not args.output_bucket:
            print("Error: --output (local) or --output-bucket (S3) is required")
            sys.exit(1)

        print(f"Starting report generation for {args.cluster_id}")

        # --- Load ElastiCache metrics ---
        print(f"Reading metrics from {args.metrics_csv}")
        try:
            metrics_content = read_file_content(args.metrics_csv)
        except Exception as e:
            print(f"Error: Failed to read metrics from {args.metrics_csv}: {e}")
            sys.exit(1)
        metrics_df = parse_metrics_csv(metrics_content)

        # --- Load memtier logs ---
        print(f"Reading logs from {args.logs_txt}")
        try:
            logs_content = read_file_content(args.logs_txt)
        except Exception as e:
            print(f"Error: Failed to read logs from {args.logs_txt}: {e}")
            sys.exit(1)
        logs_df = parse_memtier_logs(logs_content)
        extra_stats = parse_memtier_extra_stats(logs_content)

        # --- Load ECS metrics (optional) ---
        ecs_metrics_df = pd.DataFrame()
        if args.ecs_metrics_csv:
            print(f"Reading ECS metrics from {args.ecs_metrics_csv}")
            try:
                ecs_content = read_file_content(args.ecs_metrics_csv)
                ecs_metrics_df = parse_metrics_csv(ecs_content)
            except Exception as e:
                print(f"Warning: Failed to read ECS metrics: {e}, continuing without them.")

        # --- Generate report ---
        print("Generating report...")
        config = {
            'engine_type': args.engine_type,
            'engine_version': args.engine_version,
            'node_type': args.node_type,
            'node_count': args.node_count,
            'cluster_mode': args.cluster_mode,
        }
        html_content, summary_json = create_report(
            metrics_df, logs_df, args.cluster_id, args.suffix,
            ecs_metrics_df, config, extra_stats=extra_stats,
        )

        # --- Write output ---
        if args.output:
            os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"Report written to {args.output}")

            json_path = re.sub(r'\.html$', '.json', args.output)
            if json_path == args.output:
                json_path = args.output + '.json'
            with open(json_path, 'w', encoding='utf-8') as f:
                f.write(summary_json)
            print(f"Summary JSON written to {json_path}")
        else:
            import boto3
            timestamp_match = re.search(r'\d{8}-\d{6}', args.metrics_csv or '')
            if timestamp_match:
                timestamp = timestamp_match.group(0)
                output_key      = f"{args.output_prefix}{timestamp}/results_{args.suffix}.html"
                output_json_key = f"{args.output_prefix}{timestamp}/results_{args.suffix}.json"
            else:
                output_key      = f"{args.output_prefix}results_{args.suffix}.html"
                output_json_key = f"{args.output_prefix}results_{args.suffix}.json"

            s3 = boto3.client('s3')
            print(f"Uploading report to s3://{args.output_bucket}/{output_key}")
            s3.put_object(
                Bucket=args.output_bucket,
                Key=output_key,
                Body=html_content,
                ContentType='text/html',
            )
            print(f"Uploading summary JSON to s3://{args.output_bucket}/{output_json_key}")
            s3.put_object(
                Bucket=args.output_bucket,
                Key=output_json_key,
                Body=summary_json,
                ContentType='application/json',
            )

        print("Report generation complete.")

    except Exception as e:
        print(f"Error generating report: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
