import boto3
import csv
import io
import json
import os
import re
from datetime import datetime, timedelta

# Initialize clients
ecs = boto3.client('ecs')
elasticache = boto3.client('elasticache')
cloudwatch = boto3.client('cloudwatch')
logs = boto3.client('logs')
s3 = boto3.client('s3')

STATISTICS = ['Average', 'Sum', 'Maximum', 'Minimum']
EXPORT_BUFFER_MINUTES = 5
LOG_EXPORT_PART_SIZE = 6 * 1024 * 1024


def _time_window(duration_minutes):
    end_time = datetime.utcnow()
    lookback_minutes = max(duration_minutes, 1) + EXPORT_BUFFER_MINUTES
    start_time = end_time - timedelta(minutes=lookback_minutes)
    return start_time, end_time


def _dimensions_to_str(dimensions):
    return ";".join(
        [f"{name}={value}" for name, value in sorted((d['Name'], d['Value']) for d in dimensions)]
    )


def _list_metrics(namespace, filter_dimensions=None, metric_name_filter=None):
    metrics = []
    token = None

    while True:
        params = {'Namespace': namespace}
        if filter_dimensions:
            params['Dimensions'] = filter_dimensions
        if token:
            params['NextToken'] = token

        response = cloudwatch.list_metrics(**params)
        for metric in response.get('Metrics', []):
            metric_name = metric.get('MetricName')
            if not metric_name:
                continue
            if metric_name_filter and metric_name not in metric_name_filter:
                continue
            metrics.append({
                'MetricName': metric_name,
                'Dimensions': metric.get('Dimensions', [])
            })

        token = response.get('NextToken')
        if not token:
            break

    return metrics


def handler(event, context):
    """
    Lambda handler for shutdown orchestration:
    1. Scale ECS service to 0
    2. Delete ElastiCache replication group
    3. Export CloudWatch metrics to S3 (CSV)
    4. Export CloudWatch Logs to S3 (text)
    """

    cluster_id = os.environ['CLUSTER_ID']
    ecs_cluster = os.environ['ECS_CLUSTER']
    ecs_service = os.environ['ECS_SERVICE']
    elasticache_id = os.environ['ELASTICACHE_ID']
    s3_bucket = os.environ['S3_BUCKET']
    s3_prefix = os.environ.get('S3_PREFIX', 'exports/')
    loadgen_log_group = os.environ.get('LOADGEN_LOG_GROUP') or os.environ.get('LOG_GROUP')
    container_insights_log_group = os.environ.get('CONTAINER_INSIGHTS_LOG_GROUP')
    elasticache_log_group = os.environ.get('ELASTICACHE_LOG_GROUP')
    lambda_shutdown_log_group = os.environ.get('LAMBDA_SHUTDOWN_LOG_GROUP')
    lambda_scheduler_log_group = os.environ.get('LAMBDA_SCHEDULER_LOG_GROUP')
    test_duration_minutes = int(os.environ.get('TEST_DURATION_MINUTES', '60'))

    start_time, end_time = _time_window(test_duration_minutes)
    timestamp = end_time.strftime('%Y%m%d-%H%M%S')

    results = {
        'metrics_export': None,
        'ecs_metrics_export': None,
        'log_exports': {},
        'ecs_stopped': False,
        'elasticache_stopped': False
    }

    try:
        try:
            ecs.update_service(
                cluster=ecs_cluster,
                service=ecs_service,
                desiredCount=0
            )
            results['ecs_stopped'] = True
            print(f"ECS service {ecs_service} scaled to 0")
        except Exception as e:
            print(f"ECS stop note: {e}")
            results['ecs_stopped'] = str(e)

        try:
            delete_params = {
                "ReplicationGroupId": elasticache_id,
                "RetainPrimaryCluster": False
            }
            final_snapshot_id = os.environ.get("ELASTICACHE_FINAL_SNAPSHOT_ID")
            if final_snapshot_id:
                delete_params["FinalSnapshotIdentifier"] = final_snapshot_id

            elasticache.delete_replication_group(**delete_params)
            results['elasticache_stopped'] = True
            print(f"ElastiCache {elasticache_id} delete initiated")
        except Exception as e:
            print(f"ElastiCache delete note: {e}")
            results['elasticache_stopped'] = str(e)

        metrics_key = f"{s3_prefix}{timestamp}/metrics/{cluster_id}.csv"
        results['metrics_export'] = export_elasticache_metrics_to_s3(
            elasticache_id, s3_bucket, metrics_key, start_time, end_time
        )

        ecs_metrics_key = f"{s3_prefix}{timestamp}/metrics/{cluster_id}-ecs.csv"
        results['ecs_metrics_export'] = export_ecs_metrics_to_s3(
            ecs_cluster, ecs_service, s3_bucket, ecs_metrics_key, start_time, end_time
        )

        log_exports = results['log_exports']
        log_exports['loadgen'] = export_logs_to_s3(
            loadgen_log_group,
            s3_bucket,
            f"{s3_prefix}{timestamp}/logs/{cluster_id}.txt",
            start_time,
            end_time
        )

        log_exports['container_insights'] = export_logs_to_s3(
            container_insights_log_group,
            s3_bucket,
            f"{s3_prefix}{timestamp}/logs/container-insights/{cluster_id}.txt",
            start_time,
            end_time
        )

        log_exports['elasticache'] = export_logs_to_s3(
            elasticache_log_group,
            s3_bucket,
            f"{s3_prefix}{timestamp}/logs/elasticache/{cluster_id}.txt",
            start_time,
            end_time
        )

        log_exports['lambda_shutdown_scheduler'] = export_logs_to_s3(
            lambda_scheduler_log_group,
            s3_bucket,
            f"{s3_prefix}{timestamp}/logs/lambda-shutdown-scheduler/{cluster_id}.txt",
            start_time,
            end_time
        )

        # Send email notification if configured
        try:
            notification_result = send_notification(
                results=results,
                cluster_id=cluster_id,
                elasticache_id=elasticache_id,
                s3_bucket=s3_bucket,
                s3_prefix=s3_prefix,
                timestamp=timestamp
            )
            results['notification_sent'] = notification_result
        except Exception as e:
            print(f"Notification failed (non-fatal): {e}")
            results['notification_sent'] = False

    except Exception as e:
        print(f"Error during shutdown: {e}")
        raise

    return {
        'statusCode': 200,
        'body': json.dumps(results)
    }


def send_notification(results, cluster_id, elasticache_id, s3_bucket, s3_prefix, timestamp):
    """Send email notification via SES when shutdown completes."""
    
    email = os.environ.get('NOTIFICATION_EMAIL', '')
    ses_arn = os.environ.get('SES_IDENTITY_ARN', '')
    
    if not email or not ses_arn:
        print("Email notification disabled (NOTIFICATION_EMAIL or SES_IDENTITY_ARN not set)")
        return None
    
    # Parse SES ARN: arn:aws:ses:{region}:{account}:identity/{domain-or-email}
    arn_match = re.match(r'arn:aws:ses:([^:]+):[^:]+:identity/(.+)', ses_arn)
    if not arn_match:
        print(f"Invalid SES ARN format: {ses_arn}")
        return False
    
    ses_region = arn_match.group(1)
    identity = arn_match.group(2)
    
    # If identity is an email address, use it as-is; otherwise it's a domain
    if "@" in identity:
        source_email = identity
    else:
        source_email = f"aws-elasticache-lab@{identity}"
    
    # Create SES client in the correct region
    ses = boto3.client('ses', region_name=ses_region)
    
    # Build email content
    ecs_status = (
        "OK - Stopped (0 running tasks)"
        if results.get('ecs_stopped') is True
        else f"FAILED - {results.get('ecs_stopped', 'Unknown')}"
    )
    elasticache_status = (
        "OK - Deleted"
        if results.get('elasticache_stopped') is True
        else f"FAILED - {results.get('elasticache_stopped', 'Unknown')}"
    )
    
    metrics_path = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/metrics/"
    logs_path = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/logs/"
    
    email_body = f"""ElastiCache Performance Test Complete

Cluster: {cluster_id}

=== Resource Status ===
ECS Service: {ecs_status}
ElastiCache ({elasticache_id}): {elasticache_status}

=== Exports ===
Metrics: {metrics_path}
Logs: {logs_path}

Review status above for any remaining resources.
"""
    
    response = ses.send_email(
        Source=source_email,
        Destination={'ToAddresses': [email]},
        Message={
            'Subject': {'Data': f'[ElastiCache Test Complete] {cluster_id}'},
            'Body': {'Text': {'Data': email_body}}
        }
    )
    
    print(f"Notification sent to {email}, MessageId: {response['MessageId']}")
    return True


def export_elasticache_metrics_to_s3(replication_group_id, bucket, key, start_time, end_time):
    """Export ElastiCache CloudWatch metrics to S3 as CSV."""

    sources = [
        {
            'namespace': 'AWS/ElastiCache',
            'dimensions': [{'Name': 'ReplicationGroupId', 'Value': replication_group_id}]
        }
    ]

    try:
        response = elasticache.describe_replication_groups(
            ReplicationGroupId=replication_group_id
        )
        for group in response.get('ReplicationGroups', []):
            for cluster_id in group.get('MemberClusters', []):
                sources.append({
                    'namespace': 'AWS/ElastiCache',
                    'dimensions': [{'Name': 'CacheClusterId', 'Value': cluster_id}]
                })
    except Exception as e:
        print(f"Error describing replication group {replication_group_id}: {e}")

    return export_metric_sources_to_s3(sources, bucket, key, start_time, end_time)


def export_ecs_metrics_to_s3(cluster, service, bucket, key, start_time, end_time):
    """Export ECS and Container Insights metrics to S3 as CSV."""

    sources = [
        {
            'namespace': 'AWS/ECS',
            'dimensions': [
                {'Name': 'ClusterName', 'Value': cluster},
                {'Name': 'ServiceName', 'Value': service}
            ]
        },
        {
            'namespace': 'ECS/ContainerInsights',
            'dimensions': [
                {'Name': 'ClusterName', 'Value': cluster}
            ]
        },
        {
            'namespace': 'ECS/ContainerInsights',
            'dimensions': [
                {'Name': 'ClusterName', 'Value': cluster},
                {'Name': 'ServiceName', 'Value': service}
            ]
        }
    ]

    return export_metric_sources_to_s3(sources, bucket, key, start_time, end_time)


def export_metric_sources_to_s3(sources, bucket, key, start_time, end_time):
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(['Timestamp', 'Namespace', 'MetricName', 'Stat', 'Value', 'Unit', 'Dimensions'])

    metric_map = {}
    for source in sources:
        namespace = source['namespace']
        filter_dimensions = source.get('dimensions') or []
        metric_filter = set(source.get('metric_names', [])) if source.get('metric_names') else None
        try:
            metrics = _list_metrics(namespace, filter_dimensions, metric_filter)
        except Exception as e:
            print(f"Error listing metrics for {namespace} {filter_dimensions}: {e}")
            continue

        for metric in metrics:
            metric_name = metric['MetricName']
            dimensions = metric.get('Dimensions', [])
            dims_key = tuple(sorted((d['Name'], d['Value']) for d in dimensions))
            metric_key = (namespace, metric_name, dims_key)
            metric_map[metric_key] = dimensions

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
                Statistics=STATISTICS
            )
        except Exception as e:
            print(f"Error fetching metric {namespace}/{metric_name} for {dimensions_str}: {e}")
            continue

        datapoints = sorted(
            response.get('Datapoints', []),
            key=lambda d: d['Timestamp']
        )
        for datapoint in datapoints:
            ts = datapoint['Timestamp'].isoformat()
            unit = datapoint.get('Unit', 'None')
            for stat in STATISTICS:
                if stat in datapoint:
                    writer.writerow([
                        ts,
                        namespace,
                        metric_name,
                        stat,
                        datapoint[stat],
                        unit,
                        dimensions_str
                    ])

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=csv_buffer.getvalue(),
        ContentType='text/csv'
    )

    print(f"Metrics exported to s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"


def export_logs_to_s3(log_group, bucket, key, start_time, end_time):
    """Export CloudWatch Logs to S3 as plain text (streamed)."""

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
            resp = s3.create_multipart_upload(
                Bucket=bucket,
                Key=key,
                ContentType='text/plain'
            )
            upload_id = resp['UploadId']

    def _upload_part(data):
        nonlocal part_number
        _start_multipart()
        resp = s3.upload_part(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data
        )
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
                'logGroupName': log_group,
                'startTime': start_time_ms,
                'endTime': end_time_ms,
                'interleaved': True
            }
            if next_token:
                params['nextToken'] = next_token

            response = logs.filter_log_events(**params)
            for event in response.get('events', []):
                ts = datetime.fromtimestamp(event['timestamp'] / 1000).isoformat()
                stream = event.get('logStreamName', '')
                message = event.get('message', '').rstrip('\n')
                line = f"[{ts}] [{stream}] {message}\n"
                buffer.extend(line.encode("utf-8", "replace"))
                if len(buffer) >= LOG_EXPORT_PART_SIZE:
                    _flush()

            token = response.get('nextToken')
            if not token or token == next_token:
                break
            next_token = token
    except Exception as e:
        print(f"Error fetching logs from {log_group}: {e}")
        buffer.extend(f"Error fetching logs from {log_group}: {e}\n".encode("utf-8", "replace"))

    if upload_id:
        try:
            _flush(force=True)
            s3.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts}
            )
        except Exception:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            raise
    else:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=bytes(buffer),
            ContentType='text/plain'
        )

    print(f"Logs exported to s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"
