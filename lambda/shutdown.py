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
    # Use the folder name that Terraform fixed at apply time so that
    # cluster_details.json (written by Terraform) and metrics/logs (written
    # here) land in the same S3 folder.  Fall back to a generated timestamp
    # only if the variable is absent (e.g. manual Lambda invocation).
    run_folder = os.environ.get('RUN_FOLDER') or end_time.strftime('%Y%m%d-%H%M%S')
    timestamp = run_folder

    results = {
        'metrics_export': None,
        'ecs_metrics_export': None,
        'log_exports': {},
        'ecs_stopped': False,
        'elasticache_stopped': False
    }

    # Describe replication group NOW, before we initiate deletion, so we have
    # the full member cluster list available for metric export regardless of
    # whether describe_replication_groups races with the delete call later.
    member_clusters = []
    try:
        rg_response = elasticache.describe_replication_groups(
            ReplicationGroupId=elasticache_id
        )
        for group in rg_response.get('ReplicationGroups', []):
            member_clusters = group.get('MemberClusters', [])
        print(f"Pre-shutdown: found member clusters {member_clusters}")
    except Exception as e:
        print(f"Pre-shutdown describe failed (will retry inside export): {e}")

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
            elasticache_id, s3_bucket, metrics_key, start_time, end_time,
            member_clusters=member_clusters
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

        # Run reporter task
        try:
            reporter_result = run_reporter_task(
                cluster_id=cluster_id,
                ecs_cluster=ecs_cluster,
                metrics_key=f"s3://{s3_bucket}/{metrics_key}",
                ecs_metrics_key=f"s3://{s3_bucket}/{ecs_metrics_key}",
                logs_key=log_exports['loadgen'],
                s3_bucket=s3_bucket,
                s3_prefix=s3_prefix,
                timestamp=timestamp
            )
            results['reporter_task'] = reporter_result
        except Exception as e:
            print(f"Reporter task launch failed: {e}")
            results['reporter_task'] = str(e)

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
    """Send HTML email notification via SES when shutdown completes."""
    
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
    ecs_ok = results.get('ecs_stopped') is True
    ec_ok = results.get('elasticache_stopped') is True

    ecs_status_text = "Stopped (0 running tasks)" if ecs_ok else f"Issue - {results.get('ecs_stopped', 'Unknown')}"
    elasticache_status_text = "Delete initiated" if ec_ok else f"Issue - {results.get('elasticache_stopped', 'Unknown')}"

    ecs_icon = "&#9989;" if ecs_ok else "&#9888;&#65039;"
    ec_icon = "&#9989;" if ec_ok else "&#9888;&#65039;"
    ecs_color = "#1e8e3e" if ecs_ok else "#e37400"
    ec_color = "#1e8e3e" if ec_ok else "#e37400"

    metrics_exported = results.get('metrics_export') is not None
    ecs_metrics_exported = results.get('ecs_metrics_export') is not None
    logs_exported = any(v for v in results.get('log_exports', {}).values() if v)
    reporter_launched = results.get('reporter_task') is not None and not isinstance(results.get('reporter_task'), str)

    metrics_path = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/metrics/"
    logs_path = f"s3://{s3_bucket}/{s3_prefix}{timestamp}/logs/"
    aws_region = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', ''))

    # Determine overall status
    all_ok = ecs_ok and ec_ok
    header_bg = "linear-gradient(135deg,#1e8e3e,#137333)" if all_ok else "linear-gradient(135deg,#e37400,#c56200)"
    header_title = "&#9989; Shutdown Complete" if all_ok else "&#9888;&#65039; Shutdown Complete (with issues)"

    email_body_text = f"""ElastiCache Performance Test Complete

Cluster: {cluster_id}

=== Resource Status ===
ECS Service: {ecs_status_text}
ElastiCache ({elasticache_id}): {elasticache_status_text}

=== Exports ===
Metrics: {metrics_path}
Logs: {logs_path}

Review status above for any remaining resources.
"""

    email_body_html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f4f6f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f9;padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:{header_bg};padding:30px 40px;">
            <span style="font-size:14px;color:rgba(255,255,255,0.85);text-transform:uppercase;letter-spacing:1px;">Performance Test</span>
            <h1 style="margin:6px 0 0;font-size:24px;color:#ffffff;font-weight:600;">{header_title}</h1>
          </td>
        </tr>

        <!-- Cluster ID bar -->
        <tr>
          <td style="background-color:#e8f0fe;padding:14px 40px;border-bottom:1px solid #d2e3fc;">
            <span style="font-size:13px;color:#5f6368;">Cluster</span><br>
            <span style="font-size:16px;color:#1a73e8;font-weight:600;font-family:monospace;">{cluster_id}</span>
          </td>
        </tr>

        <!-- Resource Status -->
        <tr>
          <td style="padding:28px 40px 10px;">
            <h2 style="margin:0 0 16px;font-size:15px;color:#5f6368;text-transform:uppercase;letter-spacing:0.5px;">Resource Status</h2>
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e8eaed;border-radius:6px;overflow:hidden;">
              <tr style="background-color:#f8f9fa;">
                <td style="padding:10px 16px;font-size:13px;color:#5f6368;border-bottom:1px solid #e8eaed;">Resource</td>
                <td style="padding:10px 16px;font-size:13px;color:#5f6368;border-bottom:1px solid #e8eaed;">Status</td>
              </tr>
              <tr>
                <td style="padding:10px 16px;font-size:14px;color:#202124;border-bottom:1px solid #e8eaed;">ECS Service</td>
                <td style="padding:10px 16px;font-size:14px;color:{ecs_color};font-weight:500;border-bottom:1px solid #e8eaed;">{ecs_icon} {ecs_status_text}</td>
              </tr>
              <tr>
                <td style="padding:10px 16px;font-size:14px;color:#202124;">ElastiCache ({elasticache_id})</td>
                <td style="padding:10px 16px;font-size:14px;color:{ec_color};font-weight:500;">{ec_icon} {elasticache_status_text}</td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Export Summary -->
        <tr>
          <td style="padding:20px 40px;">
            <h2 style="margin:0 0 16px;font-size:15px;color:#5f6368;text-transform:uppercase;letter-spacing:0.5px;">Data Exports</h2>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">CloudWatch Metrics</span><br>
                  <span style="font-size:14px;color:{'#1e8e3e' if metrics_exported else '#d93025'};font-weight:500;">{'&#9989; Exported' if metrics_exported else '&#10060; Failed'}</span>
                </td>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">ECS Metrics</span><br>
                  <span style="font-size:14px;color:{'#1e8e3e' if ecs_metrics_exported else '#d93025'};font-weight:500;">{'&#9989; Exported' if ecs_metrics_exported else '&#10060; Failed'}</span>
                </td>
              </tr>
              <tr>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">CloudWatch Logs</span><br>
                  <span style="font-size:14px;color:{'#1e8e3e' if logs_exported else '#d93025'};font-weight:500;">{'&#9989; Exported' if logs_exported else '&#10060; Failed'}</span>
                </td>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">HTML Report</span><br>
                  <span style="font-size:14px;color:{'#1e8e3e' if reporter_launched else '#80868b'};font-weight:500;">{'&#9989; Generating' if reporter_launched else '&#8212; Not launched'}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- S3 Paths -->
        <tr>
          <td style="padding:10px 40px 28px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f3f4;border-radius:6px;">
              <tr>
                <td style="padding:14px 20px;">
                  <span style="font-size:12px;color:#80868b;">Metrics Location</span><br>
                  <span style="font-size:13px;color:#202124;font-family:monospace;">{metrics_path}</span><br><br>
                  <span style="font-size:12px;color:#80868b;">Logs Location</span><br>
                  <span style="font-size:13px;color:#202124;font-family:monospace;">{logs_path}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background-color:#f8f9fa;padding:20px 40px;border-top:1px solid #e8eaed;">
            <p style="margin:0;font-size:12px;color:#80868b;text-align:center;">
              Automated notification from ElastiCache Performance Lab&nbsp;&nbsp;&#8226;&nbsp;&nbsp;{aws_region}
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    
    response = ses.send_email(
        Source=source_email,
        Destination={'ToAddresses': [email]},
        Message={
            'Subject': {'Data': f'[ElastiCache Test Complete] {cluster_id}', 'Charset': 'UTF-8'},
            'Body': {
                'Text': {'Data': email_body_text, 'Charset': 'UTF-8'},
                'Html': {'Data': email_body_html, 'Charset': 'UTF-8'}
            }
        }
    )
    
    print(f"Notification sent to {email}, MessageId: {response['MessageId']}")
    return True


def export_elasticache_metrics_to_s3(replication_group_id, bucket, key, start_time, end_time,
                                      member_clusters=None):
    """Export all ElastiCache CloudWatch metrics to S3 as CSV.

    Covers three dimension granularities published by ElastiCache:
      1. ReplicationGroupId only          (e.g. DatabaseMemoryUsage*)
      2. ReplicationGroupId + NodeGroupId (sharded variants)
      3. CacheClusterId only              (e.g. EngineCPUUtilization)
      4. CacheClusterId + NodeGroupId     (node-level variants)
    """

    # -- Replication-group level sources --
    sources = [
        {
            'namespace': 'AWS/ElastiCache',
            'dimensions': [{'Name': 'ReplicationGroupId', 'Value': replication_group_id}]
        }
    ]

    # -- Cluster-level sources; use pre-fetched list when available --
    if not member_clusters:
        try:
            response = elasticache.describe_replication_groups(
                ReplicationGroupId=replication_group_id
            )
            for group in response.get('ReplicationGroups', []):
                member_clusters = group.get('MemberClusters', [])
        except Exception as e:
            print(f"Error describing replication group {replication_group_id}: {e}")
            member_clusters = []

    for cluster_id in member_clusters:
        # CacheClusterId only — catches single-dim metrics (EngineCPUUtilization, etc.)
        sources.append({
            'namespace': 'AWS/ElastiCache',
            'dimensions': [{'Name': 'CacheClusterId', 'Value': cluster_id}]
        })

    print(f"ElastiCache metric sources: {[s['dimensions'] for s in sources]}")
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


def run_reporter_task(cluster_id, ecs_cluster, metrics_key, ecs_metrics_key, logs_key, s3_bucket, s3_prefix, timestamp):
    """Launch ECS task to generate HTML report."""

    task_definition = os.environ.get('REPORTER_TASK_DEFINITION')
    if not task_definition:
        print("REPORTER_TASK_DEFINITION not set, skipping report generation.")
        return None

    if not metrics_key or not logs_key:
        print("Missing metrics or logs key, skipping report generation.")
        return None

    # Get network configuration from the loadgen service to reuse subnets/security groups
    # This assumes the loadgen service still exists (even if scaled to 0) which matches our flow
    try:
        service_desc = ecs.describe_services(
            cluster=ecs_cluster,
            services=[os.environ['ECS_SERVICE']]
        )
        network_config = service_desc['services'][0]['networkConfiguration']
    except Exception as e:
        print(f"Failed to get network config from service, using defaults: {e}")
        # Fallback or fail? We need subnets to run Fargate.
        # If we can't get them, we probably can't run the task.
        raise e

    # S3 keys passed to this function might be full s3:// paths returned by export functions
    # or just keys. The export functions return "s3://bucket/key".
    # The reporter script expects s3:// paths for input.

    # metrics_key is like "s3://bucket/prefix/timestamp/metrics/cluster.csv"
    # logs_key is like "s3://bucket/prefix/timestamp/logs/cluster.txt"

    suffix = timestamp  # Use timestamp as suffix for the report file

    response = ecs.run_task(
        cluster=ecs_cluster,
        taskDefinition=task_definition,
        launchType='FARGATE',
        networkConfiguration=network_config,
        overrides={
            'containerOverrides': [
                {
                    'name': 'reporter',
                    'environment': [
                        {'name': 'METRICS_CSV', 'value': metrics_key},
                        {'name': 'ECS_METRICS_CSV', 'value': ecs_metrics_key or ''},
                        {'name': 'LOGS_TXT', 'value': logs_key},
                        {'name': 'OUTPUT_BUCKET', 'value': s3_bucket},
                        {'name': 'OUTPUT_PREFIX', 'value': s3_prefix},
                        {'name': 'SUFFIX', 'value': suffix},
                        {'name': 'S3_BUCKET', 'value': s3_bucket},
                        {'name': 'S3_PREFIX', 'value': s3_prefix},
                        {'name': 'REPORT_TIMESTAMP', 'value': timestamp},
                        {'name': 'CLUSTER_ID', 'value': cluster_id},
                        {'name': 'CLUSTER_MODE', 'value': os.environ.get('CLUSTER_MODE', 'false')}
                    ]
                }
            ]
        },
        count=1,
        startedBy='ShutdownLambda'
    )

    tasks = response.get('tasks', [])
    if not tasks:
        print(f"run_task did not return any tasks. Response failures: {response.get('failures')}")
        return None

    task_arn = tasks[0].get('taskArn')
    print(f"Reporter task launched: {task_arn}")
    return task_arn


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
