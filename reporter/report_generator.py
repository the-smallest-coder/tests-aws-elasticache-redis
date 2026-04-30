from __future__ import annotations

import argparse
import os
import sys

from report_common import ECS_ENV_VARS
from report_compare import run_compare_report
from report_ecs import run_ecs_report
from parsers import parse_metrics_csv, parse_memtier_logs, parse_memtier_extra_stats
from summary import build_summary


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


def run_generate_report(run_dir: str, config: dict) -> None:
    import glob
    import json
    import pandas as pd
    from pathlib import Path

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

    # Build time_range string
    if not metrics_df.empty:
        ts = metrics_df["Timestamp"]
        t0 = ts.min()
        t1 = ts.max()
        duration_min = round((t1 - t0).total_seconds() / 60, 0)
        time_range = f"{t0.strftime('%Y-%m-%d %H:%M')} \u2013 {t1.strftime('%H:%M')} ({int(duration_min)} min)"
    else:
        time_range = ""

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


def main() -> None:
    if len(sys.argv) == 1 and not missing_ecs_env_vars():
        run_ecs_report()
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
