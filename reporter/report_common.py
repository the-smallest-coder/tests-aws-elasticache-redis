from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ECS_ENV_VARS = ("S3_BUCKET", "S3_PREFIX", "REPORT_TIMESTAMP", "CLUSTER_ID")
GENERATOR_SCHEMA_VERSION = "2026-08-loadgen-quality-v2"
Normalizer = Callable[[Any], float | None]


@dataclass(frozen=True)
class RunData:
    role: str
    results_path: Path
    folder: str
    summary: dict[str, Any]
    cluster_details: dict[str, Any] | None
    warnings: tuple[str, ...] = ()


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
    warning_above: float | None = None


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
        local_path = path / "results_local.json"
        if local_path.exists():
            return local_path

        generated = sorted(path.glob("results_*.json"))
        generated = [candidate for candidate in generated if candidate.name != "report_status.json"]
        if len(generated) == 1:
            return generated[0]
        if len(generated) > 1:
            names = ", ".join(candidate.name for candidate in generated)
            raise FileExistsError(f"Multiple result JSON files found in {path}: {names}")
        path = local_path
    return path


def _canonical_result_jsons(run_dir: Path) -> list[Path]:
    return sorted(
        p for p in run_dir.glob("results_*.json")
        if p.name not in {"results_local.json", "report_status.json"}
    )


def _s3_key_from_uri(uri: str) -> str:
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        return ""
    parts = uri[5:].split("/", 1)
    return parts[1] if len(parts) == 2 else ""


def _status_report_artifacts_exist(run_dir: Path, status: dict[str, Any]) -> bool:
    report_key = _s3_key_from_uri(status.get("report", ""))
    summary_key = _s3_key_from_uri(status.get("summary", ""))
    if not report_key or not summary_key:
        return False
    report_name = Path(report_key).name
    summary_name = Path(summary_key).name
    return (run_dir / report_name).is_file() and (run_dir / summary_name).is_file()


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _summary_has_legacy_relative_fields(summary: dict[str, Any]) -> list[str]:
    legacy_fields = []
    benchmark = summary.get("benchmark", {}) if isinstance(summary.get("benchmark"), dict) else {}
    cache_eff = summary.get("cache_efficiency", {}) if isinstance(summary.get("cache_efficiency"), dict) else {}
    if "active_window_min" in benchmark:
        legacy_fields.append("benchmark.active_window_min")
    if "prefill_min" in benchmark:
        legacy_fields.append("benchmark.prefill_min")
    if "first_eviction_offset_min" in cache_eff:
        legacy_fields.append("cache_efficiency.first_eviction_offset_min")
    return legacy_fields


def summary_is_current_schema(summary: dict[str, Any]) -> bool:
    meta = summary.get("meta", {}) if isinstance(summary.get("meta"), dict) else {}
    return meta.get("generator_schema_version") == GENERATOR_SCHEMA_VERSION


def inspect_run_directory(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    info: dict[str, Any] = {
        "run_dir": run_dir,
        "run_folder": run_dir.name,
        "files": {},
        "warnings": [],
        "uploaded_ready": False,
        "local_ready": False,
        "canonical_json_path": None,
        "results_local_path": run_dir / "results_local.json",
    }

    status_path = run_dir / "report_status.json"
    local_json_path = run_dir / "results_local.json"
    cluster_details_path = run_dir / "cluster_details.json"
    canonical_jsons = _canonical_result_jsons(run_dir)
    canonical_htmls = sorted(p for p in run_dir.glob("results_*.html") if p.name != "results_local.html")
    logs_dir = run_dir / "logs"

    memtier_txt = sorted(logs_dir.glob("loadgen/**/*.txt")) if logs_dir.is_dir() else []
    memtier_minute = sorted(logs_dir.glob("loadgen/**/*_memtier.minute.csv")) if logs_dir.is_dir() else []
    memtier_totals = sorted(logs_dir.glob("loadgen/**/*.totals.json")) if logs_dir.is_dir() else []

    info["files"] = {
        "report_status_json": status_path.is_file(),
        "results_local_json": local_json_path.is_file(),
        "canonical_json": bool(canonical_jsons),
        "canonical_html": bool(canonical_htmls),
        "cluster_details_json": cluster_details_path.is_file(),
        "memtier_logs": bool(memtier_txt),
        "memtier_minute_artifact": bool(memtier_minute),
        "memtier_totals_artifacts": bool(memtier_totals),
    }

    status = None
    if status_path.is_file():
        try:
            status = load_json(status_path)
        except Exception as exc:
            info["warnings"].append(f"report_status.json unreadable: {exc}")
    else:
        info["warnings"].append("Missing report_status.json.")

    if not info["files"]["memtier_logs"]:
        info["warnings"].append("Missing memtier logs under logs/loadgen.")
    if not info["files"]["memtier_minute_artifact"] or not info["files"]["memtier_totals_artifacts"]:
        info["warnings"].append("Missing memtier ETL sidecars (_memtier.minute.csv and/or *.totals.json).")

    local_summary = None
    local_meta = {}
    if local_json_path.is_file():
        try:
            local_summary = load_json(local_json_path)
            local_meta = local_summary.get("meta", {}) if isinstance(local_summary.get("meta"), dict) else {}
        except Exception as exc:
            info["warnings"].append(f"results_local.json unreadable: {exc}")

    legacy_fields = _summary_has_legacy_relative_fields(local_summary or {})
    if legacy_fields:
        joined = ", ".join(legacy_fields)
        info["warnings"].append(f"Legacy relative fields detected: {joined}.")

    report_start = _parse_iso_timestamp(local_meta.get("report_start"))
    report_end = _parse_iso_timestamp(local_meta.get("report_end"))
    strict_window_present = report_start is not None and report_end is not None and report_start <= report_end
    marker_ok = (local_meta.get("generator_schema_version") == GENERATOR_SCHEMA_VERSION)
    source_mode = local_meta.get("source_mode")
    source_mode_ok = source_mode in {"local", "uploaded"}
    memtier_window_source_ok = local_meta.get("memtier_window_source") == "memtier_log_messages"
    artifact_source_ok = local_meta.get("artifact_source") in {"generated", "uploaded"}
    has_required_inputs = info["files"]["memtier_logs"] or (
        info["files"]["memtier_minute_artifact"] and info["files"]["memtier_totals_artifacts"]
    )

    info["local_ready"] = bool(
        local_summary
        and marker_ok
        and source_mode_ok
        and memtier_window_source_ok
        and artifact_source_ok
        and strict_window_present
        and has_required_inputs
    )
    if local_summary and not info["local_ready"]:
        info["warnings"].append("results_local.json is legacy/incomplete for current schema/readiness checks.")

    # A canonical results_*.json is only ever written by the S3-upload/ECS
    # pipeline (see run_uploaded_report in report_generator.py); the local
    # `generate` command deliberately never writes one, so it doesn't
    # clobber a canonical report later downloaded for the same run (see its
    # comment there). A purely local run is therefore permanently without
    # one by design -- only warn when there's also no ready local report to
    # fall back on, i.e. when this run has nothing usable at all.
    if not canonical_jsons and not info["local_ready"]:
        info["warnings"].append("Missing canonical results_*.json.")

    if status is not None:
        status_complete = bool(status.get("complete") is True)
        status_has_outputs = bool(
            isinstance(status.get("report"), str)
            and status.get("report")
            and isinstance(status.get("summary"), str)
            and status.get("summary")
        )
        outputs_exist = _status_report_artifacts_exist(run_dir, status) if status_has_outputs else False
        info["uploaded_ready"] = status_complete and status_has_outputs and outputs_exist
        if status_complete and not status_has_outputs:
            info["warnings"].append("report_status.json has complete=true but missing report/summary fields.")
        if status_has_outputs and not outputs_exist:
            info["warnings"].append("report_status.json references canonical outputs that are not present locally.")

    if canonical_jsons:
        preferred_name = f"results_{run_dir.name}.json"
        preferred = next((p for p in canonical_jsons if p.name == preferred_name), canonical_jsons[0])
        info["canonical_json_path"] = preferred

    return info


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def enrich_summary_meta(summary: dict[str, Any], cluster_details: dict[str, Any] | None) -> None:
    meta = summary.setdefault("meta", {})
    ecs = summary.setdefault("ecs", {})
    if "service_cpu_time_avg_pct" not in ecs and "avg_cpu_pct" in ecs:
        ecs["service_cpu_time_avg_pct"] = ecs.pop("avg_cpu_pct")
    if "service_cpu_time_peak_pct" not in ecs and "max_cpu_pct" in ecs:
        ecs["service_cpu_time_peak_pct"] = ecs.pop("max_cpu_pct")
    expected_task_count = get_nested(summary, ("loadgen", "expected_task_count"))
    if not ecs.get("task_count") and expected_task_count is not None:
        ecs["task_count"] = expected_task_count
    if not cluster_details:
        return

    elasticache = cluster_details.get("elasticache", {})
    run_info = cluster_details.get("run", {})
    memtier = cluster_details.get("memtier", {})

    meta["cluster_id"] = meta.get("cluster_id") or run_info.get("cluster_id") or ""
    meta["engine_type"] = meta.get("engine_type") or elasticache.get("engine") or ""
    meta["engine_version"] = meta.get("engine_version") or elasticache.get("engine_version_configured") or ""
    meta["node_type"] = meta.get("node_type") or elasticache.get("node_type") or ""
    meta["node_memory_bytes"] = meta.get("node_memory_bytes") or elasticache.get("node_memory_bytes") or ""
    meta["node_hourly_usd"] = meta.get("node_hourly_usd") or elasticache.get("node_hourly_usd") or ""
    meta["node_hourly_usd_source"] = meta.get("node_hourly_usd_source") or elasticache.get("node_hourly_usd_source") or ""
    meta["node_count"] = meta.get("node_count") or elasticache.get("num_cache_nodes") or ""
    if not meta.get("cluster_mode"):
        meta["cluster_mode"] = elasticache.get("cluster_mode_enabled")
    memtier_task_count = memtier.get("task_count")
    if not ecs.get("task_count") and memtier_task_count:
        ecs["task_count"] = memtier_task_count


def load_run(role: str, raw_path: str) -> RunData:
    source_path = Path(raw_path)
    warnings: list[str] = []

    if source_path.is_dir():
        inspection = inspect_run_directory(source_path)
        warnings.extend(inspection.get("warnings", []))
        canonical_path = inspection.get("canonical_json_path")
        local_path = inspection.get("results_local_path")

        if canonical_path is not None and Path(canonical_path).is_file():
            results_path = Path(canonical_path)
            canonical_summary = load_json(results_path)
            if not summary_is_current_schema(canonical_summary):
                warnings.append("Canonical JSON exists but is not current schema; treating run as legacy/incomplete.")
            summary = canonical_summary
        elif Path(local_path).is_file() and inspection.get("local_ready", False):
            results_path = Path(local_path)
            summary = load_json(results_path)
        elif Path(local_path).is_file():
            results_path = Path(local_path)
            summary = load_json(results_path)
            warnings.append("Using results_local.json fallback because canonical current-schema JSON is unavailable.")
        else:
            raise FileNotFoundError(f"No result JSON file found in {source_path}")
    else:
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
        warnings=tuple(dict.fromkeys(warnings)),
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
