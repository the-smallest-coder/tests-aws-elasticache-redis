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
# order of magnitude inside this margin. "Run until ECS stops the task"
# (loadgen_memtier_test_time == 0, ecs.tf's default) has no configured
# duration to truncate against, so that case must never be flagged -- but
# test_time_seconds itself can't signal it: memtier's --test-time needs a
# positive integer, so ecs.tf:194 substitutes 2147483647, not 0, and that
# is what every artifact this rig produces under the default actually
# carries. duration_label (node_details.tf:89) is explicit about the same
# thing instead of relying on a magic number in test_time_seconds.
TRUNCATED_RUN_WINDOW_RATIO = 0.5


def _reason(code: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, **extra}


def coerce_control_value(field: str, value: Any) -> Any:
    """Normalize a control-variable value before comparing across runs.

    Pre-WP0 (thin) artifacts carry strings ("1", "false"); post-WP0 ones
    carry native JSON types (1, false). Without this, "1" != 1 would report
    a control_variable_differs that isn't real (PLAN_2.md WP4). Numeric
    values are further collapsed to int when they carry no fraction (1 and
    1.0 must produce the same value): both aggregator.py's fingerprint and
    this module's own control_variable_differs check compare/hash the
    result, and int(1) == float(1.0) is not enough when the two feed
    repr()-based hashing rather than ==.
    """
    if value is None:
        return None
    if field == "cluster_mode_enabled":
        return normalize_cluster_mode(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) if isinstance(value, float) and value.is_integer() else value
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        as_float = float(text)
        return int(as_float) if as_float.is_integer() else as_float
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


def _runs_until_stopped(run: RunData) -> bool:
    """True when memtier was configured to run until the ECS shutdown Lambda
    ends it, not for a fixed duration -- see the TRUNCATED_RUN_WINDOW_RATIO
    comment above for why test_time_seconds alone can't tell this apart from
    a genuine ~68-year configured run.
    """
    return control_variable_value(run, "memtier", "duration_label") == "until stopped"


def _is_truncated_run(run: RunData) -> bool:
    if _runs_until_stopped(run):
        return False
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


#: reason codes that force verdict == "invalid" regardless of what else is
#: in `reasons` -- everything else is "conditional"-tier.
_INVALID_CODES = frozenset({
    "diagnostic_status_invalid", "task_count_mismatch", "memtier_window_missing", "truncated_run",
})


def build_comparison_contract(baseline: RunData, candidate: RunData) -> dict[str, Any]:
    """-> {"verdict": "comparable"|"conditional"|"invalid", "reasons": [...],
           "intended_dimensions": {...}, "control_diffs": [...]}

    All reasons are always computed, regardless of verdict: an invalid pair
    still shows its control-variable diffs and every other conditional-tier
    finding alongside the invalid one(s). The verdict is the single most
    severe classification present, not a short-circuit that hides the rest
    of the picture -- a reader debugging "why is this invalid" also wants
    "and what else differs" in the same view.
    """
    intended = _intended_dimensions_payload(baseline, candidate)
    reasons: list[dict[str, Any]] = []

    # invalid-tier: a defect in one run's own data. Nothing about the
    # *other* run, and no amount of re-pairing, can fix these.
    for run in (baseline, candidate):
        if _loadgen_status(run) == "invalid":
            reasons.append(_reason("diagnostic_status_invalid", role=run.role))
        if _task_count_mismatch(run):
            reasons.append(_reason("task_count_mismatch", role=run.role))
        window = run_window_seconds(run)
        if window is None:
            reasons.append(_reason("memtier_window_missing", role=run.role))
        elif _is_truncated_run(run):
            reasons.append(_reason("truncated_run", role=run.role))

    # conditional-tier: computed unconditionally (see docstring), even when
    # an invalid-tier reason above already decides the verdict.
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

    if any(reason["code"] in _INVALID_CODES for reason in reasons):
        verdict = "invalid"
    elif reasons:
        verdict = "conditional"
    else:
        verdict = "comparable"

    return {
        "verdict": verdict,
        "reasons": reasons,
        "intended_dimensions": intended,
        "control_diffs": [r for r in reasons if r["code"] == "control_variable_differs"],
    }
