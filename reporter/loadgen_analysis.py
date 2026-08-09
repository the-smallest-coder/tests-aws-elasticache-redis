"""Per-task and per-AZ load-generator quality analysis."""

from __future__ import annotations

import re

import pandas as pd

from helpers import elasticache_availability_zone, ecs_task_index_map


GENERATOR_CPU_THRESHOLD_PCT = 85.0
WITHIN_AZ_SKEW_THRESHOLD = 1.3


def _task_id(value: object) -> str:
    text = str(value or "")
    return text.replace("\\", "/").rsplit("/", 1)[-1]


def _dimension_value(dimensions: object, name: str) -> str | None:
    match = re.search(rf"(?:^|;){re.escape(name)}=([^;]+)", str(dimensions or ""))
    return match.group(1) if match else None


def _clip(df: pd.DataFrame, start, end) -> pd.DataFrame:
    if df is None or df.empty or "Timestamp" not in df.columns:
        return pd.DataFrame() if df is None else df.copy()
    result = df.copy()
    result["Timestamp"] = pd.to_datetime(result["Timestamp"], utc=True).dt.tz_localize(None)
    return result[(result["Timestamp"] >= start) & (result["Timestamp"] <= end)].copy()


def _preferred_task_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate the multiple Container Insights dimension sets per task."""
    if df.empty:
        return df
    rows = df.copy()
    dims = rows["Dimensions"].astype(str)
    rows["TaskId"] = dims.map(lambda value: _dimension_value(value, "TaskId"))
    rows["AvailabilityZone"] = dims.map(
        lambda value: _dimension_value(value, "AvailabilityZone") or "unknown"
    )
    rows = rows[rows["TaskId"].notna()].copy()
    rows["_dimension_rank"] = (
        dims.str.contains(r"(?:^|;)ServiceName=", regex=True, na=False).astype(int) * 2
        + dims.str.contains(r"(?:^|;)AvailabilityZone=", regex=True, na=False).astype(int)
    )
    return (
        rows.sort_values(["Timestamp", "TaskId", "_dimension_rank"])
        .drop_duplicates(["Timestamp", "TaskId"], keep="last")
        .drop(columns="_dimension_rank")
    )


def _task_cpu_from_ecs(ecs_df: pd.DataFrame) -> pd.DataFrame:
    columns = ["Timestamp", "TaskId", "AvailabilityZone", "CpuUtilized", "CpuReserved", "Stat"]
    if ecs_df is None or ecs_df.empty:
        return pd.DataFrame(columns=columns)

    utilized = ecs_df[
        (ecs_df["MetricName"] == "CpuUtilized") & (ecs_df["Stat"] == "Average")
    ].copy()
    reserved = ecs_df[
        (ecs_df["MetricName"] == "CpuReserved") & (ecs_df["Stat"] == "Average")
    ].copy()
    utilized = _preferred_task_rows(utilized)
    reserved = _preferred_task_rows(reserved)
    if utilized.empty or reserved.empty:
        return pd.DataFrame(columns=columns)

    utilized = utilized.rename(columns={"Value": "CpuUtilized"})
    reserved = reserved.rename(columns={"Value": "CpuReserved"})
    joined = utilized[["Timestamp", "TaskId", "AvailabilityZone", "CpuUtilized"]].merge(
        reserved[["Timestamp", "TaskId", "AvailabilityZone", "CpuReserved"]],
        on=["Timestamp", "TaskId"],
        how="inner",
        suffixes=("", "_reserved"),
    )
    joined["AvailabilityZone"] = joined["AvailabilityZone"].where(
        joined["AvailabilityZone"] != "unknown", joined["AvailabilityZone_reserved"]
    )
    joined["Stat"] = "Average"
    return joined[columns]


def _running_task_count(ecs_df: pd.DataFrame, ci_service_df: pd.DataFrame, start, end) -> int | None:
    def representative_count(values: pd.Series) -> int | None:
        counts = pd.to_numeric(values, errors="coerce").dropna()
        counts = counts[counts > 0].round().astype(int)
        if counts.empty:
            return None
        # Mode ignores short-lived deployment transitions (for example 6 -> 7
        # -> 6).  In the unlikely event of a tie, prefer the lower mode so a
        # transient scale-up cannot invalidate the whole report window.
        return int(counts.mode().min())

    service = _clip(ci_service_df, start, end)
    if not service.empty:
        count = representative_count(service["RunningTaskCount"])
        if count is not None:
            return count

    if ecs_df is not None and not ecs_df.empty:
        rows = ecs_df[
            (ecs_df["MetricName"] == "RunningTaskCount") & (ecs_df["Stat"] == "Average")
        ]
        rows = _clip(rows, start, end)
        if not rows.empty:
            count = representative_count(rows["Value"])
            if count is not None:
                return count
    return None


def _ratio_p90_p10(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    p10 = float(numeric.quantile(0.10))
    if p10 <= 0:
        return None
    return float(numeric.quantile(0.90)) / p10


def _rounded(value: float | None, digits: int = 3):
    return round(float(value), digits) if value is not None else None


def _task_latency_map(memtier_totals_df: pd.DataFrame) -> dict[str, float]:
    """Map ECS task ID -> final memtier p50 latency (ms) for AZ inference.

    ``memtier_totals_df["source"]`` identifies the stream two different
    ways depending on how it was loaded: a ``*.totals.json`` artifact path
    (local/uploaded sidecar files) or the payload's own ``stream_id`` string
    (in-memory reconstruction from raw logs), e.g.::

        .../logs/loadgen/memtier/memtier/<task-id>.totals.json
        memtier/memtier/<task-id>

    Stripping a trailing ``.totals.json`` and then any ``.../`` prefix with
    ``_task_id`` normalizes either shape to the bare ECS task ID, which is
    the same ID Container Insights reports - no separate translation table
    is needed.
    """
    required = {"source", "p50_latency_ms"}
    if memtier_totals_df is None or memtier_totals_df.empty or not required.issubset(memtier_totals_df.columns):
        return {}
    df = memtier_totals_df.copy()
    stream = df["source"].astype(str).str.replace(r"\.totals\.json$", "", regex=True)
    df["TaskId"] = stream.map(_task_id)
    df["p50_latency_ms"] = pd.to_numeric(df["p50_latency_ms"], errors="coerce")
    df = df[df["TaskId"].astype(bool)].dropna(subset=["p50_latency_ms"])
    return dict(zip(df["TaskId"], df["p50_latency_ms"]))


def build_loadgen_summary(
    memtier_samples_df: pd.DataFrame,
    memtier_minute_df: pd.DataFrame,
    ecs_df: pd.DataFrame,
    ci_task_df: pd.DataFrame,
    ci_service_df: pd.DataFrame,
    report_start,
    report_end,
    measured_elasticache_az=None,
    memtier_totals_df: pd.DataFrame | None = None,
) -> dict:
    """Build load-generator validity metrics using only complete absolute minutes."""
    samples = _clip(memtier_samples_df, report_start, report_end)
    if samples.empty:
        return {}
    samples["TaskId"] = samples["Stream"].map(_task_id)
    samples["minute_utc"] = samples["Timestamp"].dt.floor("min")

    stream_count = int(samples["TaskId"].nunique())
    expected_task_count = _running_task_count(ecs_df, ci_service_df, report_start, report_end) or stream_count

    if memtier_minute_df is not None and not memtier_minute_df.empty and "task_count_present" in memtier_minute_df:
        minute_counts = memtier_minute_df[["Timestamp", "task_count_present"]].copy()
        minute_counts["Timestamp"] = pd.to_datetime(minute_counts["Timestamp"], utc=True).dt.tz_localize(None)
        start_minute = pd.Timestamp(report_start).floor("min")
        end_minute = pd.Timestamp(report_end).floor("min")
        minute_counts = minute_counts[
            (minute_counts["Timestamp"] >= start_minute) & (minute_counts["Timestamp"] <= end_minute)
        ]
        minute_counts = minute_counts.rename(columns={"Timestamp": "minute_utc"})
    else:
        minute_counts = (
            samples.groupby("minute_utc", as_index=False)["TaskId"]
            .nunique()
            .rename(columns={"TaskId": "task_count_present"})
        )

    minute_counts["task_count_present"] = pd.to_numeric(
        minute_counts["task_count_present"], errors="coerce"
    )
    minute_counts["is_full_absolute_minute"] = (
        (minute_counts["minute_utc"] >= pd.Timestamp(report_start).ceil("min"))
        & (minute_counts["minute_utc"] + pd.Timedelta(minutes=1) <= pd.Timestamp(report_end))
    )
    minute_counts["has_expected_tasks"] = minute_counts["task_count_present"] >= expected_task_count
    boundary_mask = ~minute_counts["is_full_absolute_minute"]
    missing_task_any_mask = ~minute_counts["has_expected_tasks"]
    missing_task_full_minute_mask = (
        minute_counts["is_full_absolute_minute"] & missing_task_any_mask
    )
    discarded_mask = boundary_mask | missing_task_full_minute_mask
    complete_minutes = minute_counts[~discarded_mask]["minute_utc"].drop_duplicates()
    complete_minute_set = set(pd.to_datetime(complete_minutes).tolist())
    complete_samples = samples[samples["minute_utc"].isin(complete_minute_set)].copy()

    ci_cpu = _clip(ci_task_df, report_start, report_end)
    if ci_cpu.empty:
        ci_cpu = _clip(_task_cpu_from_ecs(ecs_df), report_start, report_end)
    az_by_task = {}
    if not ci_cpu.empty:
        ci_cpu["TaskId"] = ci_cpu["TaskId"].map(_task_id)
        known_az = ci_cpu[ci_cpu["AvailabilityZone"].astype(str) != "unknown"]
        az_by_task = known_az.groupby("TaskId")["AvailabilityZone"].last().to_dict()
        ci_cpu["minute_utc"] = ci_cpu["Timestamp"].dt.floor("min")
        ci_cpu["CpuUtilized"] = pd.to_numeric(ci_cpu["CpuUtilized"], errors="coerce")
        ci_cpu["CpuReserved"] = pd.to_numeric(ci_cpu["CpuReserved"], errors="coerce")
        ci_cpu = ci_cpu[
            (ci_cpu["Stat"] == "Average")
            & (ci_cpu["CpuUtilized"] > 0)
            & (ci_cpu["CpuReserved"] > 0)
            & ci_cpu["minute_utc"].isin(complete_minute_set)
        ].copy()
        ci_cpu["cpu_pct"] = ci_cpu["CpuUtilized"] / ci_cpu["CpuReserved"] * 100.0

    cpu_vector = []
    if not ci_cpu.empty:
        for task_id, group in ci_cpu.groupby("TaskId", sort=True):
            cpu_vector.append(
                {
                    "task_id": task_id,
                    "availability_zone": az_by_task.get(task_id, "unknown"),
                    "p95_pct": _rounded(float(group["cpu_pct"].quantile(0.95))),
                    "sample_count": int(len(group)),
                }
            )

    throughput_vector = []
    if not complete_samples.empty:
        for task_id, group in complete_samples.groupby("TaskId", sort=True):
            throughput_vector.append(
                {
                    "task_id": task_id,
                    "availability_zone": az_by_task.get(task_id, "unknown"),
                    "median_ops_sec": _rounded(float(group["Ops/sec"].median())),
                    "sample_count": int(len(group)),
                }
            )

    elasticache_az = elasticache_availability_zone(
        task_latency_map=_task_latency_map(memtier_totals_df),
        task_az_map=az_by_task,
        measured_availability_zone=measured_elasticache_az,
    )
    task_indexes = ecs_task_index_map(
        {
            row['task_id']
            for row in cpu_vector + throughput_vector
        },
        task_az_map=az_by_task,
        elasticache_az=elasticache_az['availability_zone'],
    )
    for vector in (cpu_vector, throughput_vector):
        for row in vector:
            row['task_index'] = task_indexes[row['task_id']]
        vector.sort(key=lambda row: (row['task_index'], row['task_id']))

    throughput_df = pd.DataFrame(throughput_vector)
    fleet_ratio = None
    within_az_max = None
    between_az_ratio = None
    az_vector = []
    if not throughput_df.empty:
        fleet_ratio = _ratio_p90_p10(throughput_df["median_ops_sec"])
        az_groups = list(throughput_df.groupby("availability_zone", sort=True))
        az_groups.sort(key=lambda item: (
            0 if item[0] == elasticache_az['availability_zone'] else 1,
            item[0] == 'unknown',
            item[0],
        ))
        for az, group in az_groups:
            az_vector.append(
                {
                    "availability_zone": az,
                    "task_count": int(len(group)),
                    "median_ops_sec": _rounded(float(group["median_ops_sec"].median())),
                    "p90_to_p10": _rounded(_ratio_p90_p10(group["median_ops_sec"])),
                }
            )
        missing_az_tasks = sorted(
            throughput_df.loc[
                throughput_df["availability_zone"].astype(str).eq("unknown"), "task_id"
            ].astype(str).tolist()
        )
        known_az = pd.DataFrame([row for row in az_vector if row["availability_zone"] != "unknown"])
        if not missing_az_tasks and not known_az.empty:
            within_az_max = float(known_az["p90_to_p10"].max())
            az_medians = known_az["median_ops_sec"]
            if len(az_medians) >= 2 and float(az_medians.min()) > 0:
                between_az_ratio = float(az_medians.max()) / float(az_medians.min())
    else:
        missing_az_tasks = []

    generator_cpu_p95 = max((row["p95_pct"] for row in cpu_vector), default=None)
    cpu_p95_values = pd.Series(
        [row["p95_pct"] for row in cpu_vector], dtype="float64"
    ).dropna()
    generator_cpu_across_tasks = None
    if not cpu_p95_values.empty:
        generator_cpu_across_tasks = {
            "min": _rounded(float(cpu_p95_values.min())),
            "median": _rounded(float(cpu_p95_values.median())),
            "max": _rounded(float(cpu_p95_values.max())),
        }
    generator_limited = generator_cpu_p95 is not None and generator_cpu_p95 > GENERATOR_CPU_THRESHOLD_PCT
    within_az_warning = within_az_max is not None and within_az_max > WITHIN_AZ_SKEW_THRESHOLD
    reasons = []
    if generator_limited:
        reasons.append("generator_cpu_p95_above_85_pct")
    if within_az_warning:
        reasons.append("throughput_skew_within_az_above_1_3")
    unknown_reasons = []
    if missing_az_tasks:
        unknown_reasons.append("availability_zone_missing")
    if elasticache_az["source"] == "unavailable":
        unknown_reasons.append("elasticache_availability_zone_unavailable")
    elif elasticache_az["source"] == "ambiguous":
        unknown_reasons.append("elasticache_availability_zone_ambiguous")

    has_cpu = generator_cpu_p95 is not None
    has_throughput = (
        fleet_ratio is not None
        and within_az_max is not None
        and not missing_az_tasks
    )
    status = (
        "warning" if reasons
        else ("unknown" if unknown_reasons else ("ok" if has_cpu and has_throughput else "unknown"))
    )
    # `status` used to be "invalid"/"valid" with reasons under
    # "invalid_reasons"; a run over the generator-CPU/within-AZ-skew
    # thresholds does not invalidate the ElastiCache-side result, only ECS
    # task latency conclusions (see latency_tail_valid below), so this was
    # renamed to "warning"/"ok" with reasons under "warning_reasons".
    # `diagnostic_status` was added as the new canonical name at the same
    # time -- `validation_status` is no longer written here, but
    # report_compare.py still reads it (and invalid_reasons) as a fallback
    # for summaries generated before this rename. Do not remove that
    # fallback without checking for pre-rename results_*.json files first.
    return {
        "diagnostic_status": status,
        "warning_reasons": reasons,
        "unknown_reasons": unknown_reasons,
        "latency_tail_valid": not generator_limited if has_cpu else None,
        "expected_task_count": expected_task_count,
        "memtier_stream_count": stream_count,
        "complete_minute_count": int(len(complete_minute_set)),
        "discarded_incomplete_minute_count": int(discarded_mask.sum()),
        "discarded_partial_boundary_minute_count": int(boundary_mask.sum()),
        "discarded_missing_task_minute_count": int(missing_task_full_minute_mask.sum()),
        "minutes_below_expected_task_count": int(missing_task_any_mask.sum()),
        "generator_cpu_p95_pct": _rounded(generator_cpu_p95),
        "generator_cpu_threshold_pct": GENERATOR_CPU_THRESHOLD_PCT,
        "generator_cpu_limited": generator_limited if has_cpu else None,
        "generator_cpu_across_tasks": generator_cpu_across_tasks,
        "generator_cpu_p95_by_task": cpu_vector,
        "elasticache_availability_zone": elasticache_az["availability_zone"],
        "elasticache_availability_zone_source": elasticache_az["source"],
        "throughput_task_skew_p90_to_p10": _rounded(fleet_ratio),
        "throughput_skew_within_az_max": _rounded(within_az_max),
        "throughput_skew_within_az_threshold": WITHIN_AZ_SKEW_THRESHOLD,
        "throughput_skew_between_az_max_to_min": _rounded(between_az_ratio),
        "throughput_median_by_task": throughput_vector,
        "throughput_by_az": az_vector,
    }
