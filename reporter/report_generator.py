from __future__ import annotations

import argparse
from io import StringIO
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
    parse_memtier_extra_stats,
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


def _read_s3_prefix_contents(prefix_uri: str, suffixes: tuple[str, ...]) -> list[tuple[str, str]]:
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
            if not key.endswith(suffixes):
                continue
            uri = f"s3://{bucket}/{key}"
            entries.append((uri, read_file_content(uri)))
    return sorted(entries, key=lambda item: item[0])


def _read_local_log_contents(logs_dir: Path, cluster_id: str) -> list[tuple[str, str]]:
    loadgen_dir = logs_dir / "loadgen"
    files = sorted(path for path in loadgen_dir.rglob("*.txt") if path.is_file()) if loadgen_dir.exists() else []
    if not files:
        legacy_loadgen = logs_dir / f"{cluster_id}.txt"
        files = [legacy_loadgen] if legacy_loadgen.is_file() else []

    return [
        (str(path), path.read_text(encoding="utf-8", errors="replace"))
        for path in files
    ]


def _read_uploaded_log_contents(logs_prefix: str) -> list[tuple[str, str]]:
    return _read_s3_prefix_contents(logs_prefix, (".txt",)) if logs_prefix else []


def _read_uploaded_memtier_artifact_contents(logs_prefix: str) -> list[tuple[str, str]]:
    return _read_s3_prefix_contents(logs_prefix, (".minute.csv", ".totals.json")) if logs_prefix else []


def _is_memtier_log_entry(source: str) -> bool:
    normalized = source.replace("\\", "/")
    path = Path(normalized)
    return "/logs/loadgen/memtier/" in normalized or (
        path.suffix == ".txt" and path.parent.name == "logs"
    )


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

    first_message_ts = None
    last_message_ts = None
    first_oom_rejection_ts = None
    oom_frames = []
    for source, content in memtier_entries:
        stream = _memtier_stream_from_source(source)
        stats = parse_memtier_extra_stats(content, stream)
        for key, current in (
            ("first_message_ts", first_message_ts),
            ("last_message_ts", last_message_ts),
            ("first_oom_rejection_ts", first_oom_rejection_ts),
        ):
            value = stats.get(key)
            if value is None:
                continue
            if key == "first_message_ts":
                first_message_ts = value if current is None or value < current else current
            elif key == "last_message_ts":
                last_message_ts = value if current is None or value > current else current
            else:
                first_oom_rejection_ts = value if current is None or value < current else current
        oom_frames.append(stats.get("oom_df", pd.DataFrame()))

    oom_df = pd.concat([frame for frame in oom_frames if not frame.empty], ignore_index=True) if any(
        not frame.empty for frame in oom_frames
    ) else pd.DataFrame(columns=["Timestamp", "OOM_events"])
    if not oom_df.empty:
        oom_df = oom_df.groupby("Timestamp", as_index=False)["OOM_events"].sum()

    return pd.DataFrame(), {
        "first_message_ts": first_message_ts,
        "last_message_ts": last_message_ts,
        "first_oom_rejection_ts": first_oom_rejection_ts,
        "oom_df": oom_df,
    }


_MINUTE_COLUMN_ALIASES = {
    "Timestamp": ("Timestamp", "timestamp", "minute_utc", "minute_start_utc"),
    "throughput_sum": ("throughput_sum",),
    "latency_weighted_avg": ("latency_weighted_avg",),
    "throughput_median": ("throughput_median",),
    "throughput_avg": ("throughput_avg",),
    "throughput_p10": ("throughput_p10",),
    "throughput_p90": ("throughput_p90",),
    "throughput_min": ("throughput_min",),
    "throughput_max": ("throughput_max",),
    "latency_median": ("latency_median",),
    "latency_avg": ("latency_avg",),
    "latency_p10": ("latency_p10",),
    "latency_p90": ("latency_p90",),
    "latency_min": ("latency_min",),
    "latency_max": ("latency_max",),
}

_TOTAL_FIELD_ALIASES = {
    "throughput_avg": ("throughput_avg", "avg_throughput", "ops_per_sec", "ops_sec", "Ops/sec"),
    "latency_avg_ms": ("latency_avg_ms", "avg_latency_ms", "latency_weighted_avg", "Latency (ms)"),
    "total_bandwidth_kbs": ("total_bandwidth_kbs", "bandwidth_kbs", "Bandwidth_KBs"),
}


def _first_present(mapping, names):
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _normalize_memtier_minute_artifact(content: str, source: str):
    import pandas as pd

    df = pd.read_csv(StringIO(content))
    rename = {}
    for canonical, aliases in _MINUTE_COLUMN_ALIASES.items():
        source_name = next((name for name in aliases if name in df.columns), None)
        if source_name is None:
            raise ValueError(f"Memtier minute artifact {source} is missing required column {canonical}")
        rename[source_name] = canonical
    df = df.rename(columns=rename)[list(_MINUTE_COLUMN_ALIASES)]
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True).dt.tz_localize(None)
    return df.sort_values("Timestamp").reset_index(drop=True)


def _normalize_memtier_totals_artifacts(entries: list[tuple[str, str]]):
    import pandas as pd

    rows = []
    for source, content in entries:
        payload = json.loads(content)
        row = {"source": source}
        for canonical, aliases in _TOTAL_FIELD_ALIASES.items():
            value = _first_present(payload, aliases)
            if value is None:
                raise ValueError(f"Memtier totals artifact {source} is missing required field {canonical}")
            row[canonical] = value
        rows.append(row)
    df = pd.DataFrame(rows)
    for column in _TOTAL_FIELD_ALIASES:
        df[column] = pd.to_numeric(df[column])
    return df


def _load_memtier_artifacts(entries: list[tuple[str, str]]):
    import pandas as pd

    minute_entries = [(source, content) for source, content in entries if source.endswith("_memtier.minute.csv")]
    totals_entries = [(source, content) for source, content in entries if source.endswith(".totals.json")]
    if len(minute_entries) > 1:
        raise ValueError("Expected exactly one combined _memtier.minute.csv artifact")
    minute_df = (
        _normalize_memtier_minute_artifact(minute_entries[0][1], minute_entries[0][0])
        if minute_entries
        else pd.DataFrame(columns=list(_MINUTE_COLUMN_ALIASES))
    )
    totals_df = (
        _normalize_memtier_totals_artifacts(totals_entries)
        if totals_entries
        else pd.DataFrame(columns=["source", *_TOTAL_FIELD_ALIASES])
    )
    return minute_df, totals_df


def _read_generated_memtier_artifact_contents(generated: dict) -> list[tuple[str, str]]:
    """Read only sidecars produced by the current local ETL invocation."""
    combined_path = generated.get("combined")
    if combined_path is None or not Path(combined_path).is_file():
        return []

    files = [Path(combined_path)]
    files.extend(
        Path(totals_path)
        for stream in generated.get("streams", [])
        if (totals_path := stream.get("totals")) is not None and Path(totals_path).is_file()
    )
    files = sorted(files)
    return [(str(path), path.read_text(encoding="utf-8")) for path in files]


def _memtier_dfs_from_log_entries(log_entries: list[tuple[str, str]]):
    """Build memtier minute/totals DataFrames in-memory when ETL sidecar files are absent."""
    import pandas as pd
    from memtier_etl import generate_memtier_dataframes

    pairs = [
        (_memtier_stream_from_source(source), content)
        for source, content in log_entries
        if _is_memtier_log_entry(source)
    ]
    if not pairs:
        return (
            pd.DataFrame(columns=list(_MINUTE_COLUMN_ALIASES)),
            pd.DataFrame(columns=["source", *_TOTAL_FIELD_ALIASES]),
        )

    combined_df, totals_list = generate_memtier_dataframes(pairs)

    # Align to _load_memtier_artifacts output: rename minute_utc → Timestamp, strip tz
    if not combined_df.empty:
        if "minute_utc" in combined_df.columns:
            combined_df = combined_df.rename(columns={"minute_utc": "Timestamp"})
        combined_df["Timestamp"] = pd.to_datetime(combined_df["Timestamp"], utc=True).dt.tz_localize(None)

    canonical_cols = list(_MINUTE_COLUMN_ALIASES)
    if combined_df.empty:
        combined_df = pd.DataFrame(columns=canonical_cols)
    else:
        combined_df = combined_df[[c for c in canonical_cols if c in combined_df.columns]]
        combined_df = combined_df.sort_values("Timestamp").reset_index(drop=True)

    totals_rows = [
        {
            "source": p["stream_id"],
            "throughput_avg": float(p["ops_per_sec"]),
            "latency_avg_ms": float(p["avg_latency_ms"]),
            "total_bandwidth_kbs": float(p["bandwidth_kbs"]),
        }
        for p in totals_list
    ]
    totals_cols = ["source", *_TOTAL_FIELD_ALIASES]
    if totals_rows:
        totals_df = pd.DataFrame(totals_rows, columns=totals_cols)
        for col in _TOTAL_FIELD_ALIASES:
            totals_df[col] = pd.to_numeric(totals_df[col])
    else:
        totals_df = pd.DataFrame(columns=totals_cols)

    return combined_df, totals_df


def create_report(
    metrics_df,
    logs_df,
    memtier_minute_df,
    memtier_totals_df,
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

    fig_m = build_memtier_figure(memtier_minute_df, oom_df, metrics_window_df, x_min, x_max)
    fig_i = build_infra_figure(ecs_window_df, metrics_window_df, cluster_id, config, x_min, x_max)
    fig_d = build_elasticache_deep_dive_figure(metrics_window_df, cluster_id, config, x_min, x_max)

    time_range = _format_time_range(x_min, x_max)
    cluster_mode = str(config.get("cluster_mode", "false")).lower() == "true"
    id_label = "Cluster" if cluster_mode else "Replication Group"

    summary = build_summary(
        metrics_window_df, memtier_minute_df, memtier_totals_df, ecs_window_df, extra_stats, config, cluster_id, time_range
    )
    summary_json = json.dumps(summary, indent=2, default=str)

    html_content = render_html(
        cluster_id=cluster_id,
        suffix=suffix,
        id_label=id_label,
        time_range=time_range,
        pills_html=header_pills(config),
        cards_html=stat_cards_html(
            memtier_minute_df,
            memtier_totals_df,
            metrics_window_df,
            ecs_window_df,
            extra_stats=extra_stats,
            config=config,
            cluster_id=cluster_id,
        ),
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
    artifact_entries = []
    try:
        from memtier_etl import generate_memtier_artifacts as _gen_etl
        artifact_entries = _read_generated_memtier_artifact_contents(_gen_etl(run_path))
    except Exception as exc:
        print(f"Warning: memtier ETL generation failed: {exc}")
    if not log_entries:
        print(f"No log file found in {logs_dir}")
        logs_df = pd.DataFrame()
        extra_stats = {}
    else:
        print(f"Reading {len(log_entries)} loadgen log file(s)")
        logs_df, extra_stats = _parse_memtier_log_entries(log_entries)
    if artifact_entries:
        memtier_minute_df, memtier_totals_df = _load_memtier_artifacts(artifact_entries)
    else:
        memtier_minute_df, memtier_totals_df = _memtier_dfs_from_log_entries(log_entries)

    _warn_if_cache_hit_rate_missing(metrics_df, str(ec_csvs[0]))

    html_content, summary_json = create_report(
        metrics_df=metrics_df,
        logs_df=logs_df,
        memtier_minute_df=memtier_minute_df,
        memtier_totals_df=memtier_totals_df,
        cluster_id=cluster_id,
        suffix=run_path.name,
        ecs_metrics_df=ecs_df,
        config=config,
        extra_stats=extra_stats,
    )

    cluster_details_path = run_path / "cluster_details.json"
    if cluster_details_path.exists():
        try:
            from report_common import enrich_summary_meta
            cluster_details = json.loads(cluster_details_path.read_text(encoding="utf-8"))
            summary_obj = json.loads(summary_json)
            enrich_summary_meta(summary_obj, cluster_details)
            summary_json = json.dumps(summary_obj, indent=2, default=str)
        except Exception as exc:
            print(f"Warning: failed to enrich summary with cluster_details.json: {exc}")

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
    artifact_entries = _read_uploaded_memtier_artifact_contents(logs_prefix)
    if not artifact_entries:
        print(
            f"Error: no memtier ETL sidecar artifacts found under {logs_prefix}\n"
            "Expected _memtier.minute.csv and *.totals.json to be present.\n"
            "Run the exporter to generate them before calling the report generator."
        )
        sys.exit(2)
    memtier_minute_df, memtier_totals_df = _load_memtier_artifacts(artifact_entries)

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
        memtier_minute_df=memtier_minute_df,
        memtier_totals_df=memtier_totals_df,
        cluster_id=cluster_id,
        suffix=suffix,
        ecs_metrics_df=ecs_df,
        config=_config_from_env(),
        extra_stats=extra_stats,
    )

    cluster_details_uri = f"s3://{output_bucket}/{output_prefix}{timestamp}/cluster_details.json"
    try:
        from report_common import enrich_summary_meta
        cluster_details = json.loads(read_file_content(cluster_details_uri))
        summary_obj = json.loads(summary_json)
        enrich_summary_meta(summary_obj, cluster_details)
        summary_json = json.dumps(summary_obj, indent=2, default=str)
        print(f"Summary enriched from {cluster_details_uri}")
    except Exception:
        pass  # cluster_details.json is optional

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
