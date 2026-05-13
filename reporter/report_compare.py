from __future__ import annotations

import math
from pathlib import Path
from typing import Any

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
    parse_duration_minutes,
    percent_change,
)
from template import render_report


SECTION_META: dict[str, dict[str, str]] = {
    "benchmark": {"title": "Benchmark Summary", "badge": "memtier"},
    "engine_memory": {"title": "Engine and Memory", "badge": "infra"},
    "cache_latency": {"title": "Cache, Latency, Connections", "badge": "behavior"},
    "network_ecs": {"title": "Network and ECS", "badge": "loadgen"},
}


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("benchmark", "Avg Throughput", ("benchmark", "avg_ops"), "ops/sec", 1, "higher", "Average observed memtier throughput during the active benchmark window."),
    MetricSpec("benchmark", "Peak Throughput", ("benchmark", "peak_ops"), "ops/sec", 1, "higher", "Highest single observed throughput value."),
    MetricSpec("benchmark", "Throughput CV", ("benchmark", "cv_pct"), "%", 2, "lower", "Coefficient of variation. Lower values mean steadier throughput.", "points"),
    MetricSpec("benchmark", "Avg Latency", ("benchmark", "avg_latency_ms"), "ms", 3, "lower", "Average client-side latency reported by memtier."),
    MetricSpec("benchmark", "Max Latency", ("benchmark", "max_latency_ms"), "ms", 2, "lower", "Worst client-side latency observed during the run."),
    MetricSpec("benchmark", "P95 Latency", ("benchmark", "p95_latency_ms"), "ms", 2, "lower", "95th percentile client-side latency."),
    MetricSpec("benchmark", "P99 Latency", ("benchmark", "p99_latency_ms"), "ms", 2, "lower", "99th percentile client-side latency."),
    MetricSpec("benchmark", "Avg Bandwidth", ("benchmark", "avg_bandwidth_kbs"), "KB/s", 2, "neutral", "Average network throughput reported by memtier."),
    MetricSpec("benchmark", "Active Load Window", ("benchmark", "active_window_min"), "min", 1, "neutral", "Measured benchmark window with non-zero traffic."),
    MetricSpec("benchmark", "Pre-fill Duration", ("benchmark", "prefill_min"), "min", 1, "lower", "Warm-up or key pre-population time before steady benchmark traffic."),
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
    MetricSpec("cache_latency", "Cache Hit Rate", ("cache_efficiency", "avg_hit_rate_pct"), "%", 2, "higher", "Average CacheHitRate during the benchmark window.", "points"),
    MetricSpec("cache_latency", "Total Evictions", ("cache_efficiency", "total_evictions"), "", 0, "lower", "Total key evictions reported by CloudWatch."),
    MetricSpec("cache_latency", "Min Freeable Memory", ("cache_efficiency", "min_freeable_memory_mb"), "MB", 1, "higher", "Lowest freeable memory value seen during the run."),
    MetricSpec("cache_latency", "Peak Key Count", ("cache_efficiency", "peak_key_count"), "", 0, "neutral", "Maximum total key count recorded on the cache node."),
    MetricSpec("cache_latency", "First Eviction Offset", ("cache_efficiency", "first_eviction_offset_min"), "min", 1, "higher", "Minutes from benchmark start to the first eviction event.", none_label="None"),
    MetricSpec("cache_latency", "GET Latency", ("latency_server_us", "get_avg"), "us", 3, "lower", "Average server-side GET command latency."),
    MetricSpec("cache_latency", "SET Latency", ("latency_server_us", "set_avg"), "us", 3, "lower", "Average server-side SET command latency."),
    MetricSpec("cache_latency", "String Latency", ("latency_server_us", "string_avg"), "us", 3, "lower", "Average server-side string command latency."),
    MetricSpec("cache_latency", "Avg Connections", ("connections", "avg"), "", 1, "neutral", "Average concurrent connections on the cache node."),
    MetricSpec("cache_latency", "Peak Connections", ("connections", "max"), "", 1, "neutral", "Highest concurrent connection count."),
    MetricSpec("network_ecs", "Avg Cache In", ("network", "cache", "avg_in_kbs"), "KB/s", 2, "neutral", "Average inbound network throughput on the cache node."),
    MetricSpec("network_ecs", "Avg Cache Out", ("network", "cache", "avg_out_kbs"), "KB/s", 2, "neutral", "Average outbound network throughput on the cache node."),
    MetricSpec("network_ecs", "BW In Throttle Events", ("network", "throttling", "bw_in_exceeded_total"), "", 0, "lower", "Total bandwidth-in throttle events."),
    MetricSpec("network_ecs", "BW Out Throttle Events", ("network", "throttling", "bw_out_exceeded_total"), "", 0, "lower", "Total bandwidth-out throttle events."),
    MetricSpec("network_ecs", "PPS Throttle Events", ("network", "throttling", "pps_exceeded_total"), "", 0, "lower", "Total packets-per-second throttle events."),
    MetricSpec("network_ecs", "Avg ECS CPU", ("ecs", "avg_cpu_pct"), "%", 2, "neutral", "Average CPU utilization across load generator tasks.", "points"),
    MetricSpec("network_ecs", "Peak ECS CPU", ("ecs", "max_cpu_pct"), "%", 2, "lower", "Peak CPU utilization across load generator tasks.", "points"),
    MetricSpec("network_ecs", "Peak ECS Memory", ("ecs", "peak_mem_mb"), "MB", 1, "lower", "Peak load generator memory usage."),
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
    if spec.path == ("benchmark", "prefill_min") and (baseline < 0 or candidate < 0):
        return "warning"
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
    for spec in METRICS:
        baseline_raw = get_nested(baseline.summary, spec.path)
        candidate_raw = get_nested(candidate.summary, spec.path)
        baseline_value = metric_value(spec, baseline_raw)
        candidate_value = metric_value(spec, candidate_raw)
        rows.append(
            {
                "section": spec.section,
                "label": spec.label,
                "baseline": display_value(spec, baseline_raw),
                "candidate": display_value(spec, candidate_raw),
                "delta": format_delta(spec, baseline_value, candidate_value),
                "tone": classify_delta(spec, baseline_value, candidate_value),
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


def build_run_context(run: RunData) -> dict[str, Any]:
    meta = run.summary.get("meta", {})
    elasticache = (run.cluster_details or {}).get("elasticache", {})
    memtier = (run.cluster_details or {}).get("memtier", {})
    items: list[dict[str, str]] = []

    def add(label: str, value: Any) -> None:
        if value in (None, ""):
            return
        items.append({"label": label, "value": str(value)})

    add("Run folder", run.folder)
    add("Source JSON", str(run.results_path))
    add("Cluster ID", meta.get("cluster_id"))
    add("Time range", meta.get("time_range"))
    add("Duration", parse_duration_minutes(meta.get("time_range", "")))
    add("Engine", meta.get("engine_type") or elasticache.get("engine"))
    add("Engine version", meta.get("engine_version") or elasticache.get("engine_version_configured"))
    add("Node type", meta.get("node_type") or elasticache.get("node_type"))
    add("Node count", meta.get("node_count") or elasticache.get("num_cache_nodes"))
    add("Cluster mode", normalize_cluster_mode(meta.get("cluster_mode")))
    add("Task count", memtier.get("task_count") or get_nested(run.summary, ("ecs", "task_count")))
    add("Clients", memtier.get("clients"))
    add("Threads", memtier.get("threads"))
    add("Pipeline", memtier.get("pipeline"))
    add("Ratio", memtier.get("ratio"))
    add("Data size", f"{memtier.get('data_size_bytes')} bytes" if memtier.get("data_size_bytes") else None)
    add("Key maximum", memtier.get("key_maximum"))
    note = None
    if not run.cluster_details:
        note = "cluster_details.json is missing for this run, so config fields are partial."
    return {"role": run.role, "folder": run.folder, "title": meta.get("cluster_id") or run.folder, "items": items, "note": note}


def collect_takeaways(baseline: RunData, candidate: RunData) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def number(path: tuple[str, ...]) -> tuple[float | None, float | None]:
        spec = next(metric for metric in METRICS if metric.path == path)
        return metric_value(spec, get_nested(baseline.summary, path)), metric_value(spec, get_nested(candidate.summary, path))

    avg_ops_a, avg_ops_b = number(("benchmark", "avg_ops"))
    max_latency_a, max_latency_b = number(("benchmark", "max_latency_ms"))

    # Determine tone based on both throughput (higher is better) and latency (lower is better).
    avg_ops_delta = (avg_ops_b or 0) - (avg_ops_a or 0)
    latency_delta = (max_latency_b or 0) - (max_latency_a or 0)

    throughput_better = avg_ops_delta > 0
    throughput_worse = avg_ops_delta < 0
    latency_better = latency_delta < 0
    latency_worse = latency_delta > 0

    if (throughput_better and not latency_worse) or (latency_better and not throughput_worse):
        tone = "better"
    elif (throughput_worse and not latency_better) or (latency_worse and not throughput_better):
        tone = "worse"
    else:
        # Mixed tradeoff: one metric improved while the other worsened or both unchanged.
        tone = "mixed"

    items.append(
        {
            "tone": tone,
            "title": "Throughput vs latency tradeoff",
            "text": (
                f"Avg throughput moved {format_number(avg_ops_a or 0, 1)} -> {format_number(avg_ops_b or 0, 1)} ops/sec "
                f"({percent_change(avg_ops_a, avg_ops_b)}), while max latency moved "
                f"{format_number(max_latency_a or 0, 2)} -> {format_number(max_latency_b or 0, 2)} ms "
                f"({percent_change(max_latency_a, max_latency_b)})."
            ),
        }
    )

    max_mem_a, max_mem_b = number(("memory", "max_usage_pct"))
    headroom_a, headroom_b = number(("memory", "headroom_pct"))
    swap_a, swap_b = number(("memory", "swap_max_bytes"))

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
                f"Max memory usage moved {format_number(max_mem_a or 0, 2)}% -> {format_number(max_mem_b or 0, 2)}% "
                f"({diff_points(max_mem_a, max_mem_b, 2)}), headroom moved "
                f"{format_number(headroom_a or 0, 2)}% -> {format_number(headroom_b or 0, 2)}% "
                f"({diff_points(headroom_a, headroom_b, 2)}), and peak swap moved "
                f"{format_number(swap_a or 0, 1)} -> {format_number(swap_b or 0, 1)} MB."
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
                f"Cache hit rate moved {format_number(hit_a or 0, 2)}% -> {format_number(hit_b or 0, 2)}% "
                f"({diff_points(hit_a, hit_b, 2)}). Server-side GET latency moved "
                f"{format_number(get_lat_a or 0, 3)} -> {format_number(get_lat_b or 0, 3)} us and SET latency moved "
                f"{format_number(set_lat_a or 0, 3)} -> {format_number(set_lat_b or 0, 3)} us."
            ),
        }
    )

    prefill_a, prefill_b = number(("benchmark", "prefill_min"))
    if (prefill_a is not None and prefill_a < 0) or (prefill_b is not None and prefill_b < 0):
        items.append(
            {
                "tone": "warning",
                "title": "Prefill timing anomaly",
                "text": (
                    f"Prefill duration is {format_number(prefill_a or 0, 1)} min for the baseline and "
                    f"{format_number(prefill_b or 0, 1)} min for the candidate. Negative values are invalid and "
                    "likely indicate a bug in the prefill or active-window calculation."
                ),
            }
        )

    first_evict_a, first_evict_b = number(("cache_efficiency", "first_eviction_offset_min"))
    evictions_a, evictions_b = number(("cache_efficiency", "total_evictions"))
    if (evictions_a == 0 and first_evict_a is not None) or (evictions_b == 0 and first_evict_b is not None):
        items.append(
            {
                "tone": "warning",
                "title": "Eviction fields need validation",
                "text": (
                    "At least one run reports a first eviction offset even though total evictions are zero. "
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


def build_compare_payload(baseline: RunData, candidate: RunData) -> dict[str, Any]:
    rows = metric_rows(baseline, candidate)
    return {
        "title": f"ElastiCache Comparison - {baseline.folder} vs {candidate.folder}",
        "heading": "ElastiCache Comparison Report",
        "subtitle": f"{baseline.folder} vs {candidate.folder}",
        "baseline_name": baseline.folder,
        "candidate_name": candidate.folder,
        "comparison_direction": "candidate minus baseline",
        "topline_cards": build_topline_cards(rows),
        "takeaways": collect_takeaways(baseline, candidate),
        "runs": [build_run_context(baseline), build_run_context(candidate)],
        "sections": build_sections(rows),
    }


def run_compare_report(baseline_path: str, candidate_path: str, output: str | None) -> None:
    baseline = load_run("Baseline", baseline_path)
    candidate = load_run("Candidate", candidate_path)
    payload = build_compare_payload(baseline, candidate)
    output_path = infer_output_path(baseline, candidate, output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(payload), encoding="utf-8")
    print(f"Comparison report written to {output_path}")
