import boto3
import pandas as pd
import plotly.graph_objects as go
import os
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

    # First parse into raw key/value pairs.
    raw_dims = {}
    try:
        parts = dim_str.split(';')
        for part in parts:
            if '=' in part:
                k, v = part.split('=', 1)
                raw_dims[k] = v
    except Exception:
        pass

    # Support CloudWatch-style "Name=X;Value=Y" as well as "X=Y".
    if 'Name' in raw_dims and 'Value' in raw_dims:
        dims = {raw_dims['Name']: raw_dims['Value']}
    else:
        dims = raw_dims
    if 'CacheClusterId' in dims:
        return f"Node: {dims['CacheClusterId']}"
    elif 'ReplicationGroupId' in dims:
        return f"Cluster: {dims['ReplicationGroupId']}"
    elif 'ServiceName' in dims:
        return f"Service: {dims['ServiceName']}"
    elif 'ClusterName' in dims:
        return f"ECS Cluster: {dims['ClusterName']}"
    return "Global"

def generate_html_report(cluster_id, timestamp, summary_stats, throughput_div, cpu_div, ecs_div):
    """
    Constructs a complete HTML dashboard using a string template.
    """

    stats_rows = ""
    for stat in summary_stats:
        stats_rows += f"""
        <div class="card">
            <h3>{stat['label']}</h3>
            <div class="value">{stat['value']}</div>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Performance Report: {cluster_id}</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f5f7fa;
            color: #333;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max_width: 1200px;
            margin: 0 auto;
        }}
        header {{
            margin-bottom: 30px;
            border-bottom: 1px solid #ddd;
            padding-bottom: 10px;
        }}
        h1 {{ margin: 0; color: #2c3e50; }}
        .meta {{ color: #7f8c8d; font-size: 0.9em; margin-top: 5px; }}

        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
            text-align: center;
        }}
        .card h3 {{
            margin: 0 0 10px 0;
            font-size: 0.9em;
            text-transform: uppercase;
            color: #7f8c8d;
            letter-spacing: 0.5px;
        }}
        .card .value {{
            font-size: 1.8em;
            font-weight: bold;
            color: #2c3e50;
        }}

        .chart-section {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
            margin-bottom: 30px;
        }}
        .chart-section h2 {{
            margin-top: 0;
            font-size: 1.2em;
            border-bottom: 1px solid #eee;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>ElastiCache Performance Report</h1>
            <div class="meta">Cluster: {cluster_id} | Run ID: {timestamp}</div>
        </header>

        <section class="summary-grid">
            {stats_rows}
        </section>

        <section class="chart-section">
            <h2>Throughput (Ops/sec)</h2>
            {throughput_div}
        </section>

        <section class="chart-section">
            <h2>Server CPU Utilization (%)</h2>
            {cpu_div}
        </section>

        <section class="chart-section">
            <h2>Load Generator Health (CPU)</h2>
            {ecs_div}
        </section>
    </div>
</body>
</html>
    """
    return html

def main():
    # 1. Configuration
    BUCKET = get_env_var("S3_BUCKET")
    PREFIX = get_env_var("S3_PREFIX")
    TIMESTAMP = get_env_var("REPORT_TIMESTAMP")
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
    frames = []
    if df_ec is not None: frames.append(df_ec)
    if df_ecs is not None: frames.append(df_ecs)

    if not frames:
        print("No data frames to process.")
        sys.exit(1)

    df = pd.concat(frames)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df.sort_values('Timestamp', inplace=True)
    df['Source'] = df['Dimensions'].apply(parse_dimensions)

    # 5. Build Charts & Stats
    summary_stats = []

    # --- Throughput ---
    throughput_mask = (
        (df['Namespace'] == 'AWS/ElastiCache') &
        (df['Stat'] == 'Sum') &
        (df['MetricName'].isin(['CmdSet', 'CmdGet']))
    )
    df_throughput = df[throughput_mask].copy()

    fig_throughput = go.Figure()
    max_ops = 0

    for source in df_throughput['Source'].unique():
        if 'Cluster:' in source or 'Node:' in source:
            subset = df_throughput[df_throughput['Source'] == source]
            pivoted = subset.pivot_table(index='Timestamp', columns='MetricName', values='Value', aggfunc='sum').fillna(0)

            # Robust calculation: use .get(0) to handle missing read or write metrics gracefully
            # e.g. for read-only tests, CmdSet might be missing
            pivoted['Total'] = pivoted.get('CmdGet', 0) + pivoted.get('CmdSet', 0)
            pivoted['OpsSec'] = pivoted['Total'] / 60.0

            # Update max ops for summary
            current_max = pivoted['OpsSec'].max()
            if current_max > max_ops:
                max_ops = current_max

            fig_throughput.add_trace(
                go.Scatter(x=pivoted.index, y=pivoted['OpsSec'], mode='lines', name=f"{source}")
            )

    fig_throughput.update_layout(height=400, margin=dict(l=20, r=20, t=20, b=20))
    summary_stats.append({"label": "Peak Ops/Sec", "value": f"{int(max_ops):,}"})

    # --- Server CPU ---
    cpu_mask = (
        (df['Namespace'] == 'AWS/ElastiCache') &
        (df['Stat'] == 'Average') &
        (df['MetricName'].isin(['EngineCPUUtilization', 'CPUUtilization']))
    )
    df_cpu = df[cpu_mask]

    fig_cpu = go.Figure()
    max_cpu = 0

    for source in df_cpu['Source'].unique():
        subset = df_cpu[df_cpu['Source'] == source]
        for metric in subset['MetricName'].unique():
            metric_data = subset[subset['MetricName'] == metric]
            current_max = metric_data['Value'].max()
            if current_max > max_cpu:
                max_cpu = current_max

            fig_cpu.add_trace(
                go.Scatter(x=metric_data['Timestamp'], y=metric_data['Value'], mode='lines', name=f"{source} {metric}")
            )

    fig_cpu.update_layout(height=400, margin=dict(l=20, r=20, t=20, b=20), yaxis_title="Percent")
    summary_stats.append({"label": "Peak Server CPU", "value": f"{max_cpu:.1f}%"})

    # --- ECS CPU ---
    ecs_cpu_mask = (
        (df['Namespace'].str.contains('ECS', na=False)) &
        (df['Stat'] == 'Average') &
        (df['MetricName'].isin(['CpuUtilized', 'CPUUtilization']))
    )
    df_ecs_cpu = df[ecs_cpu_mask]

    fig_ecs = go.Figure()

    for source in df_ecs_cpu['Source'].unique():
        subset = df_ecs_cpu[df_ecs_cpu['Source'] == source]
        fig_ecs.add_trace(
            go.Scatter(x=subset['Timestamp'], y=subset['Value'], mode='lines', name=f"{source}")
        )
    fig_ecs.update_layout(height=400, margin=dict(l=20, r=20, t=20, b=20))

    # 6. Generate HTML
    # We output only the div string for the charts, excluding the JS (which is loaded once in head)
    # Note: include_plotlyjs='cdn' in to_html usually puts the script tag.
    # To embed multiple charts efficiently, we can use full_html=False and include_plotlyjs=False,
    # then manually add the script tag in the template.

    throughput_div = fig_throughput.to_html(full_html=False, include_plotlyjs=False)
    cpu_div = fig_cpu.to_html(full_html=False, include_plotlyjs=False)
    ecs_div = fig_ecs.to_html(full_html=False, include_plotlyjs=False)

    html_report = generate_html_report(
        CLUSTER_ID,
        TIMESTAMP,
        summary_stats,
        throughput_div,
        cpu_div,
        ecs_div
    )

    # 7. Upload to S3
    output_key = f"{PREFIX}{TIMESTAMP}/results_{TIMESTAMP}.html"
    print(f"Uploading report to s3://{BUCKET}/{output_key}")

    s3.put_object(
        Bucket=BUCKET,
        Key=output_key,
        Body=html_report,
        ContentType='text/html'
    )
    print("Done.")

if __name__ == "__main__":
    main()
