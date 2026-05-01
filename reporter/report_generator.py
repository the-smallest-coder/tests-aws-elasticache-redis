from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from report_common import ECS_ENV_VARS
from report_compare import run_compare_report
from helpers import read_file_content, resample_logs
from parsers import parse_metrics_csv, parse_memtier_logs, parse_memtier_extra_stats
from summary import build_summary
from cards import header_pills, stat_cards_html
from charts import build_memtier_figure, build_infra_figure, build_elasticache_deep_dive_figure
from template import render_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate ElastiCache reports. No args uses ECS env vars; local comparison uses the compare command.",
    )
    subparsers = parser.add_subparsers(dest="command")
    compare = subparsers.add_parser("compare", help="Compare two local results_local.json runs.")
    compare.add_argument("baseline", help="Baseline results_local.json path or its parent run directory.")
    compare.add_argument("candidate", help="Candidate results_local.json path or its parent run directory.")
    compare.add_argument(
        "-o",
        "--output",
        help="Output HTML path. Defaults to results/comparisons/<baseline>_vs_<candidate>.html.",
    )
    generate = subparsers.add_parser("generate", help="Build results_local.json from local CSVs and logs.")
    generate.add_argument("run_dir", help="Path to a run results directory (containing metrics/ and logs/).")
    generate.add_argument("--engine-type", default="", help="e.g. redis or valkey")
    generate.add_argument("--engine-version", default="", help="e.g. 7.1")
    generate.add_argument("--node-type", default="", help="e.g. cache.t4g.micro")
    generate.add_argument("--node-count", default="", help="e.g. 1")
    generate.add_argument("--cluster-mode", default="false", help="true or false")
    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    if argv and argv[0] not in {"compare", "generate"} and not argv[0].startswith("-"):
        return ["compare", *argv]
    return argv


def missing_ecs_env_vars() -> list[str]:
    return [name for name in ECS_ENV_VARS if not os.environ.get(name)]


def _compute_time_range(*frames) -> str:
    all_timestamps = []
    for df in frames:
        if df is None or df.empty or "Timestamp" not in df.columns:
            continue
        ts_min = df["Timestamp"].min()
        ts_max = df["Timestamp"].max()
        if hasattr(ts_min, "tzinfo") and ts_min.tzinfo is not None:
            ts_min = ts_min.replace(tzinfo=None)
            ts_max = ts_max.replace(tzinfo=None)
        all_timestamps.extend([ts_min, ts_max])
    if not all_timestamps:
        return ""
    t0 = min(all_timestamps)
    t1 = max(all_timestamps)
    duration_min = int((t1 - t0).total_seconds() / 60)
    return f"{t0.strftime('%Y-%m-%d %H:%M')} \u2013 {t1.strftime('%H:%M')} ({duration_min} min)"


def _config_from_env() -> dict[str, str]:
    return {
        "engine_type": os.environ.get("ENGINE_TYPE", ""),
        "engine_version": os.environ.get("ENGINE_VERSION", ""),
        "node_type": os.environ.get("NODE_TYPE", ""),
        "node_count": os.environ.get("NODE_COUNT", ""),
        "cluster_mode": os.environ.get("CLUSTER_MODE", "false"),
    }


def _warn_if_cache_hit_rate_missing(metrics_df, source: str) -> None:
    if metrics_df.empty:
        return
    hit_rate = metrics_df[
        (metrics_df["MetricName"] == "CacheHitRate")
        & (metrics_df["Stat"] == "Average")
        & (metrics_df["Dimensions"].astype(str).str.startswith("CacheClusterId"))
    ]
    if hit_rate.empty:
        print(
            "Warning: CacheHitRate/Average with CacheClusterId dimensions is missing from "
            f"{source}. The old local report plotted this CloudWatch metric directly, "
            "so the cache-hit chart cannot be reproduced until those metrics are present."
        )


def create_report(
    metrics_df,
    logs_df,
    cluster_id: str,
    suffix: str,
    ecs_metrics_df=None,
    config: dict | None = None,
    extra_stats: dict | None = None,
) -> tuple[str, str]:
    """Build the rich HTML report and matching summary JSON.

    This intentionally follows the March 2026 local report pipeline: cache hit
    rate is read from the metrics CSV as CacheHitRate/Average at CacheClusterId
    granularity, then plotted in the memtier chart.
    """
    import pandas as pd

    ecs_df = ecs_metrics_df if ecs_metrics_df is not None else pd.DataFrame()
    config = config or {}
    extra_stats = extra_stats or {}

    logs_resampled = resample_logs(logs_df)
    if not logs_resampled.empty:
        x_min = logs_resampled["Timestamp"].min()
        x_max = logs_resampled["Timestamp"].max()
    else:
        x_min = x_max = None

    oom_df = extra_stats.get("oom_df", pd.DataFrame())

    fig_m = build_memtier_figure(logs_resampled, oom_df, metrics_df, x_min, x_max)
    fig_i = build_infra_figure(ecs_df, metrics_df, cluster_id, config)
    fig_d = build_elasticache_deep_dive_figure(metrics_df, cluster_id, config)

    time_range = _compute_time_range(logs_df, metrics_df, ecs_df)
    cluster_mode = str(config.get("cluster_mode", "false")).lower() == "true"
    id_label = "Cluster" if cluster_mode else "Replication Group"

    summary = build_summary(metrics_df, logs_df, ecs_df, extra_stats, config, cluster_id, time_range)
    summary_json = json.dumps(summary, indent=2, default=str)

    html_content = render_html(
        cluster_id=cluster_id,
        suffix=suffix,
        id_label=id_label,
        time_range=time_range,
        pills_html=header_pills(config),
        cards_html=stat_cards_html(logs_df, metrics_df, ecs_df, extra_stats, config),
        chart_memtier_html=fig_m.to_html(include_plotlyjs="cdn", full_html=False),
        chart_infra_html=fig_i.to_html(include_plotlyjs=False, full_html=False),
        chart_deep_dive_html=fig_d.to_html(include_plotlyjs=False, full_html=False),
    )
    return html_content, summary_json


def run_generate_report(run_dir: str, config: dict) -> None:
    import pandas as pd

    run_path = Path(run_dir)
    metrics_dir = run_path / "metrics"
    logs_dir = run_path / "logs"

    # Find main metrics CSV (not the -ecs one)
    ec_csvs = [p for p in metrics_dir.glob("*.csv") if not p.name.endswith("-ecs.csv")]
    ecs_csvs = list(metrics_dir.glob("*-ecs.csv"))
    if not ec_csvs:
        print(f"No metrics CSV found in {metrics_dir}")
        sys.exit(1)

    cluster_id = ec_csvs[0].stem  # filename without extension = cluster_id
    print(f"Cluster ID: {cluster_id}")

    # Read and parse metrics CSVs
    metrics_df = parse_metrics_csv(ec_csvs[0].read_text(encoding="utf-8"))
    ecs_df = parse_metrics_csv(ecs_csvs[0].read_text(encoding="utf-8")) if ecs_csvs else pd.DataFrame()

    # Find and parse the main loadgen log file (directly in logs/, not in subdirs)
    log_files = [p for p in logs_dir.glob(f"{cluster_id}.txt")]
    if not log_files:
        log_files = [p for p in logs_dir.glob("*.txt")]
    if not log_files:
        print(f"No log file found in {logs_dir}")
        logs_df = pd.DataFrame()
        extra_stats = {}
    else:
        log_content = log_files[0].read_text(encoding="utf-8")
        logs_df = parse_memtier_logs(log_content)
        extra_stats = parse_memtier_extra_stats(log_content)

    time_range = _compute_time_range(logs_df, metrics_df, ecs_df)
    _warn_if_cache_hit_rate_missing(metrics_df, str(ec_csvs[0]))

    summary = build_summary(
        metrics_df=metrics_df,
        logs_df=logs_df,
        ecs_df=ecs_df,
        extra_stats=extra_stats,
        config=config,
        cluster_id=cluster_id,
        time_range=time_range,
    )

    out_path = run_path / "results_local.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Written: {out_path}")

    html_content, _summary_json = create_report(
        metrics_df=metrics_df,
        logs_df=logs_df,
        cluster_id=cluster_id,
        suffix=run_path.name,
        ecs_metrics_df=ecs_df,
        config=config,
        extra_stats=extra_stats,
    )

    html_path = run_path / "results_local.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"Written: {html_path}")


def run_uploaded_report() -> None:
    import boto3
    import pandas as pd

    s3_bucket = os.environ.get("S3_BUCKET", "")
    s3_prefix = os.environ.get("S3_PREFIX", "")
    timestamp = os.environ.get("REPORT_TIMESTAMP") or os.environ.get("SUFFIX", "report")
    cluster_id = os.environ.get("CLUSTER_ID", "Unknown")

    metrics_csv = os.environ.get("METRICS_CSV")
    ecs_metrics_csv = os.environ.get("ECS_METRICS_CSV", "")
    logs_txt = os.environ.get("LOGS_TXT")
    if not metrics_csv and s3_bucket and s3_prefix and timestamp and cluster_id:
        metrics_csv = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/metrics/{cluster_id}.csv"
    if not ecs_metrics_csv and s3_bucket and s3_prefix and timestamp and cluster_id:
        ecs_metrics_csv = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/metrics/{cluster_id}-ecs.csv"
    if not logs_txt and s3_bucket and s3_prefix and timestamp and cluster_id:
        logs_txt = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/logs/{cluster_id}.txt"

    output_bucket = os.environ.get("OUTPUT_BUCKET") or s3_bucket
    output_prefix = os.environ.get("OUTPUT_PREFIX", s3_prefix)
    suffix = os.environ.get("SUFFIX", timestamp)

    if not metrics_csv or not logs_txt or not output_bucket:
        missing = [
            name
            for name, value in {
                "METRICS_CSV": metrics_csv,
                "LOGS_TXT": logs_txt,
                "OUTPUT_BUCKET": output_bucket,
            }.items()
            if not value
        ]
        print(f"Missing ECS report inputs: {', '.join(missing)}")
        sys.exit(2)

    print(f"Reading metrics from {metrics_csv}")
    metrics_df = parse_metrics_csv(read_file_content(metrics_csv))
    _warn_if_cache_hit_rate_missing(metrics_df, metrics_csv)

    print(f"Reading logs from {logs_txt}")
    logs_content = read_file_content(logs_txt)
    logs_df = parse_memtier_logs(logs_content)
    extra_stats = parse_memtier_extra_stats(logs_content)

    ecs_df = pd.DataFrame()
    if ecs_metrics_csv:
        print(f"Reading ECS metrics from {ecs_metrics_csv}")
        try:
            ecs_df = parse_metrics_csv(read_file_content(ecs_metrics_csv))
        except Exception as exc:
            print(f"Warning: failed to read ECS metrics: {exc}")

    html_content, summary_json = create_report(
        metrics_df=metrics_df,
        logs_df=logs_df,
        cluster_id=cluster_id,
        suffix=suffix,
        ecs_metrics_df=ecs_df,
        config=_config_from_env(),
        extra_stats=extra_stats,
    )

    output_key = f"{output_prefix}{timestamp}/results_{suffix}.html"
    output_json_key = re.sub(r"\.html$", ".json", output_key)
    if output_json_key == output_key:
        output_json_key = f"{output_prefix}{timestamp}/results_{suffix}.json"

    print(f"Uploading report to s3://{output_bucket}/{output_key}")
    s3 = boto3.client("s3")
    s3.put_object(Bucket=output_bucket, Key=output_key, Body=html_content, ContentType="text/html")
    print(f"Uploading summary JSON to s3://{output_bucket}/{output_json_key}")
    s3.put_object(Bucket=output_bucket, Key=output_json_key, Body=summary_json, ContentType="application/json")


def main() -> None:
    if len(sys.argv) == 1 and not missing_ecs_env_vars():
        run_uploaded_report()
        return

    parser = build_parser()
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    if args.command == "compare":
        run_compare_report(args.baseline, args.candidate, args.output)
        return

    if args.command == "generate":
        config = {
            "engine_type": args.engine_type,
            "engine_version": args.engine_version,
            "node_type": args.node_type,
            "node_count": args.node_count,
            "cluster_mode": args.cluster_mode,
        }
        run_generate_report(args.run_dir, config)
        return

    parser.print_help()
    if len(sys.argv) == 1:
        missing = missing_ecs_env_vars()
        if missing:
            print(f"\nMissing ECS environment variables: {', '.join(missing)}")
    sys.exit(2)


if __name__ == "__main__":
    main()
