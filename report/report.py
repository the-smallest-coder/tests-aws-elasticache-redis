import os
import boto3
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import io

def main():
    s3_bucket = os.environ['S3_BUCKET']
    s3_prefix = os.environ.get('S3_PREFIX', 'exports/')
    cluster_id = os.environ['CLUSTER_ID']
    timestamp = os.environ['TIMESTAMP']

    s3 = boto3.client('s3')

    # Paths
    # Note: s3_prefix usually ends with / if it's "exports/"
    # shutdown.py uses: f"{s3_prefix}{timestamp}/metrics/{cluster_id}.csv"

    base_prefix = f"{s3_prefix}{timestamp}"
    metrics_key = f"{base_prefix}/metrics/{cluster_id}.csv"
    ecs_metrics_key = f"{base_prefix}/metrics/{cluster_id}-ecs.csv"

    print(f"Downloading metrics from s3://{s3_bucket}/{metrics_key}")

    # Download Metrics
    try:
        obj = s3.get_object(Bucket=s3_bucket, Key=metrics_key)
        df_metrics = pd.read_csv(obj['Body'])
        df_metrics['Timestamp'] = pd.to_datetime(df_metrics['Timestamp'])
    except Exception as e:
        print(f"Error reading metrics: {e}")
        df_metrics = pd.DataFrame()

    try:
        obj = s3.get_object(Bucket=s3_bucket, Key=ecs_metrics_key)
        df_ecs = pd.read_csv(obj['Body'])
        df_ecs['Timestamp'] = pd.to_datetime(df_ecs['Timestamp'])
    except Exception as e:
        print(f"Error reading ECS metrics: {e}")
        df_ecs = pd.DataFrame()

    # Generate Report
    fig = make_subplots(
        rows=4, cols=1,
        subplot_titles=("Generated Load (Commands/sec)", "ElastiCache CPU & Network", "ECS Load Generator CPU", "ElastiCache Hits/Misses"),
        specs=[[{"secondary_y": False}], [{"secondary_y": True}], [{"secondary_y": False}], [{"secondary_y": False}]]
    )

    # 1. Generated Load (CmdGet + CmdSet)
    if not df_metrics.empty:
        # Sum of CmdGet and CmdSet per minute (Stat=Sum) divided by 60 to get ops/sec
        # Or just plot Sum per minute. Let's plot Sum (Count).
        # Actually, user wants "Generated Load". Ops/sec is better.
        # But 'Sum' is count per 60s period (if period is 60s).

        cmds = df_metrics[df_metrics['MetricName'].isin(['CmdGet', 'CmdSet']) & (df_metrics['Stat'] == 'Sum')]

        for name, group in cmds.groupby('Dimensions'):
            # Aggregate if multiple metrics (Get+Set) for same dimension?
            # Or just plot them.
            # Let's plot total commands per dimension.

            # Pivot to sum CmdGet + CmdSet
            pivoted = group.pivot_table(index='Timestamp', columns='MetricName', values='Value', aggfunc='sum').fillna(0)
            if 'CmdGet' in pivoted.columns and 'CmdSet' in pivoted.columns:
                pivoted['Total'] = pivoted['CmdGet'] + pivoted['CmdSet']
            elif 'CmdGet' in pivoted.columns:
                pivoted['Total'] = pivoted['CmdGet']
            elif 'CmdSet' in pivoted.columns:
                pivoted['Total'] = pivoted['CmdSet']
            else:
                pivoted['Total'] = 0

            # Convert to Ops/Sec (assuming 60s period)
            pivoted['OpsSec'] = pivoted['Total'] / 60.0

            fig.add_trace(go.Scatter(x=pivoted.index, y=pivoted['OpsSec'], name=f'Ops/Sec {name}'), row=1, col=1)

    # 2. ElastiCache CPU & Network
    if not df_metrics.empty:
        # Filter for CPUUtilization
        cpu = df_metrics[(df_metrics['MetricName'] == 'CPUUtilization') & (df_metrics['Stat'] == 'Average')]

        for name, group in cpu.groupby('Dimensions'):
            fig.add_trace(go.Scatter(x=group['Timestamp'], y=group['Value'], name=f'CPU {name}'), row=2, col=1)

        # Filter for NetworkBytesIn
        net_in = df_metrics[(df_metrics['MetricName'] == 'NetworkBytesIn') & (df_metrics['Stat'] == 'Average')]
        for name, group in net_in.groupby('Dimensions'):
            fig.add_trace(go.Scatter(x=group['Timestamp'], y=group['Value'], name=f'NetIn {name}', opacity=0.5), row=2, col=1, secondary_y=True)

    # 3. ECS CPU
    if not df_ecs.empty:
        cpu = df_ecs[(df_ecs['MetricName'] == 'CpuUtilized') & (df_ecs['Stat'] == 'Average')]
        for name, group in cpu.groupby('Dimensions'):
            fig.add_trace(go.Scatter(x=group['Timestamp'], y=group['Value'], name=f'ECS CPU {name}'), row=3, col=1)

    # 4. Cache Hits/Misses
    if not df_metrics.empty:
        hits = df_metrics[(df_metrics['MetricName'] == 'CacheHits') & (df_metrics['Stat'] == 'Sum')]
        for name, group in hits.groupby('Dimensions'):
            fig.add_trace(go.Scatter(x=group['Timestamp'], y=group['Value'], name=f'Hits {name}'), row=4, col=1)

        misses = df_metrics[(df_metrics['MetricName'] == 'CacheMisses') & (df_metrics['Stat'] == 'Sum')]
        for name, group in misses.groupby('Dimensions'):
            fig.add_trace(go.Scatter(x=group['Timestamp'], y=group['Value'], name=f'Misses {name}'), row=4, col=1)

    fig.update_layout(height=1600, title_text=f"Test Results: {cluster_id} ({timestamp})")

    html_content = fig.to_html(full_html=True, include_plotlyjs='cdn')

    # Upload
    output_key = f"{base_prefix}/results_{timestamp}.html"
    print(f"Uploading report to s3://{s3_bucket}/{output_key}")
    s3.put_object(Bucket=s3_bucket, Key=output_key, Body=html_content.encode('utf-8'), ContentType='text/html')
    print("Done.")

if __name__ == "__main__":
    main()
