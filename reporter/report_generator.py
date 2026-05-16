from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from report_common import ECS_ENV_VARS
from report_compare import run_compare_report
from helpers import read_file_content
from parsers import (
    parse_metrics_csv,
    parse_memtier_logs,
    parse_memtier_extra_stats,
    parse_memtier_final_totals,
)
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


def _normalize_ts(value):
    if value is None:
        return None
    if hasattr(value, "tzinfo") and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _format_time_range(start, end) -> str:
    start = _normalize_ts(start)
    end = _normalize_ts(end)
    if start is None or end is None:
        return ""

    def format_ts(value):
        pattern = "%Y-%m-%d %H:%M:%S.%f" if value.microsecond else "%Y-%m-%d %H:%M:%S"
        return f"{value.strftime(pattern)} UTC"

    return f"{format_ts(start)} - {format_ts(end)}"


def _clip_to_time_window(df, start, end):
    if df is None or df.empty or "Timestamp" not in df.columns or start is None or end is None:
        return df
    ts = df["Timestamp"]
    return df[(ts >= start) & (ts <= end)].copy()


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
        has_hits = (
            (metrics_df["MetricName"] == "CacheHits")
            & (metrics_df["Stat"] == "Sum")
            & (metrics_df["Dimensions"].astype(str).str.startswith("CacheClusterId"))
        ).any()
        has_misses = (
            (metrics_df["MetricName"] == "CacheMisses")
            & (metrics_df["Stat"] == "Sum")
            & (metrics_df["Dimensions"].astype(str).str.startswith("CacheClusterId"))
        ).any()
        if has_hits and has_misses:
            print(
                "Info: CacheHitRate/Average is missing from "
                f"{source}; deriving hit rate from CacheHits/CacheMisses."
            )
            return
        print(
            "Warning: CacheHitRate/Average with CacheClusterId dimensions is missing from "
            f"{source}. The old local report plotted this CloudWatch metric directly, "
            "so the cache-hit chart cannot be reproduced until those metrics are present."
        )


def _s3_bucket_key(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {uri}")
    parts = uri[5:].split("/", 1)
    if len(parts) != 2 or not parts[1]:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parts[0], parts[1]


def _read_s3_prefix_log_contents(prefix_uri: str) -> list[tuple[str, str]]:
    import boto3

    bucket, prefix = _s3_bucket_key(prefix_uri)
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    entries = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            if not key.endswith(".txt"):
                continue
            uri = f"s3://{bucket}/{key}"
            entries.append((uri, read_file_content(uri)))
    return sorted(entries, key=lambda item: item[0])


def _read_local_log_contents(logs_dir: Path, cluster_id: str) -> list[tuple[str, str]]:
    loadgen_dir = logs_dir / "loadgen"
    files = sorted(path for path in loadgen_dir.rglob("*.txt") if path.is_file()) if loadgen_dir.exists() else []

    return [
        (str(path), path.read_text(encoding="utf-8", errors="replace"))
        for path in files
    ]


def _read_uploaded_log_contents(logs_prefix: str) -> list[tuple[str, str]]:
    return _read_s3_prefix_log_contents(logs_prefix) if logs_prefix else []


def _is_memtier_log_entry(source: str) -> bool:
    normalized = source.replace("\\", "/")
    return "/logs/loadgen/memtier/" in normalized


def _memtier_stream_from_source(source: str) -> str:
    normalized = source.replace("\\", "/")
    marker = "/logs/loadgen/memtier/"
    suffix = normalized.split(marker, 1)[1] if marker in normalized else normalized.rsplit("/", 1)[-1]
    return suffix.removesuffix(".txt")


def _parse_memtier_log_entries(entries: list[tuple[str, str]]):
    import pandas as pd

    memtier_entries = [(source, content) for source, content in entries if _is_memtier_log_entry(source)]
    if not memtier_entries:
        return pd.DataFrame(), {}

    logs_frames = []
    totals_frames = []
    first_message_ts = None
    last_message_ts = None
    first_eviction_ts = None
    oom_frames = []
    for source, content in memtier_entries:
        stream = _memtier_stream_from_source(source)
        logs_frames.append(parse_memtier_logs(content, stream))
        totals_frames.append(parse_memtier_final_totals(content, stream))
        stats = parse_memtier_extra_stats(content, stream)
        for key, current in (
            ("first_message_ts", first_message_ts),
            ("last_message_ts", last_message_ts),
            ("first_eviction_ts", first_eviction_ts),
        ):
            value = stats.get(key)
            if value is None:
                continue
            if key == "first_message_ts":
                first_message_ts = value if current is None or value < current else current
            elif key == "last_message_ts":
                last_message_ts = value if current is None or value > current else current
            else:
                first_eviction_ts = value if current is None or value < current else current
        oom_frames.append(stats.get("oom_df", pd.DataFrame()))

    logs_df = pd.concat([frame for frame in logs_frames if not frame.empty], ignore_index=True) if any(
        not frame.empty for frame in logs_frames
    ) else pd.DataFrame()
    totals_df = pd.concat([frame for frame in totals_frames if not frame.empty], ignore_index=True) if any(
        not frame.empty for frame in totals_frames
    ) else pd.DataFrame()
    oom_df = pd.concat([frame for frame in oom_frames if not frame.empty], ignore_index=True) if any(
        not frame.empty for frame in oom_frames
    ) else pd.DataFrame(columns=["Timestamp", "OOM_events"])
    if not oom_df.empty:
        oom_df = oom_df.groupby("Timestamp", as_index=False)["OOM_events"].sum()

    return logs_df, {
        "first_message_ts": first_message_ts,
        "last_message_ts": last_message_ts,
        "first_eviction_ts": first_eviction_ts,
        "oom_df": oom_df,
        "final_totals_df": totals_df,
    }


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

    This follows the March 2026 local report pipeline. Cache hit rate is read
    from CacheHitRate/Average when CloudWatch publishes it, or derived from
    CacheHits/CacheMisses at CacheClusterId granularity when it does not.
    """
    import pandas as pd

    ecs_df = ecs_metrics_df if ecs_metrics_df is not None else pd.DataFrame()
    config = config or {}
    extra_stats = extra_stats or {}

    x_min = _normalize_ts(extra_stats.get("first_message_ts"))
    x_max = _normalize_ts(extra_stats.get("last_message_ts"))

    oom_df = extra_stats.get("oom_df", pd.DataFrame())

    metrics_window_df = _clip_to_time_window(metrics_df, x_min, x_max)
    ecs_window_df = _clip_to_time_window(ecs_df, x_min, x_max)

    fig_m = build_memtier_figure(logs_df, oom_df, metrics_window_df, x_min, x_max)
    fig_i = build_infra_figure(ecs_window_df, metrics_window_df, cluster_id, config, x_min, x_max)
    fig_d = build_elasticache_deep_dive_figure(metrics_window_df, cluster_id, config, x_min, x_max)

    time_range = _format_time_range(x_min, x_max)
    cluster_mode = str(config.get("cluster_mode", "false")).lower() == "true"
    id_label = "Cluster" if cluster_mode else "Replication Group"

    summary = build_summary(metrics_window_df, logs_df, ecs_window_df, extra_stats, config, cluster_id, time_range)
    summary_json = json.dumps(summary, indent=2, default=str)

    html_content = render_html(
        cluster_id=cluster_id,
        suffix=suffix,
        id_label=id_label,
        time_range=time_range,
        pills_html=header_pills(config),
        cards_html=stat_cards_html(logs_df, metrics_window_df, ecs_window_df, extra_stats, config),
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

    log_entries = _read_local_log_contents(logs_dir, cluster_id)
    if not log_entries:
        print(f"No log file found in {logs_dir}")
        logs_df = pd.DataFrame()
        extra_stats = {}
    else:
        print(f"Reading {len(log_entries)} loadgen log file(s)")
        logs_df, extra_stats = _parse_memtier_log_entries(log_entries)

    _warn_if_cache_hit_rate_missing(metrics_df, str(ec_csvs[0]))

    html_content, summary_json = create_report(
        metrics_df=metrics_df,
        logs_df=logs_df,
        cluster_id=cluster_id,
        suffix=run_path.name,
        ecs_metrics_df=ecs_df,
        config=config,
        extra_stats=extra_stats,
    )

    out_path = run_path / "results_local.json"
    out_path.write_text(summary_json, encoding="utf-8")
    print(f"Written: {out_path}")

    canonical_json_path = run_path / f"results_{run_path.name}.json"
    canonical_json_path.write_text(summary_json, encoding="utf-8")
    print(f"Written: {canonical_json_path}")

    html_path = run_path / "results_local.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"Written: {html_path}")

    canonical_html_path = run_path / f"results_{run_path.name}.html"
    canonical_html_path.write_text(html_content, encoding="utf-8")
    print(f"Written: {canonical_html_path}")


def run_uploaded_report() -> None:
    import boto3
    import pandas as pd

    s3_bucket = os.environ.get("S3_BUCKET", "")
    s3_prefix = os.environ.get("S3_PREFIX", "")
    timestamp = os.environ.get("REPORT_TIMESTAMP") or os.environ.get("SUFFIX", "report")
    cluster_id = os.environ.get("CLUSTER_ID", "Unknown")

    metrics_csv = os.environ.get("METRICS_CSV")
    ecs_metrics_csv = os.environ.get("ECS_METRICS_CSV", "")
    logs_prefix = os.environ.get("LOGS_PREFIX", "")
    if not metrics_csv and s3_bucket and s3_prefix and timestamp and cluster_id:
        metrics_csv = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/metrics/{cluster_id}.csv"
    if not ecs_metrics_csv and s3_bucket and s3_prefix and timestamp and cluster_id:
        ecs_metrics_csv = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/metrics/{cluster_id}-ecs.csv"
    if not logs_prefix and s3_bucket and s3_prefix and timestamp:
        logs_prefix = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/logs/loadgen/"

    output_bucket = os.environ.get("OUTPUT_BUCKET") or s3_bucket
    output_prefix = os.environ.get("OUTPUT_PREFIX", s3_prefix)
    suffix = os.environ.get("SUFFIX", timestamp)

    if not metrics_csv or not logs_prefix or not output_bucket:
        missing = [
            name
            for name, value in {
                "METRICS_CSV": metrics_csv,
                "LOGS_PREFIX": logs_prefix,
                "OUTPUT_BUCKET": output_bucket,
            }.items()
            if not value
        ]
        print(f"Missing ECS report inputs: {', '.join(missing)}")
        sys.exit(2)

    print(f"Reading metrics from {metrics_csv}")
    metrics_df = parse_metrics_csv(read_file_content(metrics_csv))
    _warn_if_cache_hit_rate_missing(metrics_df, metrics_csv)

    log_entries = _read_uploaded_log_contents(logs_prefix)
    if not log_entries:
        print(f"No loadgen log files found from {logs_prefix}")
        sys.exit(2)
    print(f"Reading {len(log_entries)} loadgen log file(s)")
    logs_df, extra_stats = _parse_memtier_log_entries(log_entries)

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
