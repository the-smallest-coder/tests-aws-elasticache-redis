import boto3
import csv
import io
import json
import os
from datetime import datetime, timedelta

# Initialize clients
ecs = boto3.client('ecs')
elasticache = boto3.client('elasticache')
cloudwatch = boto3.client('cloudwatch')
logs = boto3.client('logs')
s3 = boto3.client('s3')

STATISTICS = ['Average', 'Sum', 'Maximum', 'Minimum']
EXPORT_BUFFER_MINUTES = 5


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
    1. Export CloudWatch metrics to S3 (CSV)
    2. Export CloudWatch Logs to S3 (text)
    3. Scale ECS service to 0
    4. Stop ElastiCache replication group
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

        log_exports['lambda_shutdown'] = export_logs_to_s3(
            lambda_shutdown_log_group,
            s3_bucket,
            f"{s3_prefix}{timestamp}/logs/lambda-shutdown/{cluster_id}.txt",
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

        ecs.update_service(
            cluster=ecs_cluster,
            service=ecs_service,
            desiredCount=0
        )
        results['ecs_stopped'] = True
        print(f"ECS service {ecs_service} scaled to 0")

        try:
            elasticache.modify_replication_group(
                ReplicationGroupId=elasticache_id,
                ApplyImmediately=True
            )
            results['elasticache_stopped'] = True
            print(f"ElastiCache {elasticache_id} modification initiated")
        except Exception as e:
            print(f"ElastiCache stop note: {e}")
            results['elasticache_stopped'] = str(e)

    except Exception as e:
        print(f"Error during shutdown: {e}")
        raise

    return {
        'statusCode': 200,
        'body': json.dumps(results)
    }


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
    """Export CloudWatch Logs to S3 as plain text."""

    if not log_group:
        return None

    start_time_ms = int(start_time.timestamp() * 1000)
    end_time_ms = int(end_time.timestamp() * 1000)

    logs_buffer = io.StringIO()
    logs_buffer.write(f"LogGroup: {log_group}\n")

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
                logs_buffer.write(f"[{ts}] [{stream}] {message}\n")

            token = response.get('nextToken')
            if not token or token == next_token:
                break
            next_token = token
    except Exception as e:
        print(f"Error fetching logs from {log_group}: {e}")
        logs_buffer.write(f"Error fetching logs from {log_group}: {e}\n")

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=logs_buffer.getvalue(),
        ContentType='text/plain'
    )

    print(f"Logs exported to s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"
