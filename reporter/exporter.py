from __future__ import annotations

import csv
import html
import io
import os
import re
from datetime import datetime, timedelta

import boto3

from report_generator import run_uploaded_report


STATISTICS = ["Average", "Sum", "Maximum", "Minimum"]
EXPORT_BUFFER_MINUTES = 5
LOG_EXPORT_PART_SIZE = 6 * 1024 * 1024


cloudwatch = boto3.client("cloudwatch")
logs = boto3.client("logs")
s3 = boto3.client("s3")
elasticache = boto3.client("elasticache")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _time_window(duration_minutes: int) -> tuple[datetime, datetime]:
    end_time = datetime.utcnow()
    lookback_minutes = max(duration_minutes, 1) + EXPORT_BUFFER_MINUTES
    return end_time - timedelta(minutes=lookback_minutes), end_time


def _dimensions_to_str(dimensions: list[dict[str, str]]) -> str:
    return ";".join(
        f"{name}={value}" for name, value in sorted((d["Name"], d["Value"]) for d in dimensions)
    )


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
        metric_filter = set(source.get("metric_names", [])) if source.get("metric_names") else None
        try:
            metrics = _list_metrics(namespace, filter_dimensions, metric_filter)
        except Exception as exc:
            print(f"Error listing metrics for {namespace} {filter_dimensions}: {exc}")
            continue

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
            }
        )

    print(f"ElastiCache metric sources: {[s['dimensions'] for s in sources]}")
    return export_metric_sources_to_s3(sources, bucket, key, start_time, end_time)


def export_ecs_metrics_to_s3(cluster, service, bucket, key, start_time, end_time) -> str:
    sources = [
        {
            "namespace": "AWS/ECS",
            "dimensions": [{"Name": "ClusterName", "Value": cluster}, {"Name": "ServiceName", "Value": service}],
        },
        {"namespace": "ECS/ContainerInsights", "dimensions": [{"Name": "ClusterName", "Value": cluster}]},
        {
            "namespace": "ECS/ContainerInsights",
            "dimensions": [{"Name": "ClusterName", "Value": cluster}, {"Name": "ServiceName", "Value": service}],
        },
    ]
    return export_metric_sources_to_s3(sources, bucket, key, start_time, end_time)


def export_logs_to_s3(log_group, bucket, key, start_time, end_time) -> str | None:
    if not log_group:
        return None

    start_time_ms = int(start_time.timestamp() * 1000)
    end_time_ms = int(end_time.timestamp() * 1000)
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
                "startTime": start_time_ms,
                "endTime": end_time_ms,
                "interleaved": True,
            }
            if next_token:
                params["nextToken"] = next_token

            response = logs.filter_log_events(**params)
            for event in response.get("events", []):
                ts = datetime.fromtimestamp(event["timestamp"] / 1000).isoformat()
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

    start_time, end_time = _time_window(_env_int("TEST_DURATION_MINUTES", 60))
    metrics_key = f"{prefix}{timestamp}/metrics/{cluster_id}.csv"
    ecs_metrics_key = f"{prefix}{timestamp}/metrics/{cluster_id}-ecs.csv"
    logs_key = f"{prefix}{timestamp}/logs/{cluster_id}.txt"

    export_elasticache_metrics_to_s3(elasticache_id, bucket, metrics_key, start_time, end_time)
    export_ecs_metrics_to_s3(ecs_cluster, ecs_service, bucket, ecs_metrics_key, start_time, end_time)
    export_logs_to_s3(os.environ.get("LOADGEN_LOG_GROUP") or os.environ.get("LOG_GROUP"), bucket, logs_key, start_time, end_time)
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

    os.environ["METRICS_CSV"] = f"s3://{bucket}/{metrics_key}"
    os.environ["ECS_METRICS_CSV"] = f"s3://{bucket}/{ecs_metrics_key}"
    os.environ["LOGS_TXT"] = f"s3://{bucket}/{logs_key}"
    os.environ["OUTPUT_BUCKET"] = bucket
    os.environ["OUTPUT_PREFIX"] = prefix
    os.environ["SUFFIX"] = timestamp
    os.environ["REPORT_TIMESTAMP"] = timestamp
    run_uploaded_report()

    report_uri = f"s3://{bucket}/{prefix}{timestamp}/results_{timestamp}.html"
    summary_uri = f"s3://{bucket}/{prefix}{timestamp}/results_{timestamp}.json"
    send_report_ready_email(cluster_id, report_uri, summary_uri, bucket, prefix, timestamp)


if __name__ == "__main__":
    main()
