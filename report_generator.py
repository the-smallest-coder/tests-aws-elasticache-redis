import boto3
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
import io
import sys

def get_env_var(name):
    val = os.environ.get(name)
    if not val:
        print(f"Error: Environment variable {name} not set.")
        sys.exit(1)
    return val

def read_csv_from_s3(s3_client, bucket, key):
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        return pd.read_csv(obj['Body'])
    except Exception as e:
        print(f"Error reading {key} from {bucket}: {e}")
        return None

def parse_dimensions(dim_str):
    """
    Parses 'Name=CacheClusterId;Value=abc-001' into a dict.
    Returns a string representation for grouping (e.g., 'Node: abc-001').
    """
    if not isinstance(dim_str, str):
        return "Unknown"

    dims = {}
    try:
        parts = dim_str.split(';')
        for part in parts:
            if '=' in part:
                k, v = part.split('=', 1)
                dims[k] = v
    except:
        pass

    if 'CacheClusterId' in dims:
        return f"Node: {dims['CacheClusterId']}"
    elif 'ReplicationGroupId' in dims:
        return f"Cluster: {dims['ReplicationGroupId']}"
    elif 'ServiceName' in dims:
        return f"Service: {dims['ServiceName']}"
    elif 'ClusterName' in dims:
        return f"ECS Cluster: {dims['ClusterName']}"
    return "Global"

def main():
    # 1. Configuration
    BUCKET = get_env_var("S3_BUCKET")
    PREFIX = get_env_var("S3_PREFIX") # e.g., "exports/"
    TIMESTAMP = get_env_var("REPORT_TIMESTAMP") # e.g., "20231027-100000"
    CLUSTER_ID = get_env_var("CLUSTER_ID")

    # 2. Initialize S3
    s3 = boto3.client('s3')

    # 3. Define paths
    metrics_key = f"{PREFIX}{TIMESTAMP}/metrics/{CLUSTER_ID}.csv"
    ecs_metrics_key = f"{PREFIX}{TIMESTAMP}/metrics/{CLUSTER_ID}-ecs.csv"

    print(f"Reading metrics from s3://{BUCKET}/{metrics_key}")
    df_ec = read_csv_from_s3(s3, BUCKET, metrics_key)

    print(f"Reading ECS metrics from s3://{BUCKET}/{ecs_metrics_key}")
    df_ecs = read_csv_from_s3(s3, BUCKET, ecs_metrics_key)

    if df_ec is None and df_ecs is None:
        print("No data found. Exiting.")
        sys.exit(1)

    # 4. Process Data
    # Merge if both exist, or use whichever exists
    frames = []
    if df_ec is not None: frames.append(df_ec)
    if df_ecs is not None: frames.append(df_ecs)

    if not frames:
        print("No data frames to process.")
        sys.exit(1)

    df = pd.concat(frames)

    # Ensure timestamp is datetime
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df.sort_values('Timestamp', inplace=True)

    # Extract readable source from Dimensions
    df['Source'] = df['Dimensions'].apply(parse_dimensions)

    # 5. Create Visualization
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.1,
        subplot_titles=("Throughput (Ops/sec)", "Server CPU Utilization (%)", "Load Generator CPU (%)")
    )

    # --- Plot 1: Throughput (CmdSet + CmdGet) ---
    # Filter: Namespace=AWS/ElastiCache, Stat=Sum, MetricName in [CmdSet, CmdGet]
    throughput_mask = (
        (df['Namespace'] == 'AWS/ElastiCache') &
        (df['Stat'] == 'Sum') &
        (df['MetricName'].isin(['CmdSet', 'CmdGet']))
    )
    df_throughput = df[throughput_mask].copy()

    # We want to aggregate by Timestamp and Source (Cluster vs Node)
    # Ideally, we look at Cluster level if available, else sum of nodes
    # For simplicity, we plot each Source's line.

    for source in df_throughput['Source'].unique():
        # Prefer Cluster level for total throughput
        if 'Cluster:' in source or 'Node:' in source:
            subset = df_throughput[df_throughput['Source'] == source]
            # Pivot to sum CmdGet and CmdSet per timestamp
            pivoted = subset.pivot_table(index='Timestamp', columns='MetricName', values='Value', aggfunc='sum').fillna(0)
            if 'CmdGet' in pivoted.columns and 'CmdSet' in pivoted.columns:
                pivoted['Total'] = pivoted['CmdGet'] + pivoted['CmdSet']
                # Convert sum/minute to ops/sec (divide by 60)
                pivoted['OpsSec'] = pivoted['Total'] / 60.0

                fig.add_trace(
                    go.Scatter(x=pivoted.index, y=pivoted['OpsSec'], mode='lines', name=f"{source} (Ops/sec)"),
                    row=1, col=1
                )

    # --- Plot 2: Server CPU ---
    # Filter: Namespace=AWS/ElastiCache, Stat=Average, MetricName=EngineCPUUtilization (or CPUUtilization)
    cpu_mask = (
        (df['Namespace'] == 'AWS/ElastiCache') &
        (df['Stat'] == 'Average') &
        (df['MetricName'].isin(['EngineCPUUtilization', 'CPUUtilization']))
    )
    df_cpu = df[cpu_mask]

    for source in df_cpu['Source'].unique():
        subset = df_cpu[df_cpu['Source'] == source]
        for metric in subset['MetricName'].unique():
            metric_data = subset[subset['MetricName'] == metric]
            fig.add_trace(
                go.Scatter(x=metric_data['Timestamp'], y=metric_data['Value'], mode='lines', name=f"{source} {metric}"),
                row=2, col=1
            )

    # --- Plot 3: Load Generator Health ---
    # Filter: Namespace=AWS/ECS, Stat=Average, MetricName=CpuUtilized
    # Note: CpuUtilized in ECS is often sum of units? Or percent?
    # ContainerInsights: CpuUtilized is units. CpuReserved is units.
    # If we have basic ECS metrics, 'CPUUtilization' is percent.
    ecs_cpu_mask = (
        (df['Namespace'].str.contains('ECS')) &
        (df['Stat'] == 'Average') &
        (df['MetricName'].isin(['CpuUtilized', 'CPUUtilization']))
    )
    df_ecs_cpu = df[ecs_cpu_mask]

    for source in df_ecs_cpu['Source'].unique():
        subset = df_ecs_cpu[df_ecs_cpu['Source'] == source]
        fig.add_trace(
            go.Scatter(x=subset['Timestamp'], y=subset['Value'], mode='lines', name=f"{source} CPU"),
            row=3, col=1
        )

    # Layout updates
    fig.update_layout(height=1200, title_text=f"Performance Report: {CLUSTER_ID}")

    # 6. Generate HTML
    html_content = fig.to_html(full_html=True, include_plotlyjs='cdn')

    # 7. Upload to S3
    output_key = f"{PREFIX}{TIMESTAMP}/results_{TIMESTAMP}.html"
    print(f"Uploading report to s3://{BUCKET}/{output_key}")

    s3.put_object(
        Bucket=BUCKET,
        Key=output_key,
        Body=html_content,
        ContentType='text/html'
    )
    print("Done.")

if __name__ == "__main__":
    main()
