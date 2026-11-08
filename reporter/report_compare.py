from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from comparison_contract import CONTROL_VARIABLES, INTENDED_DIMENSIONS, VERDICTS, build_comparison_contract
from formatting import format_gib, format_usd_hour
from report_common import (
    MetricSpec,
    RunData,
    bytes_to_mb,
    diff_points,
    display_value,
    format_number,
    get_nested,
    load_run,
    metric_value,
    normalize_cluster_mode,
    percent_change,
    public_run_warnings,
)
from report_safety import (  # noqa: F401  (ReportGenerationError is re-exported for callers)
    WITHHELD,
    ReportGenerationError,
    assert_publishable_html,
    diagnostics,
    publication_guard,
    public_cluster_details,
    public_iso_timestamp,
    public_label,
    public_number,
    public_run_config,
    public_scalar,
    public_time_range,
    public_token,
    write_files_atomically,
)
from template import CONTRACT_REASON_CODES, render_report


SECTION_META: dict[str, dict[str, str]] = {
    "benchmark": {"title": "Benchmark Summary", "badge": "memtier"},
    "engine_memory": {"title": "Engine and Memory", "badge": "infra"},
    "cache_latency": {"title": "Cache, Latency, Connections", "badge": "behavior"},
    "network_ecs": {"title": "ECS Infrastructure", "badge": "tasks + AZ"},
}


def _format_gib(byte_value: Any) -> str | None:
    text = format_gib(byte_value)
    return f"{text} GiB" if text else None


def _format_usd_hour(value: Any) -> str | None:
    text = format_usd_hour(value)
    return f"{text}/h" if text else None


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("benchmark", "Avg Throughput", ("benchmark", "avg_ops"), "ops/sec", 1, "higher", "Average observed memtier throughput."),
    MetricSpec("benchmark", "Peak Throughput", ("benchmark", "peak_ops"), "ops/sec", 1, "higher", "Highest single observed throughput value."),
    MetricSpec("benchmark", "Throughput CV", ("benchmark", "cv_pct"), "%", 2, "lower", "Coefficient of variation. Lower values mean steadier throughput.", "points"),
    MetricSpec("benchmark", "Avg Latency", ("benchmark", "avg_latency_ms"), "ms", 3, "lower", "Average client-side latency reported by memtier."),
    MetricSpec("benchmark", "Max Latency", ("benchmark", "max_latency_ms"), "ms", 2, "lower", "Worst client-side latency observed during the run."),
    MetricSpec("benchmark", "P95 Latency", ("benchmark", "p95_latency_ms"), "ms", 2, "lower", "95th percentile client-side latency."),
    MetricSpec("benchmark", "P99 Latency", ("benchmark", "p99_latency_ms"), "ms", 2, "lower", "99th percentile client-side latency."),
    MetricSpec(
        "benchmark", "Avg Bandwidth", ("benchmark", "total_bandwidth_kbs"),
        "KB/s", 2, "neutral", "Sum of network throughput reported by memtier across streams.",
    ),
    MetricSpec("engine_memory", "Avg Engine CPU", ("engine_cpu", "avg_pct"), "%", 2, "lower", "Average Redis engine CPU utilization.", "points"),
    MetricSpec("engine_memory", "Peak Engine CPU", ("engine_cpu", "max_pct"), "%", 2, "lower", "Highest Redis engine CPU utilization.", "points"),
    MetricSpec("engine_memory", "Avg CPU Credit Balance", ("engine_cpu", "credit_balance_avg"), "credits", 2, "higher", "Average burst credit balance for T-family cache nodes."),
    MetricSpec("engine_memory", "Min CPU Credit Balance", ("engine_cpu", "credit_balance_min"), "credits", 2, "higher", "Lowest burst credit balance reached during the run."),
    MetricSpec("engine_memory", "Avg CPU Credit Usage", ("engine_cpu", "credit_usage_avg"), "vCPU-min", 4, "neutral", "Average CPU credit usage rate."),
    MetricSpec("engine_memory", "Avg Memory Usage", ("memory", "avg_usage_pct"), "%", 2, "lower", "Average cache memory usage relative to node capacity.", "points"),
    MetricSpec("engine_memory", "Max Memory Usage", ("memory", "max_usage_pct"), "%", 2, "lower", "Peak cache memory usage relative to node capacity.", "points"),
    MetricSpec("engine_memory", "Memory Headroom", ("memory", "headroom_pct"), "%", 2, "higher", "100 minus peak memory usage. Negative values indicate pressure.", "points"),
    MetricSpec("engine_memory", "Avg Fragmentation", ("memory", "fragmentation_avg"), "x", 3, "lower", "Average memory fragmentation ratio."),
    MetricSpec("engine_memory", "Peak Fragmentation", ("memory", "fragmentation_max"), "x", 2, "lower", "Highest memory fragmentation ratio."),
    MetricSpec("engine_memory", "Peak Swap", ("memory", "swap_max_bytes"), "MB", 1, "lower", "Highest swap consumption observed on the cache node.", normalizer=bytes_to_mb),
    MetricSpec("cache_latency", "Cache Hit Rate", ("cache_efficiency", "avg_hit_rate_pct"), "%", 2, "higher", "Average CacheHitRate during the report window.", "points"),
    MetricSpec("cache_latency", "Total Evictions", ("cache_efficiency", "total_evictions"), "", 0, "lower", "Total key evictions reported by CloudWatch."),
    MetricSpec("cache_latency", "OOM Rejections", ("oom", "rejection_count"), "", 0, "lower", "Total memtier -OOM command rejections."),
    MetricSpec("cache_latency", "Min Freeable Memory", ("cache_efficiency", "min_freeable_memory_mb"), "MB", 1, "higher", "Lowest freeable memory value seen during the run."),
    MetricSpec("cache_latency", "Peak Key Count", ("cache_efficiency", "peak_key_count"), "", 0, "neutral", "Maximum total key count recorded on the cache node."),
    MetricSpec("cache_latency", "GET Latency", ("latency_server_us", "get_avg"), "us", 3, "lower", "Average server-side GET command latency."),
    MetricSpec("cache_latency", "SET Latency", ("latency_server_us", "set_avg"), "us", 3, "lower", "Average server-side SET command latency."),
    MetricSpec("cache_latency", "String Latency", ("latency_server_us", "string_avg"), "us", 3, "lower", "Average server-side string command latency."),
    MetricSpec("cache_latency", "Avg Connections", ("connections", "avg"), "", 1, "neutral", "Average concurrent connections on the cache node."),
    MetricSpec("cache_latency", "Peak Connections", ("connections", "max"), "", 1, "neutral", "Highest concurrent connection count."),
    # Two independent measurements, not a rename pair (see summary.py's
    # client_latency comment): memtier-Totals-based (task_median_*/worst_task_*)
    # and CloudWatch-EMF-based (p50_ms/p99_ms/etc., further below). Different
    # collection paths and different failure modes -- one being unavailable
    # says nothing about the other.
    MetricSpec(
        "network_ecs", "ECS Task Latency p50 (Totals)", ("client_latency", "task_median_p50_ms"),
        "ms", 3, "lower", "Median across each task's final memtier p50 latency total.",
    ),
    MetricSpec(
        "network_ecs", "ECS Task Latency p99 (Totals)", ("client_latency", "task_median_p99_ms"),
        "ms", 3, "lower", "Median across each task's final memtier p99 latency total.",
    ),
    MetricSpec(
        "network_ecs", "ECS Task Latency p99.9 (Totals)", ("client_latency", "task_median_p999_ms"),
        "ms", 3, "lower", "Median across each task's final memtier p99.9 latency total.",
    ),
    MetricSpec(
        "network_ecs", "Worst ECS Task Latency p99 (Totals)", ("client_latency", "worst_task_p99_ms"),
        "ms", 3, "lower", "Maximum across each task's final memtier p99 latency total.",
    ),
    MetricSpec(
        "network_ecs", "Worst ECS Task Latency p99.9 (Totals)", ("client_latency", "worst_task_p999_ms"),
        "ms", 3, "lower", "Maximum across each task's final memtier p99.9 latency total.",
    ),
    MetricSpec(
        "network_ecs", "ECS Task Latency p50 (EMF)", ("client_latency", "p50_ms"),
        "ms", 3, "lower", "Mean of per-minute CloudWatch EMF p50 latency across tasks -- an "
        "average of percentiles, not one; independent collection path from the memtier-Totals row above.",
    ),
    MetricSpec(
        "network_ecs", "ECS Task Latency p99 (EMF)", ("client_latency", "p99_ms"),
        "ms", 3, "lower", "Mean of per-minute CloudWatch EMF p99 latency across tasks -- an "
        "average of percentiles, not one; independent collection path from the memtier-Totals row above.",
    ),
    MetricSpec(
        "network_ecs", "ECS Task Latency p99.9 (EMF)", ("client_latency", "p999_ms"),
        "ms", 3, "lower", "Mean of per-minute CloudWatch EMF p99.9 latency across tasks -- an "
        "average of percentiles, not one; independent collection path from the memtier-Totals row above.",
    ),
    MetricSpec(
        "network_ecs", "Worst ECS Task Latency p99 (EMF)", ("client_latency", "worst_p99_ms"),
        "ms", 3, "lower", "Maximum per-minute CloudWatch EMF p99 latency across tasks.",
    ),
    MetricSpec(
        "network_ecs", "Worst ECS Task Latency p99.9 (EMF)", ("client_latency", "worst_p999_ms"),
        "ms", 3, "lower", "Maximum per-minute CloudWatch EMF p99.9 latency across tasks.",
    ),
    MetricSpec(
        "network_ecs", "ECS Service CPU — Time Average", ("ecs", "service_cpu_time_avg_pct"),
        "%", 2, "neutral", "Time average of service-level CPUUtilization; this is not an average across tasks.", "points",
    ),
    MetricSpec(
        "network_ecs", "ECS Service CPU — Time Peak", ("ecs", "service_cpu_time_peak_pct"),
        "%", 2, "lower", "Maximum over time of service-level CPUUtilization; this is not the worst task.", "points",
    ),
    MetricSpec(
        "network_ecs", "Task CPU p95 — Minimum Task", ("loadgen", "generator_cpu_across_tasks", "min"),
        "%", 2, "lower", "Minimum across the per-task CPU p95 values; ramp-up minima are not used.", "points",
    ),
    MetricSpec(
        "network_ecs", "Task CPU p95 — Median Task", ("loadgen", "generator_cpu_across_tasks", "median"),
        "%", 2, "lower", "Median across the per-task CPU p95 values.", "points",
    ),
    MetricSpec(
        "network_ecs", "Task CPU p95 — Maximum Task", ("loadgen", "generator_cpu_across_tasks", "max"),
        "%", 2, "lower", "Maximum across per-task p95(CpuUtilized / CpuReserved * 100); above 85% invalidates latency/tail conclusions.",
        "points", warning_above=85.0,
    ),
    MetricSpec(
        "network_ecs", "Fleet Task Throughput Skew", ("loadgen", "throughput_task_skew_p90_to_p10"),
        "p90/p10", 3, "lower", "p90/p10 across per-task median current ops/sec; cross-AZ placement is intentionally included and has no pass/fail threshold.",
    ),
    MetricSpec(
        "network_ecs", "Worst Within-AZ Throughput Skew", ("loadgen", "throughput_skew_within_az_max"),
        "p90/p10", 3, "lower", "Maximum p90/p10 across ECS tasks within the same AZ; above 1.3 indicates an ECS task imbalance.",
        warning_above=1.3,
    ),
    MetricSpec(
        "network_ecs", "Between-AZ Throughput Ratio", ("loadgen", "throughput_skew_between_az_max_to_min"),
        "max/min", 3, "lower", "Max/min across AZ median task throughput. Reported as a cross-AZ result without a pass/fail threshold.",
    ),
    MetricSpec(
        "network_ecs", "Avg Cache In", ("network", "cache", "in_kib_per_sec"),
        "KiB/s", 2, "neutral", "Average inbound network throughput on the cache node.",
    ),
    MetricSpec(
        "network_ecs", "Avg Cache Out", ("network", "cache", "out_kib_per_sec"),
        "KiB/s", 2, "neutral", "Average outbound network throughput on the cache node.",
    ),
    MetricSpec(
        "network_ecs", "BW In Throttle Events", ("network", "throttling", "bw_in_exceeded_count"),
        "", 0, "lower", "Total bandwidth-in throttle events.",
    ),
    MetricSpec(
        "network_ecs", "BW Out Throttle Events", ("network", "throttling", "bw_out_exceeded_count"),
        "", 0, "lower", "Total bandwidth-out throttle events.",
    ),
    MetricSpec(
        "network_ecs", "PPS Throttle Events", ("network", "throttling", "pps_exceeded_count"),
        "", 0, "lower", "Total packets-per-second throttle events.",
    ),
    MetricSpec("network_ecs", "Peak ECS Memory", ("ecs", "peak_mem_mb"), "MB", 1, "lower", "Peak ECS task memory usage."),
)


TOPLINE_PATHS: tuple[tuple[str, ...], ...] = (
    ("benchmark", "avg_ops"),
    ("benchmark", "avg_latency_ms"),
    ("cache_efficiency", "avg_hit_rate_pct"),
    ("memory", "max_usage_pct"),
    ("memory", "headroom_pct"),
    ("engine_cpu", "max_pct"),
)


def classify_delta(spec: MetricSpec, baseline: float | None, candidate: float | None) -> str:
    if baseline is None or candidate is None:
        return "neutral"
    # Use a stricter absolute tolerance for integer-like metrics (decimals == 0)
    # so that changes like 0 -> 1 are not treated as neutral.
    if spec.decimals == 0:
        abs_tol = 0.5
    else:
        abs_tol = max(0.01, 10 ** (-spec.decimals))
    if math.isclose(baseline, candidate, rel_tol=0.005, abs_tol=abs_tol):
        return "neutral"
    if spec.direction == "neutral":
        return "neutral"
    improved = candidate > baseline if spec.direction == "higher" else candidate < baseline
    return "better" if improved else "worse"


def format_delta(spec: MetricSpec, baseline: float | None, candidate: float | None) -> str:
    if baseline is None or candidate is None:
        return "n/a"
    diff = candidate - baseline
    if spec.delta_mode == "points":
        return f"{diff:+.{spec.decimals}f} pp"
    suffix = f" {spec.unit}" if spec.unit else ""
    text = f"{diff:+.{spec.decimals}f}{suffix}"
    if baseline != 0:
        text += f" ({(diff / baseline) * 100:+.1f}%)"
    return text


def metric_rows(baseline: RunData, candidate: RunData) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    latency_invalid = any(
        get_nested(run.summary, ("loadgen", "latency_tail_valid")) is False
        for run in (baseline, candidate)
    )
    for spec in METRICS:
        baseline_raw = get_nested(baseline.summary, spec.path)
        candidate_raw = get_nested(candidate.summary, spec.path)
        baseline_value = metric_value(spec, baseline_raw)
        candidate_value = metric_value(spec, candidate_raw)
        tone = classify_delta(spec, baseline_value, candidate_value)
        if spec.warning_above is not None and any(
            value is not None and value > spec.warning_above
            for value in (baseline_value, candidate_value)
        ):
            tone = "warning"
        if spec.path and spec.path[0] == "client_latency" and latency_invalid:
            tone = "warning"
        rows.append(
            {
                "section": spec.section,
                "label": spec.label,
                "baseline": display_value(spec, baseline_raw),
                "candidate": display_value(spec, candidate_raw),
                "delta": format_delta(spec, baseline_value, candidate_value),
                "tone": tone,
                "description": spec.description,
                "path": spec.path,
            }
        )
    return rows


def build_sections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for section_key, meta in SECTION_META.items():
        sections.append(
            {
                "title": meta["title"],
                "badge": meta["badge"],
                "rows": [row for row in rows if row["section"] == section_key],
            }
        )
    return sections


def _run_label(run: RunData) -> str:
    """The run's folder name if it is label-shaped, else its role."""
    label = public_label(run.folder)
    if label is None:
        diagnostics.warning("run folder name withheld from report for role %s", run.role)
    return label or run.role.lower()


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def build_run_context(run: RunData) -> dict[str, Any]:
    """Allowlisted per-run facts for the comparison report.

    Everything is taken through report_safety's field contracts: a field that
    is not on the list is never read, and a listed field whose value does not
    match its contract is left out. In particular no local filesystem path,
    no exception text and no free-text pricing note can appear here.
    """
    meta = _dict_or_empty(run.summary.get("meta"))
    elasticache = _dict_or_empty(_dict_or_empty(run.cluster_details).get("elasticache"))
    workload = public_cluster_details(run.cluster_details)["memtier"]
    config = public_run_config({
        "engine_type": meta.get("engine_type") or elasticache.get("engine"),
        "engine_version": meta.get("engine_version") or elasticache.get("engine_version_configured"),
        "node_type": meta.get("node_type") or elasticache.get("node_type"),
        "node_memory_bytes": meta.get("node_memory_bytes") or elasticache.get("node_memory_bytes"),
        "node_hourly_usd": meta.get("node_hourly_usd") or elasticache.get("node_hourly_usd"),
        "node_count": meta.get("node_count") or elasticache.get("num_cache_nodes"),
        "cluster_mode": meta.get("cluster_mode"),
    })
    label = _run_label(run)
    cluster_id = public_token(meta.get("cluster_id"))
    items: list[dict[str, str]] = []

    def add(item_label: str, value: Any) -> None:
        if value in (None, ""):
            return
        items.append({"label": item_label, "value": str(value)})

    add("Run folder", label)
    # The file name only: the full path would disclose the local directory layout.
    add("Source JSON", public_label(run.results_path.name))
    add("Cluster ID", cluster_id)
    add("Time range", public_time_range(meta.get("time_range")))
    add("Report start", public_iso_timestamp(meta.get("report_start")))
    add("Report end", public_iso_timestamp(meta.get("report_end")))
    engine = config.get("engine_type")
    add("Engine", engine)
    add("Engine version", config.get("engine_version"))
    add("Node type", config.get("node_type"))
    add("Node memory", _format_gib(config.get("node_memory_bytes")))
    add(
        f"{engine.title()} hourly" if engine else "Node hourly",
        _format_usd_hour(config.get("node_hourly_usd")),
    )
    add("Node count", config.get("node_count"))
    add("Cluster mode", normalize_cluster_mode(config.get("cluster_mode")))
    task_count = workload.get("task_count") or public_number(get_nested(run.summary, ("ecs", "task_count")), minimum=0)
    add("Task count", task_count)
    add("Clients", workload.get("clients"))
    add("Threads", workload.get("threads"))
    add("Pipeline", workload.get("pipeline"))
    add("Ratio", workload.get("ratio"))
    add("Data size", f"{workload['data_size_bytes']} bytes" if workload.get("data_size_bytes") else None)
    add("Key maximum", workload.get("key_maximum"))
    notes: list[str] = []
    if not run.cluster_details:
        notes.append("cluster_details.json is missing for this run, so config fields are partial.")
    notes.extend(public_run_warnings(run.warnings))
    note = " ".join(notes) if notes else None
    return {"role": run.role, "folder": label, "title": cluster_id or label, "items": items, "note": note}


def collect_takeaways(baseline: RunData, candidate: RunData) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def number(path: tuple[str, ...]) -> tuple[float | None, float | None]:
        spec = next(metric for metric in METRICS if metric.path == path)
        return metric_value(spec, get_nested(baseline.summary, path)), metric_value(spec, get_nested(candidate.summary, path))

    def metric_signal(a: float | None, b: float | None, *, higher_is_better: bool) -> int:
        """
        Return +1 if candidate is better than baseline for this metric, -1 if worse, 0 if neutral/unknown.
        """
        if a is None or b is None:
            return 0
        if b == a:
            return 0
        if higher_is_better:
            return 1 if b > a else -1
        else:
            return 1 if b < a else -1

    def shown(value: float | None, decimals: int) -> str:
        """A missing value is a fixed status in the text, never a made-up 0."""
        return format_number(value, decimals) if value is not None else "n/a"

    avg_ops_a, avg_ops_b = number(("benchmark", "avg_ops"))
    max_latency_a, max_latency_b = number(("benchmark", "max_latency_ms"))

    # Determine tone based on both throughput (higher is better) and latency
    # (lower is better). A metric missing on either side gives no signal.
    throughput_signal = metric_signal(avg_ops_a, avg_ops_b, higher_is_better=True)
    latency_signal = metric_signal(max_latency_a, max_latency_b, higher_is_better=False)
    throughput_better, throughput_worse = throughput_signal > 0, throughput_signal < 0
    latency_better, latency_worse = latency_signal > 0, latency_signal < 0

    if (throughput_better and not latency_worse) or (latency_better and not throughput_worse):
        tone = "better"
    elif (throughput_worse and not latency_better) or (latency_worse and not throughput_better):
        tone = "worse"
    elif None in (avg_ops_a, avg_ops_b, max_latency_a, max_latency_b):
        tone = "neutral"
    else:
        # Mixed tradeoff: one metric improved while the other worsened or both unchanged.
        tone = "mixed"

    items.append(
        {
            "tone": tone,
            "title": "Throughput vs latency tradeoff",
            "text": (
                f"Avg throughput moved {shown(avg_ops_a, 1)} -> {shown(avg_ops_b, 1)} ops/sec "
                f"({percent_change(avg_ops_a, avg_ops_b)}), while max latency moved "
                f"{shown(max_latency_a, 2)} -> {shown(max_latency_b, 2)} ms "
                f"({percent_change(max_latency_a, max_latency_b)})."
            ),
        }
    )

    max_mem_a, max_mem_b = number(("memory", "max_usage_pct"))
    headroom_a, headroom_b = number(("memory", "headroom_pct"))
    swap_a, swap_b = number(("memory", "swap_max_bytes"))

    # For max memory usage and swap, lower is better; for headroom, higher is better.
    signals = [
        metric_signal(max_mem_a, max_mem_b, higher_is_better=False),
        metric_signal(headroom_a, headroom_b, higher_is_better=True),
        metric_signal(swap_a, swap_b, higher_is_better=False),
    ]

    pos = any(s > 0 for s in signals)
    neg = any(s < 0 for s in signals)
    if pos and not neg:
        mem_tone = "better"
    elif neg and not pos:
        mem_tone = "worse"
    else:
        # Mixed or no clear change across metrics.
        mem_tone = "mixed"

    items.append(
        {
            "tone": mem_tone,
            "title": "Memory pressure",
            "text": (
                f"Max memory usage moved {shown(max_mem_a, 2)}% -> {shown(max_mem_b, 2)}% "
                f"({diff_points(max_mem_a, max_mem_b, 2)}), headroom moved "
                f"{shown(headroom_a, 2)}% -> {shown(headroom_b, 2)}% "
                f"({diff_points(headroom_a, headroom_b, 2)}), and peak swap moved "
                f"{shown(swap_a, 1)} -> {shown(swap_b, 1)} MB."
            ),
        }
    )

    hit_a, hit_b = number(("cache_efficiency", "avg_hit_rate_pct"))
    get_lat_a, get_lat_b = number(("latency_server_us", "get_avg"))
    set_lat_a, set_lat_b = number(("latency_server_us", "set_avg"))

    # Determine tone based on strict improvement/worsening only; equality is neutral.
    cache_latency_signals = [
        metric_signal(hit_a, hit_b, higher_is_better=True),
        metric_signal(get_lat_a, get_lat_b, higher_is_better=False),
        metric_signal(set_lat_a, set_lat_b, higher_is_better=False),
    ]
    cache_latency_signals = [signal for signal in cache_latency_signals if signal != 0]

    if cache_latency_signals and all(signal > 0 for signal in cache_latency_signals):
        cache_latency_tone = "better"
    elif cache_latency_signals and all(signal < 0 for signal in cache_latency_signals):
        cache_latency_tone = "worse"
    elif cache_latency_signals:
        cache_latency_tone = "mixed"
    else:
        cache_latency_tone = "neutral"

    items.append(
        {
            "tone": cache_latency_tone,
            "title": "Cache efficiency and server latency",
            "text": (
                f"Cache hit rate moved {shown(hit_a, 2)}% -> {shown(hit_b, 2)}% "
                f"({diff_points(hit_a, hit_b, 2)}). Server-side GET latency moved "
                f"{shown(get_lat_a, 3)} -> {shown(get_lat_b, 3)} us and SET latency moved "
                f"{shown(set_lat_a, 3)} -> {shown(set_lat_b, 3)} us."
            ),
        }
    )

    evictions_a, evictions_b = number(("cache_efficiency", "total_evictions"))
    first_evict_a = get_nested(baseline.summary, ("cache_efficiency", "first_eviction_ts"))
    first_evict_b = get_nested(candidate.summary, ("cache_efficiency", "first_eviction_ts"))
    if (evictions_a == 0 and first_evict_a) or (evictions_b == 0 and first_evict_b):
        items.append(
            {
                "tone": "warning",
                "title": "Eviction fields need validation",
                "text": (
                    "At least one run reports a first eviction timestamp even though total evictions are zero. "
                    "Those summary fields should be validated before using them for conclusions."
                ),
            }
        )

    if not baseline.cluster_details or not candidate.cluster_details:
        missing_roles = ", ".join(run.role.lower() for run in (baseline, candidate) if not run.cluster_details)
        items.append(
            {
                "tone": "warning",
                "title": "Config comparison is partial",
                "text": f"cluster_details.json is missing for {missing_roles}, so engine and memtier configuration comparison is incomplete.",
            }
        )

    constrained_runs = []
    for run in (baseline, candidate):
        loadgen = run.summary.get("loadgen", {})
        # "validation_status"/"invalid_reasons" is a pre-rename fallback:
        # older results_*.json summaries only have those fields (with real
        # "invalid" values), not "diagnostic_status"/"warning_reasons".
        # build_loadgen_summary() no longer writes the old names, but keep
        # reading them here so comparisons against those older runs still work.
        status = loadgen.get("diagnostic_status") or loadgen.get("validation_status")
        if status in {"warning", "invalid"}:
            reasons = (
                loadgen.get("warning_reasons")
                or loadgen.get("invalid_reasons", [])
            )
            if isinstance(reasons, str):
                reasons = [reasons]
            elif not isinstance(reasons, (list, tuple)):
                reasons = []
            # Every code build_loadgen_summary() emits has a label here.
            reason_labels = {
                "generator_cpu_p95_above_85_pct": "ECS task CPU p95 > 85%",
                "throughput_skew_within_az_above_1_3": "within-AZ throughput p90/p10 > 1.3",
                "task_count_mismatch_with_request": "observed ECS task count differs from the request",
            }
            # Unknown codes are not echoed: only the labels above are report text.
            reason_text = ", ".join(
                reason_labels.get(reason, "other ECS task constraint") for reason in reasons
                if isinstance(reason, str)
            ) or "ECS task constraint detected"
            constrained_runs.append(f"{run.role}: {reason_text}")
    if constrained_runs:
        items.append(
            {
                "tone": "warning",
                "title": "ECS task constraints detected",
                "text": (
                    "; ".join(constrained_runs)
                    + ". ECS task latency may not be representative; this does not invalidate the ElastiCache result."
                ),
            }
        )

    return items


def build_topline_cards(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    wanted = set(TOPLINE_PATHS)
    cards = []
    for row in rows:
        if row["path"] in wanted:
            cards.append(
                {
                    "label": row["label"],
                    "baseline": row["baseline"],
                    "candidate": row["candidate"],
                    "delta": row["delta"],
                    "tone": row["tone"],
                }
            )
    return cards


def infer_output_path(baseline: RunData, candidate: RunData, override: str | None) -> Path:
    if override:
        return Path(override)
    baseline_root = baseline.results_path.parent.parent
    output_dir = baseline_root / "comparisons" if baseline_root.name == "results" else Path.cwd()
    return output_dir / f"{baseline.folder}_vs_{candidate.folder}.html"


#: The roles run_compare_report() loads the two runs under.
BASELINE_ROLE, CANDIDATE_ROLE = "Baseline", "Candidate"
_CONTRACT_ROLES = frozenset({BASELINE_ROLE, CANDIDATE_ROLE})
_CONTRACT_FIELDS = frozenset(f"{section}.{field}" for section, field in CONTROL_VARIABLES)


def public_contract(contract: Any) -> dict[str, Any] | None:
    """The comparability contract as the report may show it.

    Reason codes, roles and field names come from closed sets; the two values
    in a "baseline vs candidate" reason are scalars or ``withheld`` -- they
    originate in cluster_details.json / summaries, which are not trusted text.
    """
    if not isinstance(contract, dict) or not contract:
        return None
    verdict = contract.get("verdict")
    reasons = []
    for reason in contract.get("reasons") or []:
        if not isinstance(reason, dict) or reason.get("code") not in CONTRACT_REASON_CODES:
            diagnostics.warning("comparability reason withheld from report: unknown reason code")
            continue
        clean: dict[str, Any] = {"code": reason["code"]}
        if reason.get("role") in _CONTRACT_ROLES:
            clean["role"] = reason["role"]
        if reason.get("field") in _CONTRACT_FIELDS:
            clean["field"] = reason["field"]
        fields = [field for field in (reason.get("fields") or []) if field in INTENDED_DIMENSIONS]
        if fields:
            clean["fields"] = fields
        if reason.get("baseline") is not None and reason.get("candidate") is not None:
            clean["baseline"] = public_scalar(reason["baseline"])
            clean["candidate"] = public_scalar(reason["candidate"])
        reasons.append(clean)
    if verdict not in VERDICTS:
        diagnostics.warning("comparability verdict withheld from report: unknown verdict")
        verdict = WITHHELD
    return {"verdict": verdict, "reasons": reasons}


def build_compare_payload(baseline: RunData, candidate: RunData) -> dict[str, Any]:
    rows = metric_rows(baseline, candidate)
    runs = [build_run_context(baseline), build_run_context(candidate)]
    baseline_name, candidate_name = runs[0]["folder"], runs[1]["folder"]
    return {
        "title": f"ElastiCache Comparison - {baseline_name} vs {candidate_name}",
        "heading": "ElastiCache Comparison Report",
        "subtitle": f"{baseline_name} vs {candidate_name}",
        "baseline_name": baseline_name,
        "candidate_name": candidate_name,
        "comparison_direction": "candidate minus baseline",
        "topline_cards": build_topline_cards(rows),
        "takeaways": collect_takeaways(baseline, candidate),
        "runs": runs,
        "sections": build_sections(rows),
        # WP4: is this pair comparable at all, separate from how the metrics
        # in `sections` above happen to differ (report_compare's own
        # tone-better/worse is about a *result*, never about whether the
        # comparison itself is trustworthy).
        "contract": public_contract(build_comparison_contract(baseline, candidate)),
    }


def run_compare_report(baseline_path: str, candidate_path: str, output: str | None) -> None:
    baseline = load_run(BASELINE_ROLE, baseline_path)
    candidate = load_run(CANDIDATE_ROLE, candidate_path)
    output_path = infer_output_path(baseline, candidate, output)
    with publication_guard("comparison report rendering"):
        document = assert_publishable_html(
            render_report(build_compare_payload(baseline, candidate)),
            context="comparison report",
        )
    # Staged and moved into place only now: a failure above never touches an
    # existing comparison file.
    write_files_atomically({output_path: document})
    print(f"Comparison report written to {output_path}")
