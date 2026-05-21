from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

import boto3

from report_generator import run_uploaded_report


STATISTICS = ["Average", "Sum", "Maximum", "Minimum"]
LOG_EXPORT_PART_SIZE = 6 * 1024 * 1024

REQUIRED_ELASTICACHE_METRICS = [
    "CacheHitRate",
    "CacheHits",
    "CacheMisses",
    "CPUCreditBalance",
    "CPUCreditUsage",
    "CPUUtilization",
    "CurrConnections",
    "CurrItems",
    "DatabaseCapacityUsageCountedForEvictPercentage",
    "DatabaseCapacityUsagePercentage",
    "DatabaseMemoryUsageCountedForEvictPercentage",
    "DatabaseMemoryUsagePercentage",
    "EngineCPUUtilization",
    "Evictions",
    "FreeableMemory",
    "GetTypeCmds",
    "GetTypeCmdsLatency",
    "MemoryFragmentationRatio",
    "NetworkBandwidthInAllowanceExceeded",
    "NetworkBandwidthOutAllowanceExceeded",
    "NetworkBytesIn",
    "NetworkBytesOut",
    "NetworkConntrackAllowanceExceeded",
    "NetworkPacketsPerSecondAllowanceExceeded",
    "NewConnections",
    "SetTypeCmds",
    "SetTypeCmdsLatency",
    "StringBasedCmds",
    "StringBasedCmdsLatency",
    "SwapUsage",
]

REQUIRED_ECS_METRICS = [
    "CPUUtilization",
    "MemoryUtilization",
    "RunningTaskCount",
    "TaskCount",
    "TaskCpuUtilization",
    "TaskMemoryUtilization",
    "CpuUtilized",
    "MemoryUtilized",
    "NetworkRxBytes",
    "NetworkTxBytes",
    "ContainerCpuUtilization",
    "ContainerMemoryUtilization",
    "ContainerNetworkRxBytes",
    "ContainerNetworkTxBytes",
]

REPORT_CONTRACT_METRICS = [
    "CacheHits",
    "CacheMisses",
    "CurrConnections",
    "CurrItems",
    "DatabaseCapacityUsageCountedForEvictPercentage",
    "DatabaseMemoryUsageCountedForEvictPercentage",
    "EngineCPUUtilization",
    "Evictions",
    "FreeableMemory",
    "GetTypeCmdsLatency",
    "SetTypeCmdsLatency",
    "StringBasedCmdsLatency",
]


class _LazyBoto3Client:
    def __init__(self, service_name: str):
        self.service_name = service_name
        self.client = None

    def __getattr__(self, name: str):
        if self.client is None:
            self.client = boto3.client(self.service_name)
        return getattr(self.client, name)


cloudwatch = _LazyBoto3Client("cloudwatch")
logs = _LazyBoto3Client("logs")
s3 = _LazyBoto3Client("s3")
elasticache = _LazyBoto3Client("elasticache")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _dimensions_to_str(dimensions: list[dict[str, str]]) -> str:
    return ";".join(
        f"{name}={value}" for name, value in sorted((d["Name"], d["Value"]) for d in dimensions)
    )


def _read_s3_text(bucket: str, key: str) -> str:
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8", "replace")


def _put_json(bucket: str, key: str, payload: dict) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def _safe_s3_key_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._=-]+", "_", value.strip())
    return safe or "_"


def _log_stream_key(key_prefix: str, stream_name: str) -> str:
    parts = [_safe_s3_key_part(part) for part in stream_name.split("/") if part]
    filename = "/".join(parts) if parts else _safe_s3_key_part(stream_name)
    return f"{key_prefix.rstrip('/')}/{filename}.txt"


def _list_log_stream_names(log_group: str) -> list[str]:
    streams = []
    token = None
    while True:
        params = {
            "logGroupName": log_group,
            "orderBy": "LogStreamName",
        }
        if token:
            params["nextToken"] = token

        response = logs.describe_log_streams(**params)
        streams.extend(stream["logStreamName"] for stream in response.get("logStreams", []))
        token = response.get("nextToken")
        if not token:
            break
    return sorted(dict.fromkeys(streams))


def _fallback_member_clusters(replication_group_id: str) -> list[str]:
    cluster_mode = os.environ.get("CLUSTER_MODE", "false").strip().lower() == "true"
    if cluster_mode:
        node_groups = max(_env_int("NUM_NODE_GROUPS", 1), 1)
        replicas = max(_env_int("REPLICAS_PER_NODE_GROUP", 0), 0)
        return [
            f"{replication_group_id}-{node_group:04d}-{replica:03d}"
            for node_group in range(1, node_groups + 1)
            for replica in range(1, replicas + 2)
        ]

    num_nodes = max(_env_int("NUM_CACHE_NODES", 1), 1)
    return [f"{replication_group_id}-{node:03d}" for node in range(1, num_nodes + 1)]


def _list_metrics(namespace: str, filter_dimensions=None, metric_name_filter=None) -> list[dict]:
    metrics = []
    token = None
    while True:
        params = {"Namespace": namespace}
        if filter_dimensions:
            params["Dimensions"] = filter_dimensions
        if token:
            params["NextToken"] = token

        response = cloudwatch.list_metrics(**params)
        for metric in response.get("Metrics", []):
            metric_name = metric.get("MetricName")
            if not metric_name:
                continue
            if metric_name_filter and metric_name not in metric_name_filter:
                continue
            metrics.append({"MetricName": metric_name, "Dimensions": metric.get("Dimensions", [])})

        token = response.get("NextToken")
        if not token:
            break
    return metrics


def export_metric_sources_to_s3(sources, bucket: str, key: str, start_time: datetime, end_time: datetime) -> str:
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["Timestamp", "Namespace", "MetricName", "Stat", "Value", "Unit", "Dimensions"])

    metric_map = {}
    for source in sources:
        namespace = source["namespace"]
        filter_dimensions = source.get("dimensions") or []
        metric_name_filter = set(source.get("metric_names", [])) if source.get("metric_names") else None

        for metric_name in sorted(metric_name_filter or []):
            dims_key = tuple(sorted((d["Name"], d["Value"]) for d in filter_dimensions))
            metric_map[(namespace, metric_name, dims_key)] = filter_dimensions

        try:
            metrics = _list_metrics(namespace, filter_dimensions, metric_name_filter)
        except Exception as exc:
            print(f"Error listing metrics for {namespace} {filter_dimensions}: {exc}")
            metrics = []

        for metric in metrics:
            dimensions = metric.get("Dimensions", [])
            dims_key = tuple(sorted((d["Name"], d["Value"]) for d in dimensions))
            metric_map[(namespace, metric["MetricName"], dims_key)] = dimensions

    for (namespace, metric_name, _dims_key), dimensions in metric_map.items():
        dimensions_str = _dimensions_to_str(dimensions)
        try:
            response = cloudwatch.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=STATISTICS,
            )
        except Exception as exc:
            print(f"Error fetching metric {namespace}/{metric_name} for {dimensions_str}: {exc}")
            continue

        for datapoint in sorted(response.get("Datapoints", []), key=lambda d: d["Timestamp"]):
            ts = datapoint["Timestamp"].isoformat()
            unit = datapoint.get("Unit", "None")
            for stat in STATISTICS:
                if stat in datapoint:
                    writer.writerow([ts, namespace, metric_name, stat, datapoint[stat], unit, dimensions_str])

    s3.put_object(Bucket=bucket, Key=key, Body=csv_buffer.getvalue(), ContentType="text/csv")
    print(f"Metrics exported to s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"


def export_elasticache_metrics_to_s3(replication_group_id, bucket, key, start_time, end_time) -> str:
    sources = [
        {
            "namespace": "AWS/ElastiCache",
            "dimensions": [{"Name": "ReplicationGroupId", "Value": replication_group_id}],
            "metric_names": REQUIRED_ELASTICACHE_METRICS,
        }
    ]

    member_clusters = []
    try:
        response = elasticache.describe_replication_groups(ReplicationGroupId=replication_group_id)
        for group in response.get("ReplicationGroups", []):
            member_clusters = group.get("MemberClusters", [])
    except Exception as exc:
        print(f"Describe replication group unavailable, using derived node IDs: {exc}")

    member_clusters = list(dict.fromkeys(member_clusters or []))
    for cluster_id in _fallback_member_clusters(replication_group_id):
        if cluster_id not in member_clusters:
            member_clusters.append(cluster_id)

    for cluster_id in member_clusters:
        sources.append(
            {
                "namespace": "AWS/ElastiCache",
                "dimensions": [{"Name": "CacheClusterId", "Value": cluster_id}],
                "metric_names": REQUIRED_ELASTICACHE_METRICS,
            }
        )

    print(f"ElastiCache metric sources: {[s['dimensions'] for s in sources]}")
    return export_metric_sources_to_s3(sources, bucket, key, start_time, end_time)


def export_ecs_metrics_to_s3(cluster, service, bucket, key, start_time, end_time) -> str:
    sources = [
        {
            "namespace": "AWS/ECS",
            "dimensions": [{"Name": "ClusterName", "Value": cluster}, {"Name": "ServiceName", "Value": service}],
            "metric_names": REQUIRED_ECS_METRICS,
        },
        {
            "namespace": "ECS/ContainerInsights",
            "dimensions": [{"Name": "ClusterName", "Value": cluster}],
            "metric_names": REQUIRED_ECS_METRICS,
        },
        {
            "namespace": "ECS/ContainerInsights",
            "dimensions": [{"Name": "ClusterName", "Value": cluster}, {"Name": "ServiceName", "Value": service}],
            "metric_names": REQUIRED_ECS_METRICS,
        },
    ]
    return export_metric_sources_to_s3(sources, bucket, key, start_time, end_time)


def export_logs_to_s3(log_group, bucket, key, start_time=None, end_time=None, log_stream_name_prefix: str = "") -> str | None:
    if not log_group:
        return None

    buffer = bytearray()
    upload_id = None
    parts = []
    part_number = 1

    def _start_multipart():
        nonlocal upload_id
        if upload_id is None:
            resp = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="text/plain")
            upload_id = resp["UploadId"]

    def _upload_part(data):
        nonlocal part_number
        _start_multipart()
        resp = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=data)
        parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
        part_number += 1

    def _flush(force=False):
        nonlocal buffer
        if not buffer:
            return
        if len(buffer) < LOG_EXPORT_PART_SIZE and not force:
            return
        _upload_part(bytes(buffer))
        buffer = bytearray()

    buffer.extend(f"LogGroup: {log_group}\n".encode("utf-8", "replace"))
    next_token = None
    try:
        while True:
            params = {
                "logGroupName": log_group,
                "interleaved": True,
            }
            if start_time is not None:
                params["startTime"] = int(start_time.timestamp() * 1000)
            if end_time is not None:
                params["endTime"] = int(end_time.timestamp() * 1000)
            if log_stream_name_prefix:
                params["logStreamNamePrefix"] = log_stream_name_prefix
            if next_token:
                params["nextToken"] = next_token

            response = logs.filter_log_events(**params)
            for event in response.get("events", []):
                ts = datetime.fromtimestamp(event["timestamp"] / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat()
                stream = event.get("logStreamName", "")
                message = event.get("message", "").rstrip("\n")
                buffer.extend(f"[{ts}] [{stream}] {message}\n".encode("utf-8", "replace"))
                if len(buffer) >= LOG_EXPORT_PART_SIZE:
                    _flush()

            token = response.get("nextToken")
            if not token or token == next_token:
                break
            next_token = token
    except Exception as exc:
        print(f"Error fetching logs from {log_group}: {exc}")
        buffer.extend(f"Error fetching logs from {log_group}: {exc}\n".encode("utf-8", "replace"))

    if upload_id:
        try:
            _flush(force=True)
            s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        except Exception:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            raise
    else:
        s3.put_object(Bucket=bucket, Key=key, Body=bytes(buffer), ContentType="text/plain")

    print(f"Logs exported to s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"


def iter_log_stream_events(log_group, log_stream_name):
    token = None
    args = {
        "logGroupName": log_group,
        "logStreamName": log_stream_name,
        "startFromHead": True,
    }

    while True:
        if token:
            args["nextToken"] = token

        response = logs.get_log_events(**args)

        for event in response["events"]:
            yield event

        next_token = response["nextForwardToken"]
        if next_token == token:
            break
        token = next_token


def _log_stream_event_line(event: dict) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode("utf-8", "replace") + b"\n"


def _cloudwatch_event_datetime(event: dict) -> datetime | None:
    timestamp = event.get("timestamp")
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _is_memtier_stream(log_stream_name: str) -> bool:
    return log_stream_name.split("/", 1)[0] == "memtier"


def write_log_stream(log_group: str, log_stream_name: str, write_line) -> dict:
    result = {
        "event_count": 0,
        "first_message_ts": None,
        "last_message_ts": None,
    }
    for event in iter_log_stream_events(log_group, log_stream_name):
        write_line(_log_stream_event_line(event))
        result["event_count"] += 1
        event_ts = _cloudwatch_event_datetime(event)
        if event_ts is None:
            continue
        first_ts = result["first_message_ts"]
        last_ts = result["last_message_ts"]
        if first_ts is None or event_ts < first_ts:
            result["first_message_ts"] = event_ts
        if last_ts is None or event_ts > last_ts:
            result["last_message_ts"] = event_ts
    return result


def write_log_stream_to_file(log_group: str, log_stream_name: str, output_path: str) -> dict:
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "wb") as output_file:
        return write_log_stream(log_group, log_stream_name, output_file.write)


def _export_log_stream_to_s3_with_status(log_group, log_stream_name, bucket, key) -> dict | None:
    if not log_group or not log_stream_name:
        return None

    buffer = bytearray()
    upload_id = None
    parts = []
    part_number = 1

    def _start_multipart():
        nonlocal upload_id
        if upload_id is None:
            resp = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="text/plain")
            upload_id = resp["UploadId"]

    def _upload_part(data):
        nonlocal part_number
        _start_multipart()
        resp = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=data)
        parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
        part_number += 1

    def _flush(force=False):
        nonlocal buffer
        if not buffer:
            return
        if len(buffer) < LOG_EXPORT_PART_SIZE and not force:
            return
        _upload_part(bytes(buffer))
        buffer = bytearray()

    def _append_line(line: bytes):
        buffer.extend(line)
        if len(buffer) >= LOG_EXPORT_PART_SIZE:
            _flush()

    result = write_log_stream(log_group, log_stream_name, _append_line)

    if upload_id:
        try:
            _flush(force=True)
            s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        except Exception:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            raise
    else:
        s3.put_object(Bucket=bucket, Key=key, Body=bytes(buffer), ContentType="text/plain")

    print(f"Log stream {log_stream_name} exported to s3://{bucket}/{key}")
    result["artifact"] = f"s3://{bucket}/{key}"
    return result


def export_log_stream_to_s3(log_group, log_stream_name, bucket, key) -> str | None:
    result = _export_log_stream_to_s3_with_status(log_group, log_stream_name, bucket, key)
    if result is None:
        return None
    return result["artifact"]


def _export_log_streams_to_s3(log_group, bucket, key_prefix, streams) -> list[dict]:
    files = []
    for stream in streams:
        key = _log_stream_key(key_prefix, stream)
        result = _export_log_stream_to_s3_with_status(log_group, stream, bucket, key)
        artifact = result.get("artifact") if result else None
        files.append({
            "stream": stream,
            "artifact": artifact,
            "event_count": result.get("event_count", 0) if result else 0,
            "first_message_ts": result.get("first_message_ts") if result else None,
            "last_message_ts": result.get("last_message_ts") if result else None,
        })
    return files


def export_loadgen_logs_to_s3(log_group, bucket, key_prefix) -> dict:
    key_prefix = key_prefix.rstrip("/")
    status = {
        "artifact": f"s3://{bucket}/{key_prefix}/",
        "files": [],
        "complete": False,
    }
    if not log_group:
        return status

    streams = _list_log_stream_names(log_group)
    files = _export_log_streams_to_s3(log_group, bucket, key_prefix, streams)
    status["files"] = files
    status["stream_count"] = len(streams)
    status["complete"] = len(files) == len(streams) and len(streams) > 0
    first_ts = None
    last_ts = None
    for file_status in files:
        if not _is_memtier_stream(file_status.get("stream", "")):
            continue
        file_first_ts = file_status.get("first_message_ts")
        file_last_ts = file_status.get("last_message_ts")
        if file_first_ts is not None and (first_ts is None or file_first_ts < first_ts):
            first_ts = file_first_ts
        if file_last_ts is not None and (last_ts is None or file_last_ts > last_ts):
            last_ts = file_last_ts
    status["first_message_ts"] = first_ts
    status["last_message_ts"] = last_ts
    return status


def _csv_metric_rows(bucket: str, key: str) -> list[dict]:
    content = _read_s3_text(bucket, key)
    return list(csv.DictReader(io.StringIO(content)))


def _metric_value(row: dict) -> float:
    try:
        return float(row.get("Value", "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def _metric_contract_status(bucket: str, key: str) -> dict:
    try:
        rows = _csv_metric_rows(bucket, key)
    except Exception as exc:
        return {"complete": False, "missing": REPORT_CONTRACT_METRICS, "error": str(exc)}

    names = {row.get("MetricName", "") for row in rows if row.get("MetricName")}
    missing = [name for name in REPORT_CONTRACT_METRICS if name not in names]
    cache_ops = sum(
        _metric_value(row)
        for row in rows
        if row.get("MetricName") in {"CacheHits", "CacheMisses"} and row.get("Stat") == "Sum"
    )
    curr_items_max = max(
        (
            _metric_value(row)
            for row in rows
            if row.get("MetricName") == "CurrItems" and row.get("Stat") == "Maximum"
        ),
        default=0.0,
    )
    value_failures = []
    if cache_ops <= 0:
        value_failures.append("CacheHits/CacheMisses have no positive Sum datapoints")
    if curr_items_max <= 0:
        value_failures.append("CurrItems has no positive Maximum datapoints")

    return {
        "complete": not missing and not value_failures,
        "missing": missing,
        "value_failures": value_failures,
        "present_count": len(names),
    }


def _ses_config():
    email = os.environ.get("NOTIFICATION_EMAIL", "").strip()
    ses_arn = os.environ.get("SES_IDENTITY_ARN", "").strip()
    if not email or not ses_arn:
        return None
    arn_match = re.match(r"arn:aws:ses:([^:]+):[^:]+:identity/(.+)", ses_arn)
    if not arn_match:
        print(f"Invalid SES ARN format: {ses_arn}")
        return None
    identity = arn_match.group(2)
    return {
        "client": boto3.client("ses", region_name=arn_match.group(1)),
        "source": identity if "@" in identity else f"aws-elasticache-lab@{identity}",
        "to": email,
    }


def send_report_ready_email(cluster_id: str, report_uri: str, summary_uri: str, bucket: str, prefix: str, timestamp: str):
    config = _ses_config()
    if not config:
        print("Report-ready email disabled.")
        return None

    cluster_html = html.escape(cluster_id, quote=True)
    report_html = html.escape(report_uri, quote=True)
    summary_html = html.escape(summary_uri, quote=True)
    s3_html = html.escape(f"s3://{bucket}/{prefix}{timestamp}/", quote=True)
    body_text = (
        "ElastiCache report ready.\n\n"
        f"Cluster: {cluster_id}\n"
        f"Report: {report_uri}\n"
        f"Summary: {summary_uri}\n"
    )
    body_html = f"""\
<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#f4f6f9;margin:0;padding:30px;">
  <table width="600" align="center" style="background:#fff;border-radius:8px;overflow:hidden;border-collapse:collapse;">
    <tr><td style="background:linear-gradient(135deg,#1e8e3e,#137333);padding:28px 36px;color:#fff;">
      <div style="font-size:13px;text-transform:uppercase;letter-spacing:1px;opacity:.85;">Performance Test</div>
      <h1 style="margin:6px 0 0;font-size:24px;">Report Ready</h1>
    </td></tr>
    <tr><td style="padding:24px 36px;">
      <p style="margin:0 0 16px;color:#202124;">Report generation completed for <strong>{cluster_html}</strong>.</p>
      <p style="font-family:monospace;font-size:13px;color:#202124;">{report_html}</p>
      <p style="font-family:monospace;font-size:13px;color:#202124;">{summary_html}</p>
      <p style="font-size:12px;color:#80868b;">Artifacts: {s3_html}</p>
    </td></tr>
  </table>
</body></html>"""
    response = config["client"].send_email(
        Source=config["source"],
        Destination={"ToAddresses": [config["to"]]},
        Message={
            "Subject": {"Data": f"[ElastiCache Report Ready] {cluster_id}", "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": body_text, "Charset": "UTF-8"},
                "Html": {"Data": body_html, "Charset": "UTF-8"},
            },
        },
    )
    print(f"Report-ready notification sent to {config['to']}, MessageId: {response['MessageId']}")
    return True


def main() -> None:
    cluster_id = os.environ["CLUSTER_ID"]
    elasticache_id = os.environ.get("ELASTICACHE_ID", cluster_id)
    ecs_cluster = os.environ["ECS_CLUSTER"]
    ecs_service = os.environ["ECS_SERVICE"]
    bucket = os.environ["S3_BUCKET"]
    prefix = os.environ.get("S3_PREFIX", "exports/")
    timestamp = os.environ.get("REPORT_TIMESTAMP") or os.environ.get("RUN_FOLDER")
    if not timestamp:
        raise RuntimeError("REPORT_TIMESTAMP or RUN_FOLDER is required")

    metrics_key = f"{prefix}{timestamp}/metrics/{cluster_id}.csv"
    ecs_metrics_key = f"{prefix}{timestamp}/metrics/{cluster_id}-ecs.csv"
    loadgen_logs_prefix = f"{prefix}{timestamp}/logs/loadgen"
    status_key = f"{prefix}{timestamp}/report_status.json"
    status = {
        "cluster_id": cluster_id,
        "timestamp": timestamp,
        "complete": False,
        "checks": {},
        "artifacts": {
            "metrics": f"s3://{bucket}/{metrics_key}",
            "ecs_metrics": f"s3://{bucket}/{ecs_metrics_key}",
            "loadgen_logs": f"s3://{bucket}/{loadgen_logs_prefix}/",
        },
    }

    loadgen_status = export_loadgen_logs_to_s3(
        os.environ.get("LOADGEN_LOG_GROUP") or os.environ.get("LOG_GROUP"),
        bucket,
        loadgen_logs_prefix,
    )
    status["checks"]["loadgen_logs"] = loadgen_status
    start_time = loadgen_status.get("first_message_ts")
    end_time = loadgen_status.get("last_message_ts")
    if start_time is None or end_time is None:
        status["checks"]["metrics"] = {
            "complete": False,
            "error": "memtier log message window is unavailable",
        }
        _put_json(bucket, status_key, status)
        raise RuntimeError(
            "Report data contract incomplete; memtier log message window is unavailable. "
            f"Details written to s3://{bucket}/{status_key}"
        )

    export_elasticache_metrics_to_s3(elasticache_id, bucket, metrics_key, start_time, end_time)
    export_ecs_metrics_to_s3(ecs_cluster, ecs_service, bucket, ecs_metrics_key, start_time, end_time)
    status["checks"]["metrics"] = _metric_contract_status(bucket, metrics_key)

    export_logs_to_s3(
        os.environ.get("CONTAINER_INSIGHTS_LOG_GROUP"),
        bucket,
        f"{prefix}{timestamp}/logs/container-insights/{cluster_id}.txt",
        start_time,
        end_time,
    )
    export_logs_to_s3(
        os.environ.get("ELASTICACHE_LOG_GROUP"),
        bucket,
        f"{prefix}{timestamp}/logs/elasticache/{cluster_id}.txt",
        start_time,
        end_time,
    )
    export_logs_to_s3(
        os.environ.get("LAMBDA_SCHEDULER_LOG_GROUP"),
        bucket,
        f"{prefix}{timestamp}/logs/lambda-shutdown-scheduler/{cluster_id}.txt",
        start_time,
        end_time,
    )

    status["complete"] = (
        status["checks"]["loadgen_logs"].get("complete", False)
        and status["checks"]["metrics"].get("complete", False)
    )
    _put_json(bucket, status_key, status)
    if not status["complete"]:
        missing = {
            name: check
            for name, check in status["checks"].items()
            if not check.get("complete", False)
        }
        raise RuntimeError(
            "Report data contract incomplete; refusing to generate an empty report. "
            f"Details written to s3://{bucket}/{status_key}: {missing}"
        )

    os.environ["METRICS_CSV"] = f"s3://{bucket}/{metrics_key}"
    os.environ["ECS_METRICS_CSV"] = f"s3://{bucket}/{ecs_metrics_key}"
    os.environ["LOGS_PREFIX"] = f"s3://{bucket}/{loadgen_logs_prefix}/"
    os.environ["OUTPUT_BUCKET"] = bucket
    os.environ["OUTPUT_PREFIX"] = prefix
    os.environ["SUFFIX"] = timestamp
    os.environ["REPORT_TIMESTAMP"] = timestamp
    run_uploaded_report()

    report_uri = f"s3://{bucket}/{prefix}{timestamp}/results_{timestamp}.html"
    summary_uri = f"s3://{bucket}/{prefix}{timestamp}/results_{timestamp}.json"
    status["report"] = report_uri
    status["summary"] = summary_uri
    _put_json(bucket, status_key, status)
    send_report_ready_email(cluster_id, report_uri, summary_uri, bucket, prefix, timestamp)


def download_stream_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download one CloudWatch log stream as raw JSONL events.")
    parser.add_argument("--log-group", required=True)
    parser.add_argument("--log-stream", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    global logs
    logs = boto3.client("logs", region_name=args.region)
    result = write_log_stream_to_file(args.log_group, args.log_stream, args.output)
    print(f"Wrote {result['event_count']} events from {args.log_stream} to {args.output}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "download-stream":
        download_stream_cli(sys.argv[2:])
    else:
        main()
