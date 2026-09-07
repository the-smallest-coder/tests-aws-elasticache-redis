"""Group repeated runs by control-variable fingerprint; report per-metric
n/median/mean/CV%/min/max, and the ElastiCache-node-only cost per successful
operation. See PLAN_2.md WP6.

Never writes into a run's own results/<run-folder>/ (D9): output goes to a
sibling results/aggregates/ directory, modeled on the existing
results/comparisons/. Input run directories are read-only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from comparison_contract import (
    CONTROL_VARIABLES,
    INTENDED_DIMENSIONS,
    coerce_control_value,
    control_variable_value,
    intended_dimension_value,
    run_window_seconds,
)
from report_common import RunData, get_nested, load_run, metric_value


def run_fingerprint(run: RunData) -> tuple[Any, ...] | None:
    """Intended dimensions + all control variables, normalized (WP4/WP6).

    None if any single piece can't be determined -- a partial fingerprint
    would silently merge runs that may not actually be repeats of each
    other, exactly the failure mode WP6 step 3's CV analysis exists to catch.
    """
    parts: list[Any] = []
    for dimension in INTENDED_DIMENSIONS:
        value = intended_dimension_value(run, dimension)
        if not value:
            return None
        parts.append(value)
    for section, field in CONTROL_VARIABLES:
        value = coerce_control_value(field, control_variable_value(run, section, field))
        if value is None:
            return None
        parts.append(value)
    return tuple(parts)


def _fingerprint_label(fingerprint: tuple[Any, ...]) -> str:
    dims = fingerprint[: len(INTENDED_DIMENSIONS)]
    return "-".join(str(part) for part in dims)


def _fingerprint_id(fingerprint: tuple[Any, ...], label: str) -> str:
    digest = hashlib.sha1(repr(fingerprint).encode("utf-8")).hexdigest()[:10]
    safe_label = "".join(char if char.isalnum() or char in "-_." else "_" for char in label)
    return f"{safe_label}-{digest}"


def group_runs(runs: list[RunData]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """-> ({fingerprint_id: {"label", "fingerprint", "runs": [RunData, ...]}}, unmatched)

    unmatched entries are {"run": RunData, "reason": str} for runs whose
    fingerprint could not be determined at all (e.g. a thin cluster_details.json).
    """
    groups: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    for run in runs:
        fingerprint = run_fingerprint(run)
        if fingerprint is None:
            unmatched.append({"run": run, "reason": "control_variables_or_intended_dimensions_unknown"})
            continue
        label = _fingerprint_label(fingerprint)
        fingerprint_id = _fingerprint_id(fingerprint, label)
        group = groups.setdefault(fingerprint_id, {"label": label, "fingerprint": fingerprint, "runs": []})
        group["runs"].append(run)
    return groups, unmatched


def _cv_pct(values: list[float]) -> float | None:
    """Sample stdev (ddof=1), matching benchmark.cv_pct's pandas.Series.std()
    default -- population stdev would disagree by 13% at n=4 (PLAN_2.md WP6)."""
    import pandas as pd

    series = pd.Series(values, dtype="float64")
    mean = float(series.mean())
    if mean == 0 or len(values) < 2:
        return None
    return float(series.std() / mean * 100.0)


def metric_stats(runs: list[RunData], metrics: tuple) -> list[dict[str, Any]]:
    """n/median/mean/cv_pct/min/max per report_compare.METRICS entry.

    CV is reported as a plain column, never turned into an automatic verdict
    (WP6 step 3): the only corpus available groups by an incomplete
    fingerprint (pre-WP0 control variables are mostly unknown), so a
    threshold set from it would calibrate against the very artifact a full
    fingerprint is meant to eliminate.
    """
    import pandas as pd

    rows = []
    for spec in metrics:
        values = [
            value for run in runs
            if (value := metric_value(spec, get_nested(run.summary, spec.path))) is not None
        ]
        if not values:
            continue
        series = pd.Series(values, dtype="float64")
        rows.append({
            "section": spec.section,
            "label": spec.label,
            "path": list(spec.path),
            "unit": spec.unit,
            "n": len(values),
            "median": float(series.median()),
            "mean": float(series.mean()),
            "cv_pct": _cv_pct(values),
            "min": float(series.min()),
            "max": float(series.max()),
        })
    return rows


def _window_seconds_from_metrics(run: RunData) -> float | None:
    """Complete minute buckets in the *metric* window x 60 (WP6 step 4).

    total_ops (the numerator) is a CloudWatch Sum over that window, not the
    memtier report window -- for a canonical run the two coincide (both
    derive from the same test), so the memtier window's complete-minute
    count stands in for it; mixing an approximate and an exact window would
    be a unit error by construction, not just an approximation.
    """
    seconds = run_window_seconds(run)
    if seconds is None or seconds <= 0:
        return None
    whole_minutes = int(seconds // 60)
    return float(whole_minutes * 60) if whole_minutes > 0 else None


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cost_per_successful_operation(run: RunData) -> dict[str, Any]:
    """D11: the ONLY money anywhere in this plan -- the ElastiCache node's
    own hourly rate for its (node_type, engine, region). Never Fargate task
    cost, network transfer, or CloudWatch: the test measures ElastiCache: the
    bill for the instrument doing the measuring is out of scope.

    Computed at render time, never stored in results_local.json: the price
    is a constant for (node_type, region, engine) that already lives once in
    meta.node_hourly_usd; a stored division goes stale the moment the price
    changes or backfills.

    -> {"value": float|None, "reason": str|None, "errors_unknown": bool}
    """
    meta = run.summary.get("meta", {}) if isinstance(run.summary, dict) else {}
    cache_efficiency = run.summary.get("cache_efficiency", {}) if isinstance(run.summary, dict) else {}
    errors = run.summary.get("errors", {}) if isinstance(run.summary, dict) else {}

    total_ops = cache_efficiency.get("total_ops")
    if not total_ops:
        return {"value": None, "reason": "total_ops_unavailable", "errors_unknown": False}

    error_count = errors.get("error_count_total")
    errors_unknown = error_count is None
    successful_ops = total_ops - (error_count or 0)

    # No fallback to meta.redis_hourly_usd (D11): that key's values come
    # from a removed hardcoded, Redis-only, us-east-1 price table -- present
    # and non-empty in 42 of 81 historical reports, and every one of those
    # 42 is a Valkey run (tests/test_elasticache_node_catalog.py:75-89 guards
    # the table's removal, but only checks ecs.tf's text; it would not catch
    # a Python fallback silently resurrecting the same defect under a
    # different name). "n/a" for the whole history is the honest answer
    # until pricing:GetProducts access is granted.
    node_hourly_usd = meta.get("node_hourly_usd")
    if node_hourly_usd in (None, ""):
        return {"value": None, "reason": "node_hourly_usd_unavailable", "errors_unknown": errors_unknown}
    try:
        node_hourly_usd = float(node_hourly_usd)
    except (TypeError, ValueError):
        return {"value": None, "reason": "node_hourly_usd_unavailable", "errors_unknown": errors_unknown}

    if successful_ops <= 0:
        return {"value": None, "reason": "no_successful_ops", "errors_unknown": errors_unknown}

    window_seconds = _window_seconds_from_metrics(run)
    if window_seconds is None:
        return {"value": None, "reason": "metric_window_unavailable", "errors_unknown": errors_unknown}

    # meta.node_count is the string "1" in 81 of 82 historical reports (the
    # same stringly-typed-JSON issue WP4 normalizes control variables for);
    # "1" * seconds would be string repetition, not multiplication.
    node_count = _as_int_or_none(meta.get("node_count")) or 1

    cluster_usd_per_sec = node_hourly_usd * node_count / 3600.0
    ops_per_sec = successful_ops / window_seconds
    value = cluster_usd_per_sec / ops_per_sec
    return {"value": value, "reason": None, "errors_unknown": errors_unknown}


def build_aggregate_payload(fingerprint_id: str, group: dict[str, Any], metrics: tuple) -> dict[str, Any]:
    import pandas as pd

    runs: list[RunData] = group["runs"]
    cost_rows = []
    cost_values = []
    for run in runs:
        cost = cost_per_successful_operation(run)
        cost_rows.append({"folder": run.folder, **cost})
        if cost["value"] is not None:
            cost_values.append(cost["value"])

    cost_stats = None
    if cost_values:
        series = pd.Series(cost_values, dtype="float64")
        cost_stats = {
            "n": len(cost_values),
            "median": float(series.median()),
            "mean": float(series.mean()),
            "cv_pct": _cv_pct(cost_values),
            "min": float(series.min()),
            "max": float(series.max()),
        }

    return {
        "fingerprint_id": fingerprint_id,
        "label": group["label"],
        "n": len(runs),
        "runs": [run.folder for run in runs],
        "metrics": metric_stats(runs, metrics),
        "cost_per_successful_operation": {"per_run": cost_rows, "stats": cost_stats},
    }


def run_aggregate_report(run_dir_args: list[str], output_dir: str | None = None) -> None:
    from report_compare import METRICS
    from template import render_aggregate_report

    runs: list[RunData] = []
    for raw_path in run_dir_args:
        try:
            runs.append(load_run(Path(raw_path).name, raw_path))
        except Exception as exc:
            print(f"Warning: skipping {raw_path}: {exc}")

    groups, unmatched = group_runs(runs)

    if output_dir:
        out_root = Path(output_dir)
    else:
        # Sibling of results/, matching the existing results/comparisons/
        # convention -- never inside a run folder (D9).
        first_root = Path(run_dir_args[0]).resolve().parent
        out_root = (first_root / "aggregates") if first_root.name == "results" else (Path.cwd() / "aggregates")
    out_root.mkdir(parents=True, exist_ok=True)

    for fingerprint_id, group in groups.items():
        payload = build_aggregate_payload(fingerprint_id, group, METRICS)
        json_path = out_root / f"{fingerprint_id}.json"
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        html_path = out_root / f"{fingerprint_id}.html"
        html_path.write_text(render_aggregate_report(payload), encoding="utf-8")
        print(f"Aggregate written: {json_path} (n={payload['n']})")

    if unmatched:
        print(f"{len(unmatched)} run(s) did not fit any fingerprint group:")
        for item in unmatched:
            print(f"  - {item['run'].folder}: {item['reason']}")
