"""Publication boundary for every HTML report this package writes.

The contract (see AGENTS.md, "Report publication boundary"):

* A report renderer is only ever handed values that passed an explicit,
  per-field allowlist in this module. Unknown fields, free-text diagnostics
  (AWS CLI stderr, exception messages, stack traces, service error bodies)
  are never copied into HTML -- not as text, not as a tooltip or other
  attribute, not as a comment, not inside embedded chart data.
* Diagnostics go to the ``reporter.diagnostics`` logger (stderr locally, the
  container's log stream on ECS), never into a published artifact.
* If something prevents a valid report from being produced, generation fails
  with :class:`ReportGenerationError` *before* anything is written or
  uploaded, and a previously written valid file is left untouched.
* The finished document is scanned one last time for diagnostic markers
  (:func:`assert_publishable_html`). That scan is a safety net behind the
  allowlists, not a substitute for them.

Deliberately pandas-free at import time (cards.py has to stay importable
without pandas); the one DataFrame helper imports nothing itself.
"""

from __future__ import annotations

import contextlib
import html
import logging
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator, Mapping

#: Private diagnostics channel. Python's last-resort handler already writes
#: WARNING and above to stderr when nothing configures logging.
diagnostics = logging.getLogger("reporter.diagnostics")

#: Shown wherever a value exists but did not conform to its contract.
WITHHELD = "withheld"


class ReportGenerationError(Exception):
    """No valid, publishable report can be produced.

    ``str(error)`` is always fixed, public-safe text. The underlying cause (if
    any) is chained via ``__cause__`` and logged on ``diagnostics`` only.
    An invalid report window is a separate, plain ValueError raised before
    rendering starts.
    """


def log_failure(stage: str, exc: BaseException) -> None:
    """Record a failure's full detail on the private channel."""
    diagnostics.error("%s failed: %s: %s", stage, type(exc).__name__, exc, exc_info=exc)


@contextlib.contextmanager
def publication_guard(stage: str) -> Iterator[None]:
    """Turn any failure while building a report into :class:`ReportGenerationError`.

    A ReportGenerationError passes through unchanged; anything else is logged
    privately with :func:`log_failure` and replaced by a fixed public message.
    """
    try:
        yield
    except ReportGenerationError:
        raise
    except Exception as exc:
        log_failure(stage, exc)
        raise ReportGenerationError(
            "report rendering failed; details are in the private diagnostic log"
        ) from exc


# --------------------------------------------------------------------------- #
#  Value contracts                                                             #
# --------------------------------------------------------------------------- #

_A = re.ASCII

#: Run folders, cluster/replication-group ids, task ids, stream names.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", _A)
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ +()-]{0,127}", _A)
#: "7.1", "9.0.0", and the "6.x" form var.engine_version also accepts.
_ENGINE_VERSION_RE = re.compile(r"\d{1,3}(?:\.(?:\d{1,3}|x)){0,3}", _A)
_NODE_TYPE_RE = re.compile(r"cache\.[a-z0-9]{1,12}\.[a-z0-9]{1,12}", _A)
_REGION_RE = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}", _A)
_AZ_RE = re.compile(r"[a-z]{2}(?:-[a-z0-9]+){1,4}", _A)
_RATIO_RE = re.compile(r"\d{1,4}:\d{1,4}", _A)
_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?", _A)
_SNAKE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}", _A)
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?", _A
)
_TS_UTC = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})? UTC"
_TIME_RANGE_RE = re.compile(rf"{_TS_UTC} - {_TS_UTC}", _A)
#: A scalar shown as "baseline vs candidate" in the comparability contract.
_SCALAR_TOKEN_RE = re.compile(
    r"[A-Za-z0-9._+-]{1,64}|\d{1,4}:\d{1,4}|[A-Za-z]:[A-Za-z]", _A
)

_PRICE_SOURCES = frozenset({"aws_pricing_api", "disabled", "unavailable"})

# CloudWatch-shaped metric rows. These are the character sets CloudWatch
# itself permits for the respective fields, minus anything that could carry a
# sentence: no spaces, no ARNs, no URLs.
_METRIC_NAMESPACE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{0,127}", _A)
_METRIC_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", _A)
_METRIC_STAT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,31}", _A)
_METRIC_UNIT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_/%]{0,31}", _A)
#: Values may contain "/" (real ECS stream dimensions look like
#: Stream=memtier/memtier/<task-id>); ":" stays out, so an ARN cannot pass.
_DIM_PAIR = r"[A-Za-z][A-Za-z0-9_]{0,63}=[A-Za-z0-9_./-]{0,256}"
_METRIC_DIMENSIONS_RE = re.compile(rf"(?:{_DIM_PAIR}(?:;{_DIM_PAIR})*)?", _A)


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else None
    if isinstance(value, (int, str)):
        return str(value).strip()
    return None


def public_token(value: Any, pattern: re.Pattern[str] = _IDENTIFIER_RE) -> str | None:
    """``value`` as text if it fully matches ``pattern``, else None."""
    text = _text(value)
    return text if text and pattern.fullmatch(text) else None


def public_label(value: Any) -> str | None:
    """A short human label such as a run folder name."""
    return public_token(value, _LABEL_RE)


def public_number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> int | float | None:
    """A finite number (or numeric string) within bounds, else None. Never a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number: int | float = value
    elif isinstance(value, str) and len(value) <= 64 and _NUMBER_RE.fullmatch(value.strip()):
        # The length cap keeps int() below Python's digit limit (which would
        # raise instead of letting the value be withheld).
        text = value.strip()
        number = float(text) if "." in text else int(text)
    else:
        return None
    if isinstance(number, float):
        if not math.isfinite(number):
            return None
        if number.is_integer():
            number = int(number)
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def public_scalar(value: Any) -> int | float | bool | str:
    """A control-variable style value, or :data:`WITHHELD` if it is free text.

    Text stays text: "7.10" and "7.1" are different versions, and turning
    either into a number would print a real difference as "7.1 vs 7.1".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = public_token(value, _SCALAR_TOKEN_RE)
        return token if token is not None else WITHHELD
    number = public_number(value)
    return number if number is not None else WITHHELD


def require_identifier(value: Any, what: str) -> str:
    """A required identifier; fatal (with a fixed message) if absent or malformed."""
    token = public_token(value)
    if token is None:
        diagnostics.error("%s is missing or not a valid identifier: %r", what, str(value)[:80])
        raise ReportGenerationError(f"{what} is missing or is not a valid identifier")
    return token


def public_run_config(config: Mapping[str, Any] | None) -> dict[str, str]:
    """Allowlisted run configuration for rendering.

    Only these keys can ever reach a report header, card or chart. Anything
    else in ``config`` -- including ``node_hourly_usd_reason`` -- is dropped,
    and a value that does not match its contract is treated as absent.
    """
    source = config if isinstance(config, Mapping) else {}
    out: dict[str, str] = {}

    def keep(key: str, value: str | None) -> None:
        if value is not None:
            out[key] = value
        elif source.get(key) not in (None, ""):
            diagnostics.warning("config field %s withheld from report: value does not match its contract", key)

    engine = _text(source.get("engine_type"))
    keep("engine_type", engine.lower() if engine and engine.lower() in {"redis", "valkey"} else None)
    keep("engine_version", public_token(source.get("engine_version"), _ENGINE_VERSION_RE))
    keep("node_type", public_token(source.get("node_type"), _NODE_TYPE_RE))

    count = public_number(source.get("node_count"), minimum=1, maximum=1000)
    keep("node_count", str(int(count)) if isinstance(count, (int, float)) and float(count).is_integer() else None)

    mode = source.get("cluster_mode")
    mode_text = _text(mode) if not isinstance(mode, bool) else str(mode)
    keep("cluster_mode", mode_text.lower() if mode_text and mode_text.lower() in {"true", "false"} else None)

    # Bounded so a nonsense value cannot render as "inf GiB" (1 PiB is far
    # above any ElastiCache node).
    memory = public_number(source.get("node_memory_bytes"), minimum=1, maximum=2**50)
    keep("node_memory_bytes", _text(source.get("node_memory_bytes")) if memory is not None else None)
    price = public_number(source.get("node_hourly_usd"), minimum=0.000001, maximum=100000)
    keep("node_hourly_usd", _text(source.get("node_hourly_usd")) if price is not None else None)

    # Why a price is missing is deliberately NOT carried: the lookup's own
    # stderr is the very text that once leaked an STS ARN. Only the outcome
    # class survives; the renderer maps it to a fixed hint.
    price_source = public_price_source(source.get("node_hourly_usd_source"))
    if price_source:
        out["node_hourly_usd_source"] = price_source
    return out


_GIT_SHA_RE = re.compile(r"[0-9a-f]{7,40}|unknown", _A)
#: registry/repository[:tag][@sha256:digest]
_IMAGE_REF_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]{0,200}(?::[A-Za-z0-9._-]{1,128})?(?:@sha256:[0-9a-f]{64})?", _A
)
_SUMMARY_META_TEXT_FIELDS: dict[str, re.Pattern[str]] = {
    "cluster_id": _IDENTIFIER_RE,
    "engine_version": _ENGINE_VERSION_RE,
    "engine_version_actual": _ENGINE_VERSION_RE,
    "node_type": _NODE_TYPE_RE,
    "git_sha": _GIT_SHA_RE,
    "loadgen_image": _IMAGE_REF_RE,
}


def public_summary_meta(meta: Any) -> None:
    """Hold the summary JSON's free-text meta fields to their shapes, in place.

    These fields come from config / cluster_details.json and are published
    verbatim in results_*.json. A value that does not fit becomes
    :data:`WITHHELD`; an empty value stays as it is.
    """
    if not isinstance(meta, dict):
        return
    engine = meta.get("engine_type")
    if engine not in (None, ""):
        text = _text(engine)
        meta["engine_type"] = text.lower() if text and text.lower() in {"redis", "valkey"} else WITHHELD
    for field, pattern in _SUMMARY_META_TEXT_FIELDS.items():
        value = meta.get(field)
        if value in (None, ""):
            continue
        token = public_token(value, pattern)
        if token is None:
            diagnostics.warning("summary field %s withheld: value does not match its contract", field)
        meta[field] = token if token is not None else WITHHELD


def public_price_source(value: Any) -> str:
    """The price lookup outcome as one of :data:`_PRICE_SOURCES`, or ""."""
    text = _text(value)
    if not text:
        return ""
    return text if text in _PRICE_SOURCES else "unavailable"


def public_price(value: Any) -> float | None:
    """An hourly node price that may enter a calculation: finite and positive."""
    number = public_number(value, minimum=0.000001, maximum=100000)
    return float(number) if number is not None else None


_WORKLOAD_INTEGER_FIELDS = (
    "task_count", "threads", "clients", "pipeline",
    "data_size_bytes", "key_maximum_total", "key_maximum",
)


def public_cluster_details(cluster_details: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Allowlisted as-applied workload settings from cluster_details.json."""
    source = cluster_details if isinstance(cluster_details, Mapping) else {}
    run = source.get("run") if isinstance(source.get("run"), Mapping) else {}
    memtier = source.get("memtier") if isinstance(source.get("memtier"), Mapping) else {}
    out: dict[str, dict[str, Any]] = {"run": {}, "memtier": {}}

    region = public_token(run.get("aws_region"), _REGION_RE)
    if region:
        out["run"]["aws_region"] = region
    for field in ("run_folder", "run_id"):
        token = public_token(run.get(field))
        if token:
            out["run"][field] = token
    for field in _WORKLOAD_INTEGER_FIELDS:
        number = public_number(memtier.get(field), minimum=0, maximum=10**15)
        if isinstance(number, int):
            out["memtier"][field] = number
    ratio = public_token(memtier.get("ratio"), _RATIO_RE)
    if ratio:
        out["memtier"]["ratio"] = ratio
    return out


#: Reason codes printed by scripts/fetch_elasticache_price.sh. Older runs stored
#: free text here (AWS CLI stderr, with the caller's ARN); those values -- and
#: anything else not on this list -- are reduced to "unspecified".
PRICE_REASON_CODES = frozenset({
    "missing_dependency_jq", "missing_dependency_aws", "bash_version_unsupported",
    "unknown_engine_type", "unknown_region", "pricing_request_failed",
    "no_ondemand_price_match", "non_positive_price",
})


def public_price_reason(value: Any) -> str:
    """Why a price is missing, as a fixed code; never the original free text."""
    text = _text(value)
    if not text:
        return ""
    return text if text in PRICE_REASON_CODES else "unspecified"


def public_iso_timestamp(value: Any) -> str | None:
    return public_token(value, _ISO_TS_RE)


def public_time_range(value: Any) -> str | None:
    return public_token(value, _TIME_RANGE_RE)


def public_availability_zone(value: Any) -> str:
    token = public_token(value, _AZ_RE)
    return token if token else "unknown"


def _row_list(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def public_loadgen_view(loadgen: Mapping[str, Any] | None) -> dict[str, Any]:
    """The subset of ``summary['loadgen']`` the HTML may show, validated.

    Task ids and AZ names originate in log/metric streams; they are shown only
    when they have the shape of an id / an AZ.
    """
    # An empty summary (no samples in the window) stays empty, so renderers
    # that skip a section for a falsy view keep doing so.
    if not isinstance(loadgen, Mapping) or not loadgen:
        return {}
    out: dict[str, Any] = {}

    for key in ("generator_cpu_p95_pct", "throughput_skew_within_az_max"):
        number = public_number(loadgen.get(key))
        if number is not None:
            out[key] = number

    # A malformed ElastiCache AZ is dropped (None), never turned into
    # "unknown": that would equal the bucket of tasks whose AZ is unknown and
    # label those tasks as co-located with the cache.
    out["elasticache_availability_zone"] = public_token(
        loadgen.get("elasticache_availability_zone"), _AZ_RE
    )
    source = public_token(loadgen.get("elasticache_availability_zone_source"), _SNAKE_RE)
    out["elasticache_availability_zone_source"] = source

    def task_rows(key: str, value_key: str) -> list[dict[str, Any]]:
        rows = []
        for row in _row_list(loadgen.get(key)):
            task_id = public_token(row.get("task_id"))
            if task_id is None:
                diagnostics.warning("%s row withheld from report: task_id does not match its contract", key)
                continue
            clean: dict[str, Any] = {
                "task_id": task_id,
                "availability_zone": public_availability_zone(row.get("availability_zone")),
                value_key: public_number(row.get(value_key)),
            }
            index = public_number(row.get("task_index"), minimum=0, maximum=100000)
            if index is not None:
                clean["task_index"] = int(index)
            rows.append(clean)
        return rows

    out["generator_cpu_p95_by_task"] = task_rows("generator_cpu_p95_by_task", "p95_pct")
    out["throughput_median_by_task"] = task_rows("throughput_median_by_task", "median_ops_sec")

    az_rows = []
    for row in _row_list(loadgen.get("throughput_by_az")):
        clean_row: dict[str, Any] = {
            "availability_zone": public_availability_zone(row.get("availability_zone")),
            # None (not 0) when missing: the renderer shows a fixed "n/a".
            "median_ops_sec": public_number(row.get("median_ops_sec")),
            "p90_to_p10": public_number(row.get("p90_to_p10")),
        }
        task_count = public_number(row.get("task_count"), minimum=0, maximum=100000)
        if task_count is not None:
            clean_row["task_count"] = task_count
        az_rows.append(clean_row)
    out["throughput_by_az"] = az_rows
    return out


def public_task_az_map(task_az_map: Mapping[str, str] | None) -> dict[str, str]:
    out = {}
    for task_id, az in (task_az_map or {}).items():
        clean_id = public_token(task_id)
        clean_az = public_token(az, _AZ_RE)
        if clean_id and clean_az:
            out[clean_id] = clean_az
    return out


def sanitize_metrics_frame(df: Any, source: str, *, required: bool = True) -> Any:
    """Drop CloudWatch-style rows whose labels are not label-shaped.

    ``Namespace``/``MetricName``/``Stat``/``Unit``/``Dimensions`` become chart
    trace names, legends, hover text and axis titles. A real row's labels fit
    the patterns above; a row that does not is withheld (and noted on the
    private log). A required frame that held rows but keeps none is fatal; an
    optional one (``required=False``) is returned empty instead.
    """
    if df is None or getattr(df, "empty", True):
        return df
    rules = {
        "Namespace": _METRIC_NAMESPACE_RE,
        "MetricName": _METRIC_NAME_RE,
        "Stat": _METRIC_STAT_RE,
        "Unit": _METRIC_UNIT_RE,
        "Dimensions": _METRIC_DIMENSIONS_RE,
    }
    keep = None
    for column, pattern in rules.items():
        if column not in df.columns:
            continue
        series = df[column]
        bad = {value for value in series.dropna().unique() if not pattern.fullmatch(str(value))}
        if not bad:
            continue
        diagnostics.warning(
            "%s: withholding rows whose %s does not match its contract (e.g. %r)",
            source, column, str(next(iter(bad)))[:80],
        )
        mask = ~series.isin(bad)
        keep = mask if keep is None else (keep & mask)
    if keep is None:
        return df
    cleaned = df[keep].copy()
    if cleaned.empty and required:
        raise ReportGenerationError(f"{source} has no publishable rows")
    return cleaned


def numeric_metric_values(df: Any, source: str) -> Any:
    """``df`` without the CloudWatch-style rows whose ``Value`` is not a number.

    Such a row is not a datapoint: it is withheld (and noted on the private
    log) instead of failing every aggregate that touches its column.
    """
    if df is None or getattr(df, "empty", True) or "Value" not in df.columns:
        return df
    import pandas as pd

    values = pd.to_numeric(df["Value"], errors="coerce")
    bad = values.isna() & df["Value"].notna()
    if not bad.any():
        if values.dtype != df["Value"].dtype:
            df = df.copy()
            df["Value"] = values
        return df
    diagnostics.warning("%s: withholding %d row(s) whose Value is not a number", source, int(bad.sum()))
    cleaned = df[~bad].copy()
    cleaned["Value"] = values[~bad]
    return cleaned


def numeric_columns(df: Any, source: str, *, exclude: tuple[str, ...] = ("Timestamp",)) -> Any:
    """``df`` with every column except ``exclude`` coerced to numbers.

    For frames whose values become chart data (memtier minute series): a cell
    that is not a number is withheld as NaN -- a gap in the chart -- instead
    of being embedded in the page as text. Noted on the private log.
    """
    if df is None or getattr(df, "empty", True):
        return df
    import pandas as pd

    out = df.copy()
    for column in out.columns:
        if column in exclude:
            continue
        original = out[column]
        coerced = pd.to_numeric(original, errors="coerce")
        dropped = int((coerced.isna() & original.notna()).sum())
        if dropped:
            diagnostics.warning("%s: withholding %d non-numeric value(s) in column %s", source, dropped, column)
        out[column] = coerced
    return out


# --------------------------------------------------------------------------- #
#  Final document check                                                        #
# --------------------------------------------------------------------------- #

#: (marker name, pattern). Names, never matched text, appear in exceptions.
#: Each pattern is a high-signal shape of diagnostic output. Ordinary report
#: vocabulary -- "ErrorCount", "Errors", "OOM Rejections", "error count" --
#: must NOT match; tests pin that.
_DIAGNOSTIC_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, flags))
    for name, pattern, flags in (
        ("aws-arn", r"arn:aws[a-z-]*:", re.IGNORECASE),
        ("aws-error-banner", r"An error occurred \(", 0),
        ("aws-error-code", r"\b(?:AccessDenied(?:Exception)?|UnauthorizedOperation|AuthFailure|"
                           r"ExpiredToken(?:Exception)?|InvalidClientTokenId|UnrecognizedClientException|"
                           r"NoSuchKey|NoSuchBucket|ThrottlingException)\b", 0),
        ("aws-not-authorized", r"is not authorized to perform", re.IGNORECASE),
        ("aws-identity", r"assumed-role/|AWSReservedSSO_|\bsts::", 0),
        # Access key ids are upper-case by definition; matching them
        # case-insensitively rejected names such as "asiapacificloadtest2".
        ("aws-credentials", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|(?i:aws_secret_access_key|aws_session_token)", 0),
        ("python-traceback", r"Traceback \(most recent call last\)", 0),
        ("python-frame", r'File "[^"]+", line \d+', 0),
        # "KeyError: x" and a bare "Error: connection refused" alike.
        ("exception-line", r"\b[A-Za-z]*(?:Error|Exception)\b\s*:", 0),
        # A standalone word only: "fix-stdout" or "stdout-cluster-1" are names.
        ("process-output", r"(?<![\w.-])(?:stderr|stdout)(?![\w.-])", re.IGNORECASE),
        # Any Windows drive path, Git-Bash style /c/..., and the usual POSIX
        # roots. A URL path ("https://host/opt/...") is not matched: the "/"
        # there follows a host name character.
        ("local-path", r"(?<![\w.])[A-Za-z]:\\{1,2}[A-Za-z0-9_. $-]+\\|"
                       r"(?<![\w.:/])/(?:[a-z]/)?(?:home|Users|root|tmp|opt|var|srv|mnt|app|work)/[A-Za-z0-9_.-]+", 0),
        ("html-comment", r"<!--", 0),
    )
)


_JS_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_JS_SIMPLE_ESCAPE = re.compile(r"\\([/\"'\\])")


def _decoded(document: str) -> str:
    """``document`` with JSON/JS string escapes and HTML entities undone.

    Plotly writes chart data with ``/`` as ``\\u002f`` and ``<`` as
    ``\\u003c``, and text nodes and attributes are HTML-escaped, so a marker
    can be present in the page without its literal characters.
    """
    text = _JS_UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), document)
    text = _JS_SIMPLE_ESCAPE.sub(r"\1", text)
    return html.unescape(text)


def find_diagnostic_markers(document: str) -> list[str]:
    """Names of the diagnostic markers present in ``document`` (empty if clean).

    Both the page as written and its decoded form are checked. Only marker
    names and offsets are logged -- never the matched text, which may be the
    very secret the check exists to stop.
    """
    views = [document]
    decoded = _decoded(document)
    if decoded != document:
        views.append(decoded)
    found = []
    for name, pattern in _DIAGNOSTIC_MARKERS:
        for view_name, view in zip(("page", "decoded page"), views):
            match = pattern.search(view)
            if match:
                diagnostics.error(
                    "publication check: marker %s in %s at offset %d", name, view_name, match.start(),
                )
                found.append(name)
                break
    return found


def assert_publishable_html(document: Any, *, context: str = "report") -> str:
    """Return ``document`` if it is a complete page free of diagnostic markers.

    Raises :class:`ReportGenerationError` otherwise. The message names marker
    categories only -- never the offending text.
    """
    if not isinstance(document, str) or "<html" not in document or "</html>" not in document:
        raise ReportGenerationError(f"{context} is not a complete HTML document")
    markers = find_diagnostic_markers(document)
    if markers:
        raise ReportGenerationError(
            f"{context} contains non-public diagnostic content ({', '.join(markers)}); nothing was written"
        )
    return document


def assert_publishable_json(document: Any, *, context: str = "summary") -> str:
    """Return ``document`` if it is JSON text free of diagnostic markers.

    The summary JSON is written and uploaded beside the HTML and carries
    configuration fields verbatim (its contract is the full run record), so
    it gets the same final scan: a free-text field that slipped in makes the
    whole report fail closed rather than be published as data.
    """
    if not isinstance(document, str):
        raise ReportGenerationError(f"{context} is not JSON text")
    markers = find_diagnostic_markers(document)
    if markers:
        raise ReportGenerationError(
            f"{context} contains non-public diagnostic content ({', '.join(markers)}); nothing was written"
        )
    return document


# --------------------------------------------------------------------------- #
#  Writing                                                                     #
# --------------------------------------------------------------------------- #

def _default_file_mode() -> int:
    """The mode a plain ``open(..., "w")`` would give a new file (0666 & ~umask)."""
    if os.name == "nt":
        return 0o666
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_files_atomically(files: Mapping[Path, str]) -> None:
    """Write every file, or none of them, never leaving a partial file.

    Each file is staged next to its destination (with the mode a normal write
    would give it: the existing file's mode, else 0666 minus the umask) and
    moved into place with ``os.replace``. Symlinked targets are written
    through, as ``Path.write_text`` did. If staging fails nothing is
    replaced; if a later move fails, files already moved are rolled back to
    their previous content (or removed if they did not exist), so the set is
    never left half-updated. Any failure raises :class:`ReportGenerationError`.
    """
    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for target, content in files.items():
            target = Path(os.path.realpath(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (target.stat().st_mode & 0o7777) if target.exists() else _default_file_mode()
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
            staged.append((Path(tmp_name), target))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
        # Keep a copy of every file about to be replaced, so a failure part
        # way through the moves can put the old set back.
        for _tmp_path, target in staged:
            if target.exists():
                fd, backup_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".bak")
                os.close(fd)
                shutil.copy2(target, backup_name)
                backups[target] = Path(backup_name)
            else:
                backups[target] = None
        for tmp_path, target in staged:
            os.replace(tmp_path, target)
            committed.append(target)
        staged.clear()
    except Exception as exc:
        for target in reversed(committed):
            backup = backups.get(target)
            try:
                if backup is None:
                    target.unlink()
                else:
                    os.replace(backup, target)
                    backups[target] = None
            except OSError as rollback_exc:
                log_failure(f"restoring {target.name}", rollback_exc)
        log_failure("writing report files", exc)
        raise ReportGenerationError("report files could not be written; previous files were kept") from exc
    finally:
        for tmp_path, _target in staged:
            _unlink_quietly(tmp_path)
        for backup in backups.values():
            if backup is not None:
                _unlink_quietly(backup)
