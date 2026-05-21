"""Generate sidecar ETL artifacts from raw per-stream memtier CloudWatch logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from parsers import parse_memtier_final_totals, parse_memtier_logs


PER_STREAM_COLUMNS = [
    "minute_utc",
    "stream_id",
    "sample_count",
    "throughput_avg",
    "throughput_median",
    "throughput_min",
    "throughput_max",
    "throughput_p10",
    "throughput_p90",
    "latency_avg",
    "latency_median",
    "latency_min",
    "latency_max",
    "latency_p10",
    "latency_p90",
    "bandwidth_avg_kbs",
]

TOTALS_FIELDS = [
    "stream_id",
    "timestamp_utc",
    "ops_per_sec",
    "avg_latency_ms",
    "p50_latency_ms",
    "p99_latency_ms",
    "p999_latency_ms",
    "bandwidth_kbs",
]

COMBINED_COLUMNS = [
    "minute_utc",
    "task_count_present",
    "sample_count_total",
    "throughput_sum",
    "throughput_avg",
    "throughput_median",
    "throughput_min",
    "throughput_max",
    "throughput_p10",
    "throughput_p90",
    "latency_weighted_avg",
    "latency_avg",
    "latency_median",
    "latency_min",
    "latency_max",
    "latency_p10",
    "latency_p90",
]


def _utc_text(timestamp: pd.Timestamp) -> str:
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def _is_memtier_source(path: Path, memtier_root: Path) -> bool:
    try:
        relative = path.relative_to(memtier_root)
    except ValueError:
        return False
    return path.suffix == ".txt" and len(relative.parts) >= 2 and relative.parts[0] == "memtier"


def _stream_id_from_source(path: Path) -> str:
    return path.stem


def _per_stream_minutes(logs_df: pd.DataFrame, stream_id: str) -> pd.DataFrame:
    if logs_df.empty:
        return pd.DataFrame(columns=PER_STREAM_COLUMNS)

    df = logs_df.copy()
    df["minute_utc"] = pd.to_datetime(df["Timestamp"], utc=True).dt.floor("min")
    rows = []
    for minute, group in df.groupby("minute_utc", sort=True):
        throughput = group["Ops/sec"]
        latency = group["Latency (ms)"]
        rows.append(
            {
                "minute_utc": _utc_text(minute),
                "stream_id": stream_id,
                "sample_count": int(len(group)),
                "throughput_avg": float(throughput.mean()),
                "throughput_median": float(throughput.median()),
                "throughput_min": float(throughput.min()),
                "throughput_max": float(throughput.max()),
                "throughput_p10": float(throughput.quantile(0.10)),
                "throughput_p90": float(throughput.quantile(0.90)),
                "latency_avg": float(latency.mean()),
                "latency_median": float(latency.median()),
                "latency_min": float(latency.min()),
                "latency_max": float(latency.max()),
                "latency_p10": float(latency.quantile(0.10)),
                "latency_p90": float(latency.quantile(0.90)),
                "bandwidth_avg_kbs": float(group["Bandwidth_KBs"].mean()),
            }
        )
    return pd.DataFrame(rows, columns=PER_STREAM_COLUMNS)


def _totals_payload(totals_df: pd.DataFrame, stream_id: str) -> dict | None:
    if totals_df.empty:
        return None
    row = totals_df.iloc[-1]
    return {
        "stream_id": stream_id,
        "timestamp_utc": _utc_text(row["Timestamp"]),
        "ops_per_sec": float(row["Ops/sec"]),
        "avg_latency_ms": float(row["Latency (ms)"]),
        "p50_latency_ms": float(row["p50 Latency (ms)"]),
        "p99_latency_ms": float(row["p99 Latency (ms)"]),
        "p999_latency_ms": float(row["p999 Latency (ms)"]),
        "bandwidth_kbs": float(row["Bandwidth_KBs"]),
    }


def _combined_minutes(per_stream_frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [frame for frame in per_stream_frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=COMBINED_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    rows = []
    for minute, group in combined.groupby("minute_utc", sort=True):
        throughput = group["throughput_avg"]
        latency = group["latency_avg"]
        throughput_sum = float(throughput.sum())
        rows.append(
            {
                "minute_utc": minute,
                "task_count_present": int(group["stream_id"].nunique()),
                "sample_count_total": int(group["sample_count"].sum()),
                "throughput_sum": throughput_sum,
                "throughput_avg": float(throughput.mean()),
                "throughput_median": float(throughput.median()),
                "throughput_min": float(throughput.min()),
                "throughput_max": float(throughput.max()),
                "throughput_p10": float(throughput.quantile(0.10)),
                "throughput_p90": float(throughput.quantile(0.90)),
                "latency_weighted_avg": float((latency * throughput).sum() / throughput_sum)
                if throughput_sum
                else 0.0,
                "latency_avg": float(latency.mean()),
                "latency_median": float(latency.median()),
                "latency_min": float(latency.min()),
                "latency_max": float(latency.max()),
                "latency_p10": float(latency.quantile(0.10)),
                "latency_p90": float(latency.quantile(0.90)),
            }
        )
    return pd.DataFrame(rows, columns=COMBINED_COLUMNS)


def generate_memtier_artifacts(run_dir: Path) -> dict:
    """Write per-stream and combined minute artifacts for one local result run."""
    memtier_root = run_dir / "logs" / "loadgen" / "memtier"
    source_paths = sorted(
        path for path in memtier_root.rglob("*.txt") if path.is_file() and _is_memtier_source(path, memtier_root)
    ) if memtier_root.exists() else []

    frames = []
    stream_results = []
    output_dir = None
    for source_path in source_paths:
        stream_id = _stream_id_from_source(source_path)
        content = source_path.read_text(encoding="utf-8", errors="replace")
        logs_df = parse_memtier_logs(content, stream_id)
        totals_df = parse_memtier_final_totals(content, stream_id)
        minute_df = _per_stream_minutes(logs_df, stream_id)
        minute_path = source_path.with_suffix(".minute.csv")
        totals_path = source_path.with_suffix(".totals.json")

        minute_df.to_csv(minute_path, index=False)
        totals_payload = _totals_payload(totals_df, stream_id)
        if totals_payload is not None:
            totals_path.write_text(json.dumps(totals_payload, indent=2) + "\n", encoding="utf-8")

        frames.append(minute_df)
        stream_results.append({"source": source_path, "minute": minute_path, "totals": totals_path})
        output_dir = source_path.parent if output_dir is None else output_dir
        if output_dir != source_path.parent:
            raise ValueError("memtier source streams must share one artifact directory")

    combined_df = _combined_minutes(frames)
    combined_path = output_dir / "_memtier.minute.csv" if output_dir is not None else None
    if combined_path is not None:
        combined_df.to_csv(combined_path, index=False)

    return {"streams": stream_results, "combined": combined_path, "combined_df": combined_df}


def generate_memtier_dataframes(log_entries: list[tuple[str, str]]) -> tuple[pd.DataFrame, list[dict]]:
    """Generate combined minute DataFrame and totals payload list from in-memory log content.

    Args:
        log_entries: List of (stream_id, content) pairs.

    Returns:
        (combined_df, totals_list) where combined_df has COMBINED_COLUMNS
        and totals_list contains _totals_payload dicts.
    """
    frames = []
    totals_list = []
    for fallback_stream_id, content in log_entries:
        logs_df = parse_memtier_logs(content, fallback_stream_id)
        if not logs_df.empty:
            for stream_id, stream_logs_df in logs_df.groupby("Stream", sort=True):
                frames.append(_per_stream_minutes(stream_logs_df, stream_id))

        totals_df = parse_memtier_final_totals(content, fallback_stream_id)
        if not totals_df.empty:
            for stream_id, stream_totals_df in totals_df.groupby("Stream", sort=True):
                payload = _totals_payload(stream_totals_df, stream_id)
                if payload is not None:
                    totals_list.append(payload)
    return _combined_minutes(frames), totals_list


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Local results run directory.")
    args = parser.parse_args()
    result = generate_memtier_artifacts(args.run_dir)
    print(f"generated {len(result['streams'])} stream artifacts")
    if result["combined"] is not None:
        print(result["combined"])


if __name__ == "__main__":
    main()
