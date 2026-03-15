from __future__ import annotations

import argparse
import os
import sys

try:
    from report_common import ECS_ENV_VARS
    from report_compare import run_compare_report
    from report_ecs import run_ecs_report
except ImportError:  # pragma: no cover - supports python -m reporter.report_generator
    from reporter.report_common import ECS_ENV_VARS
    from reporter.report_compare import run_compare_report
    from reporter.report_ecs import run_ecs_report


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
    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    if argv and argv[0] not in {"compare"} and not argv[0].startswith("-"):
        return ["compare", *argv]
    return argv


def missing_ecs_env_vars() -> list[str]:
    return [name for name in ECS_ENV_VARS if not os.environ.get(name)]


def main() -> None:
    if len(sys.argv) == 1 and not missing_ecs_env_vars():
        run_ecs_report()
        return

    parser = build_parser()
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    if args.command == "compare":
        run_compare_report(args.baseline, args.candidate, args.output)
        return

    parser.print_help()
    if len(sys.argv) == 1:
        missing = missing_ecs_env_vars()
        if missing:
            print(f"\nMissing ECS environment variables: {', '.join(missing)}")
    sys.exit(2)


if __name__ == "__main__":
    main()
