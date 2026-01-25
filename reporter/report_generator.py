import boto3
import os
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import re
from io import StringIO
import sys

def download_s3_file(bucket, key):
    s3 = boto3.client('s3')
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj['Body'].read().decode('utf-8')

def parse_memtier_logs(log_content):
    data = []
    # Pattern to extract timestamp and memtier output
    # Log format: [ISO_TIMESTAMP] [stream] message
    # Memtier output example: [RUN #1] ... Ops/sec: 1234.5 ... Latency: 1.23 ms

    # We are looking for lines that contain "Ops/sec" and "Latency"
    # The cloudwatch export format is "[timestamp] [stream] message"

    lines = log_content.split('\n')
    for line in lines:
        if "Ops/sec" in line and "Latency" in line:
            # Extract timestamp from the log line
            ts_match = re.search(r'^\[([\d\-T:\.]+)\]', line)
            if not ts_match:
                continue
            timestamp = ts_match.group(1)

            # Extract Ops/sec
            ops_match = re.search(r'Ops/sec:\s*([\d\.]+)', line)
            if ops_match:
                ops_sec = float(ops_match.group(1))
            else:
                ops_sec = None

            # Extract Latency (ms)
            lat_match = re.search(r'Latency:\s*([\d\.]+)\s*ms', line)
            if lat_match:
                latency = float(lat_match.group(1))
            else:
                latency = None

            if ops_sec is not None and latency is not None:
                data.append({
                    'Timestamp': timestamp,
                    'Ops/sec': ops_sec,
                    'Latency (ms)': latency
                })

    df = pd.DataFrame(data)
    if not df.empty:
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    return df

def parse_metrics_csv(csv_content):
    # CSV Header: Timestamp,Namespace,MetricName,Stat,Value,Unit,Dimensions
    df = pd.read_csv(StringIO(csv_content))

    required_columns = [
        "Timestamp",
        "Namespace",
        "MetricName",
        "Stat",
        "Value",
        "Unit",
        "Dimensions",
    ]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(
            f"Metrics CSV is missing required columns: {', '.join(missing_columns)}"
        )
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    return df

def create_report(metrics_df, logs_df, cluster_id, suffix):
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        subplot_titles=("Load (Ops/sec)", "Latency (ms)", "CPU Utilization (%)", "Cache Hit Rate (%)")
    )

    # 1. Load (Ops/sec) from Logs
    if not logs_df.empty:
        fig.add_trace(
            go.Scatter(x=logs_df['Timestamp'], y=logs_df['Ops/sec'], name="Ops/sec", mode='lines'),
            row=1, col=1
        )
        fig.add_trace(
            go.Scatter(x=logs_df['Timestamp'], y=logs_df['Latency (ms)'], name="Latency", mode='lines'),
            row=2, col=1
        )
    else:
        fig.add_annotation(text="No Memtier Log Data Found", xref="x1", yref="y1", showarrow=False, row=1, col=1)
        fig.add_annotation(text="No Memtier Log Data Found", xref="x2", yref="y2", showarrow=False, row=2, col=1)

    # 2. Metrics from CloudWatch
    if not metrics_df.empty:
        # CPU Utilization
        cpu_df = metrics_df[metrics_df['MetricName'] == 'CPUUtilization']
        if not cpu_df.empty:
            # Group by Dimension (Node) if multiple nodes
            for dim, group in cpu_df.groupby('Dimensions'):
                fig.add_trace(
                    go.Scatter(x=group['Timestamp'], y=group['Value'], name=f"CPU - {dim}", mode='lines'),
                    row=3, col=1
                )
        else:
             fig.add_annotation(text="No CPU Metrics Found", xref="x3", yref="y3", showarrow=False, row=3, col=1)

        # Cache Hit Rate
        hits_df = metrics_df[metrics_df['MetricName'] == 'CacheHitRate']
        if not hits_df.empty:
            for dim, group in hits_df.groupby('Dimensions'):
                fig.add_trace(
                    go.Scatter(x=group['Timestamp'], y=group['Value'], name=f"Hit Rate - {dim}", mode='lines'),
                    row=4, col=1
                )
        else:
            # Try deriving it? Or just show message.
            fig.add_annotation(text="No CacheHitRate Metrics Found", xref="x4", yref="y4", showarrow=False, row=4, col=1)
    else:
        fig.add_annotation(text="No CloudWatch Metrics Data Found", xref="x3", yref="y3", showarrow=False, row=3, col=1)
        fig.add_annotation(text="No CloudWatch Metrics Data Found", xref="x4", yref="y4", showarrow=False, row=4, col=1)

    fig.update_layout(
        height=1200,
        title_text=f"ElastiCache Performance Report - {cluster_id} ({suffix})",
        showlegend=True
    )

    return fig.to_html(include_plotlyjs='cdn')

def main():
    try:
        metrics_s3_path = os.environ.get('METRICS_CSV')
        logs_s3_path = os.environ.get('LOGS_TXT')
        output_bucket = os.environ['OUTPUT_BUCKET']
        output_prefix = os.environ.get('OUTPUT_PREFIX', '')
        suffix = os.environ.get('SUFFIX', 'report')
        cluster_id = os.environ.get('CLUSTER_ID', 'Unknown')

        print(f"Starting report generation for {cluster_id}")

        # Parse S3 URLs (s3://bucket/key)
        metrics_bucket, metrics_key = metrics_s3_path.replace("s3://", "").split("/", 1)
        logs_bucket, logs_key = logs_s3_path.replace("s3://", "").split("/", 1)

        print(f"Downloading metrics from {metrics_bucket}/{metrics_key}")
        metrics_content = download_s3_file(metrics_bucket, metrics_key)
        metrics_df = parse_metrics_csv(metrics_content)

        print(f"Downloading logs from {logs_bucket}/{logs_key}")
        logs_content = download_s3_file(logs_bucket, logs_key)
        logs_df = parse_memtier_logs(logs_content)

        print("Generating report...")
        html_content = create_report(metrics_df, logs_df, cluster_id, suffix)

        output_key = f"{output_prefix}results_{suffix}.html"
        print(f"Uploading report to s3://{output_bucket}/{output_key}")

        s3 = boto3.client('s3')
        s3.put_object(
            Bucket=output_bucket,
            Key=output_key,
            Body=html_content,
            ContentType='text/html'
        )

        print("Report generation complete.")

    except Exception as e:
        print(f"Error generating report: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
