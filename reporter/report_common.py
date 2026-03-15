from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ECS_ENV_VARS = ("S3_BUCKET", "S3_PREFIX", "REPORT_TIMESTAMP", "CLUSTER_ID")
Normalizer = Callable[[Any], float | None]


@dataclass(frozen=True)
class RunData:
    role: str
    results_path: Path
    folder: str
    summary: dict[str, Any]
    cluster_details: dict[str, Any] | None


@dataclass(frozen=True)
class MetricSpec:
    section: str
    label: str
    path: tuple[str, ...]
    unit: str = ""
    decimals: int = 1
    direction: str = "neutral"
    description: str = ""
    delta_mode: str = "absolute"
    none_label: str = "n/a"
    normalizer: Normalizer | None = None


def get_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"Error: Environment variable {name} not set.")
        sys.exit(1)
    return value


def to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    return None


def bytes_to_mb(value: Any) -> float | None:
    number = to_number(value)
    if number is None:
        return None
    return number / (1024 * 1024)


def resolve_results_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_dir():
        path = path / "results_local.json"
    return path


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def enrich_summary_meta(summary: dict[str, Any], cluster_details: dict[str, Any] | None) -> None:
    meta = summary.setdefault("meta", {})
    if not cluster_details:
        return

    elasticache = cluster_details.get("elasticache", {})
    run_info = cluster_details.get("run", {})
    memtier = cluster_details.get("memtier", {})

    meta["cluster_id"] = meta.get("cluster_id") or run_info.get("cluster_id") or ""
    meta["engine_type"] = meta.get("engine_type") or elasticache.get("engine") or ""
    meta["engine_version"] = meta.get("engine_version") or elasticache.get("engine_version_configured") or ""
    meta["node_type"] = meta.get("node_type") or elasticache.get("node_type") or ""
    meta["node_count"] = meta.get("node_count") or elasticache.get("num_cache_nodes") or ""
    if not meta.get("cluster_mode"):
        meta["cluster_mode"] = elasticache.get("cluster_mode_enabled")
    if "task_count" not in summary.get("ecs", {}):
        summary.setdefault("ecs", {})["task_count"] = memtier.get("task_count")


def load_run(role: str, raw_path: str) -> RunData:
    results_path = resolve_results_path(raw_path)
    if not results_path.exists():
        raise FileNotFoundError(f"Results file not found: {results_path}")

    summary = load_json(results_path)
    cluster_details_path = results_path.with_name("cluster_details.json")
    cluster_details = load_json(cluster_details_path) if cluster_details_path.exists() else None
    enrich_summary_meta(summary, cluster_details)
    return RunData(
        role=role,
        results_path=results_path,
        folder=results_path.parent.name,
        summary=summary,
        cluster_details=cluster_details,
    )


def get_nested(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def format_number(value: float, decimals: int) -> str:
    text = f"{value:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def metric_value(spec: MetricSpec, raw_value: Any) -> float | None:
    if spec.normalizer:
        return spec.normalizer(raw_value)
    return to_number(raw_value)


def display_value(spec: MetricSpec, raw_value: Any) -> str:
    if raw_value in (None, ""):
        return spec.none_label
    numeric = metric_value(spec, raw_value)
    if numeric is None:
        return str(raw_value)
    suffix = f" {spec.unit}" if spec.unit else ""
    return f"{format_number(numeric, spec.decimals)}{suffix}"


def parse_duration_minutes(time_range: str) -> str | None:
    match = re.search(r"\(([-0-9.]+)\s+min\)", time_range or "")
    return f"{match.group(1)} min" if match else None


def normalize_cluster_mode(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return "Cluster" if value else "Non-cluster"
    lowered = str(value).strip().lower()
    if lowered == "true":
        return "Cluster"
    if lowered == "false":
        return "Non-cluster"
    return str(value)


def percent_change(a: float | None, b: float | None) -> str:
    if a is None or b is None or a == 0:
        return "n/a"
    return f"{((b - a) / a) * 100:+.1f}%"


def diff_points(a: float | None, b: float | None, decimals: int = 1) -> str:
    if a is None or b is None:
        return "n/a"
    return f"{(b - a):+.{decimals}f} pp"
