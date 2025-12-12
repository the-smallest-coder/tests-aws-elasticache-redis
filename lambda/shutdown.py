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


def handler(event, context):
    """
    Lambda handler for shutdown orchestration:
    1. Export CloudWatch metrics to S3 (CSV)
    2. Export memtier logs to S3 (plain text)
    3. Scale ECS service to 0
    4. Stop ElastiCache replication group
    """
    
    # Get configuration from environment
    cluster_id = os.environ['CLUSTER_ID']
    ecs_cluster = os.environ['ECS_CLUSTER']
    ecs_service = os.environ['ECS_SERVICE']
    elasticache_id = os.environ['ELASTICACHE_ID']
    s3_bucket = os.environ['S3_BUCKET']
    s3_prefix = os.environ.get('S3_PREFIX', 'exports/')
    log_group = os.environ['LOG_GROUP']
    aws_region = os.environ['AWS_REGION']
    
    timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    
    results = {
        'metrics_export': None,
        'logs_export': None,
        'ecs_stopped': False,
        'elasticache_stopped': False
    }
    
    try:
        # 1. Export CloudWatch metrics to CSV
        metrics_key = f"{s3_prefix}metrics/{cluster_id}-{timestamp}.csv"
        results['metrics_export'] = export_metrics_to_s3(
            elasticache_id, s3_bucket, metrics_key, aws_region
        )
        
        # 2. Export memtier logs to plain text
        logs_key = f"{s3_prefix}logs/{cluster_id}-{timestamp}.txt"
        results['logs_export'] = export_logs_to_s3(
            log_group, s3_bucket, logs_key
        )
        
        # 3. Scale ECS service to 0
        ecs.update_service(
            cluster=ecs_cluster,
            service=ecs_service,
            desiredCount=0
        )
        results['ecs_stopped'] = True
        print(f"ECS service {ecs_service} scaled to 0")
        
        # 4. Stop ElastiCache replication group
        try:
            elasticache.modify_replication_group(
                ReplicationGroupId=elasticache_id,
                ApplyImmediately=True
            )
            # Note: ElastiCache doesn't have a "stop" - we just leave it
            # User will terraform destroy to fully remove
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


def export_metrics_to_s3(elasticache_id, bucket, key, region):
    """Export ElastiCache CloudWatch metrics to S3 as CSV"""
    
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=2)  # Get last 2 hours of metrics
    
    metrics_to_export = [
        'CPUUtilization',
        'NetworkBytesIn',
        'NetworkBytesOut',
        'CurrConnections',
        'CacheHits',
        'CacheMisses',
        'GetTypeCmds',
        'SetTypeCmds',
        'DatabaseMemoryUsagePercentage',
        'Evictions'
    ]
    
    # CSV buffer
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(['Timestamp', 'MetricName', 'Value', 'Unit'])
    
    for metric_name in metrics_to_export:
        try:
            response = cloudwatch.get_metric_statistics(
                Namespace='AWS/ElastiCache',
                MetricName=metric_name,
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=['Average', 'Sum', 'Maximum']
            )
            
            for datapoint in response.get('Datapoints', []):
                ts = datapoint['Timestamp'].isoformat()
                value = datapoint.get('Average') or datapoint.get('Sum') or datapoint.get('Maximum', 0)
                unit = datapoint.get('Unit', 'None')
                writer.writerow([ts, metric_name, value, unit])
                
        except Exception as e:
            print(f"Error fetching metric {metric_name}: {e}")
    
    # Upload to S3
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=csv_buffer.getvalue(),
        ContentType='text/csv'
    )
    
    print(f"Metrics exported to s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"


def export_logs_to_s3(log_group, bucket, key):
    """Export CloudWatch Logs to S3 as plain text"""
    
    end_time = int(datetime.utcnow().timestamp() * 1000)
    start_time = end_time - (2 * 60 * 60 * 1000)  # Last 2 hours
    
    logs_buffer = io.StringIO()
    
    try:
        # Get log streams
        streams_response = logs.describe_log_streams(
            logGroupName=log_group,
            orderBy='LastEventTime',
            descending=True,
            limit=50
        )
        
        for stream in streams_response.get('logStreams', []):
            stream_name = stream['logStreamName']
            logs_buffer.write(f"\n=== Stream: {stream_name} ===\n")
            
            # Get log events
            events_response = logs.get_log_events(
                logGroupName=log_group,
                logStreamName=stream_name,
                startTime=start_time,
                endTime=end_time
            )
            
            for event in events_response.get('events', []):
                ts = datetime.fromtimestamp(event['timestamp'] / 1000).isoformat()
                message = event['message']
                logs_buffer.write(f"[{ts}] {message}\n")
                
    except Exception as e:
        print(f"Error fetching logs: {e}")
        logs_buffer.write(f"Error fetching logs: {e}\n")
    
    # Upload to S3
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=logs_buffer.getvalue(),
        ContentType='text/plain'
    )
    
    print(f"Logs exported to s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"
