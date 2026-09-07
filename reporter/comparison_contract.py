"""Comparability contract between two runs (WP4, PLAN_2.md).

Two runs with a different node_type are the point of the experiment,
not a confound -- but a different memtier pipeline, an unknown loadgen
validity, or a stale schema version silently make the comparison meaningless
even though nothing about it "looks" wrong. `verdict` is the top-level
signal; `reasons` is the structured list that explains it; `control_diffs`
is a rendering convenience (the control-variable subset of `reasons`).

`invalid` is reserved for a defect in one run's OWN data -- a run cannot be
made comparable by any choice of partner. Two runs that each look fine
individually but differ on more than one intended dimension are not
"invalid": that is a multi-factor experiment design, a fact about what was
run, not a data defect (see PLAN_2.md WP4 for the corpus evidence: every
"invalid" case found by naive multi-factor detection turned out to be
exactly this).
"""

from __future__ import annotations

from typing import Any

from report_common import RunData, get_nested, normalize_cluster_mode


INTENDED_DIMENSIONS = ("node_type", "engine", "engine_version")

CONTROL_VARIABLES: tuple[tuple[str, str], ...] = (
    ("memtier", "task_count"), ("memtier", "clients"), ("memtier", "threads"),
    ("memtier", "pipeline"), ("memtier", "data_size_bytes"), ("memtier", "ratio"),
    ("memtier", "key_pattern"), ("memtier", "key_maximum_total"),
    ("memtier", "test_time_seconds"),
    ("ecs", "fargate_cpu"), ("ecs", "fargate_memory"),
    ("elasticache", "cluster_mode_enabled"), ("elasticache", "num_cache_nodes"),
)

# An observed memtier window under this fraction of the *configured*
# test_time_seconds is a truncated run (crash, early kill, task replaced
# mid-run) -- not normal jitter. The 81-run corpus's max natural jitter
# between nominally identical 60-minute runs is 5.3% (PLAN_2.md WP4), an
# order of magnitude inside this margin. test_time_seconds == 0 means "run
# until ECS stops the task" (ecs.tf); there is no configured duration to
# truncate against, so that case is never flagged.
TRUNCATED_RUN_WINDOW_RATIO = 0.5

# The WP1 D1 double-written (new path, legacy path) pairs report_compare.py
# falls back to (see report_compare.METRICS' legacy_path=). Duplicated here
# in summary form rather than imported from report_compare, which imports
# build_comparison_contract from this module -- importing METRICS back would
# make the two modules circular.
RENAMED_METRIC_PATH_PAIRS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("client_latency", "task_median_p50_ms"), ("client_latency", "p50_ms")),
    (("client_latency", "task_median_p99_ms"), ("client_latency", "p99_ms")),
    (("client_latency", "task_median_p999_ms"), ("client_latency", "p999_ms")),
    (("client_latency", "worst_task_p99_ms"), ("client_latency", "worst_stream_p99_ms")),
    (("client_latency", "worst_task_p999_ms"), ("client_latency", "worst_stream_p999_ms")),
    (("network", "cache", "in_kib_per_sec"), ("network", "cache", "avg_in_kbs")),
    (("network", "cache", "out_kib_per_sec"), ("network", "cache", "avg_out_kbs")),
    (("network", "throttling", "bw_in_exceeded_count"), ("network", "throttling", "bw_in_exceeded_total")),
    (("network", "throttling", "bw_out_exceeded_count"), ("network", "throttling", "bw_out_exceeded_total")),
    (("network", "throttling", "pps_exceeded_count"), ("network", "throttling", "pps_exceeded_total")),
)


def _reason(code: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, **extra}


def coerce_control_value(field: str, value: Any) -> Any:
    """Normalize a control-variable value before comparing across runs.

    Pre-WP0 (thin) artifacts carry strings ("1", "false"); post-WP0 ones
    carry native JSON types (1, false). Without this, "1" != 1 would report
    a control_variable_differs that isn't real (PLAN_2.md WP4).
    """
    if value is None:
        return None
    if field == "cluster_mode_enabled":
        return normalize_cluster_mode(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def control_variable_value(run: RunData, section: str, field: str) -> Any:
    return get_nested(run.cluster_details or {}, (section, field))


def intended_dimension_value(run: RunData, dimension: str) -> Any:
    meta = run.summary.get("meta", {}) if isinstance(run.summary, dict) else {}
    if dimension == "engine":
        return meta.get("engine_type")
    return meta.get(dimension)


def _intended_dimensions_payload(baseline: RunData, candidate: RunData) -> dict[str, dict[str, Any]]:
    payload = {}
    for dimension in INTENDED_DIMENSIONS:
        baseline_value = intended_dimension_value(baseline, dimension)
        candidate_value = intended_dimension_value(candidate, dimension)
        payload[dimension] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "differs": bool(baseline_value) and bool(candidate_value) and baseline_value != candidate_value,
        }
    return payload


def run_window_seconds(run: RunData) -> float | None:
    from report_common import _parse_iso_timestamp

    meta = run.summary.get("meta", {}) if isinstance(run.summary, dict) else {}
    start = _parse_iso_timestamp(meta.get("report_start"))
    end = _parse_iso_timestamp(meta.get("report_end"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _configured_test_time_seconds(run: RunData) -> float | None:
    coerced = coerce_control_value(
        "test_time_seconds", control_variable_value(run, "memtier", "test_time_seconds")
    )
    return float(coerced) if isinstance(coerced, (int, float)) else None


def _is_truncated_run(run: RunData) -> bool:
    configured = _configured_test_time_seconds(run)
    if not configured or configured <= 0:
        return False
    observed = run_window_seconds(run)
    if observed is None:
        return False  # covered separately by memtier_window_missing
    return observed < configured * TRUNCATED_RUN_WINDOW_RATIO


def _loadgen_status(run: RunData) -> str | None:
    loadgen = run.summary.get("loadgen") if isinstance(run.summary, dict) else None
    if not loadgen:
        return None
    # validation_status/invalid_reasons is the pre-rename fallback also used
    # by report_compare.py's collect_takeaways -- see its comment there.
    return loadgen.get("diagnostic_status") or loadgen.get("validation_status")


def _task_count_mismatch(run: RunData) -> bool:
    loadgen = run.summary.get("loadgen") if isinstance(run.summary, dict) else None
    return bool(loadgen) and loadgen.get("task_count_matches_request") is False


def _any_metric_uses_legacy_fallback(baseline: RunData, candidate: RunData) -> bool:
    for new_path, legacy_path in RENAMED_METRIC_PATH_PAIRS:
        if get_nested(baseline.summary, new_path) is not None and get_nested(candidate.summary, new_path) is not None:
            continue
        if get_nested(baseline.summary, legacy_path) is not None or get_nested(candidate.summary, legacy_path) is not None:
            return True
    return False


def build_comparison_contract(baseline: RunData, candidate: RunData) -> dict[str, Any]:
    """-> {"verdict": "comparable"|"conditional"|"invalid", "reasons": [...],
           "intended_dimensions": {...}, "control_diffs": [...]}
    """
    intended = _intended_dimensions_payload(baseline, candidate)

    # invalid: a defect in one run's own data. Nothing about the *other*
    # run, and no amount of re-pairing, can fix these.
    invalid_reasons: list[dict[str, Any]] = []
    for run in (baseline, candidate):
        if _loadgen_status(run) == "invalid":
            invalid_reasons.append(_reason("diagnostic_status_invalid", role=run.role))
        if _task_count_mismatch(run):
            invalid_reasons.append(_reason("task_count_mismatch", role=run.role))
        window = run_window_seconds(run)
        if window is None:
            invalid_reasons.append(_reason("memtier_window_missing", role=run.role))
        elif _is_truncated_run(run):
            invalid_reasons.append(_reason("truncated_run", role=run.role))

    if invalid_reasons:
        return {
            "verdict": "invalid",
            "reasons": invalid_reasons,
            "intended_dimensions": intended,
            "control_diffs": [],
        }

    reasons: list[dict[str, Any]] = []

    differing_dimensions = [dim for dim, info in intended.items() if info["differs"]]
    if len(differing_dimensions) >= 2:
        reasons.append(_reason("multiple_intended_dimensions_differ", fields=differing_dimensions))

    if not intended["engine_version"]["differs"]:
        baseline_actual = get_nested(baseline.summary, ("meta", "engine_version_actual"))
        candidate_actual = get_nested(candidate.summary, ("meta", "engine_version_actual"))
        if baseline_actual and candidate_actual and baseline_actual != candidate_actual:
            reasons.append(_reason(
                "engine_version_actual_differs", baseline=baseline_actual, candidate=candidate_actual,
            ))

    control_known = all(
        control_variable_value(run, section, field) is not None
        for section, field in CONTROL_VARIABLES
        for run in (baseline, candidate)
    )
    if not control_known:
        reasons.append(_reason("control_variables_unknown"))
    else:
        for section, field in CONTROL_VARIABLES:
            baseline_value = coerce_control_value(field, control_variable_value(baseline, section, field))
            candidate_value = coerce_control_value(field, control_variable_value(candidate, section, field))
            if baseline_value != candidate_value:
                reasons.append(_reason(
                    "control_variable_differs",
                    field=f"{section}.{field}",
                    baseline=baseline_value,
                    candidate=candidate_value,
                ))

    baseline_status = _loadgen_status(baseline)
    candidate_status = _loadgen_status(candidate)
    if baseline_status is None or candidate_status is None:
        reasons.append(_reason("loadgen_unknown"))
    else:
        for run, status in ((baseline, baseline_status), (candidate, candidate_status)):
            if status == "warning":
                reasons.append(_reason("diagnostic_warning", role=run.role))

    baseline_schema = get_nested(baseline.summary, ("meta", "generator_schema_version"))
    candidate_schema = get_nested(candidate.summary, ("meta", "generator_schema_version"))
    if baseline_schema != candidate_schema:
        reasons.append(_reason("schema_version_differs", baseline=baseline_schema, candidate=candidate_schema))

    if _any_metric_uses_legacy_fallback(baseline, candidate):
        reasons.append(_reason("metric_only_in_legacy_variant"))

    return {
        "verdict": "conditional" if reasons else "comparable",
        "reasons": reasons,
        "intended_dimensions": intended,
        "control_diffs": [r for r in reasons if r["code"] == "control_variable_differs"],
    }
