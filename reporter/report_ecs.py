from __future__ import annotations

import sys
import html
from typing import Any

from report_common import get_env_var


def validate_csv_columns(df: Any, csv_name: str) -> bool:
    required_columns = ["Timestamp", "Namespace", "Stat", "MetricName", "Value", "Dimensions"]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        print(f"Error: CSV file '{csv_name}' is missing required columns: {', '.join(missing_columns)}")
        print(f"Expected columns: {', '.join(required_columns)}")
        print(f"Actual columns: {', '.join(df.columns.tolist())}")
        return False
    return True


def read_csv_from_s3(s3_client: Any, bucket: str, key: str) -> Any:
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        import pandas as pd

        return pd.read_csv(obj["Body"])
    except Exception as exc:
        print(f"Error reading {key} from {bucket}: {exc}")
        sys.exit(1)


def parse_dimensions(dim_str: Any) -> str:
    if not isinstance(dim_str, str):
        return "Unknown"

    dims: dict[str, str] = {}
    try:
        for part in dim_str.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                dims[key] = value
    except Exception:
        return "Unknown"

    if "CacheClusterId" in dims:
        return f"Node: {dims['CacheClusterId']}"
    if "ReplicationGroupId" in dims:
        return f"Cluster: {dims['ReplicationGroupId']}"
    if "ServiceName" in dims:
        return f"Service: {dims['ServiceName']}"
    if "ClusterName" in dims:
        return f"ECS Cluster: {dims['ClusterName']}"
    return "Global"


def generate_s3_html_report(
    cluster_id: str,
    timestamp: str,
    summary_stats: list[dict[str, str]],
    throughput_div: str,
    cpu_div: str,
    ecs_div: str,
) -> str:
    escaped_cluster_id = html.escape(cluster_id, quote=True)
    escaped_timestamp = html.escape(timestamp, quote=True)
    stats_rows = []
    for stat in summary_stats:
        stats_rows.append(
            f"""
        <div class="card">
            <h3>{stat['label']}</h3>
            <div class="value">{stat['value']}</div>
        </div>
        """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Performance Report: {escaped_cluster_id}</title>
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
            max-width: 1200px;
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
            <div class="meta">Cluster: {escaped_cluster_id} | Run ID: {escaped_timestamp}</div>
        </header>
        <section class="summary-grid">
            {''.join(stats_rows)}
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


def run_ecs_report() -> None:
    import boto3
    import pandas as pd
    import plotly.graph_objects as go

    bucket = get_env_var("S3_BUCKET")
    prefix = get_env_var("S3_PREFIX")
    timestamp = get_env_var("REPORT_TIMESTAMP")
    cluster_id = get_env_var("CLUSTER_ID")

    s3 = boto3.client("s3")
    metrics_key = f"{prefix}{timestamp}/metrics/{cluster_id}.csv"
    ecs_metrics_key = f"{prefix}{timestamp}/metrics/{cluster_id}-ecs.csv"

    print(f"Reading metrics from s3://{bucket}/{metrics_key}")
    df_ec = read_csv_from_s3(s3, bucket, metrics_key)
    if df_ec is not None and not validate_csv_columns(df_ec, metrics_key):
        print("Validation failed. Exiting.")
        sys.exit(1)

    print(f"Reading ECS metrics from s3://{bucket}/{ecs_metrics_key}")
    df_ecs = read_csv_from_s3(s3, bucket, ecs_metrics_key)
    if df_ecs is not None and not validate_csv_columns(df_ecs, ecs_metrics_key):
        print("Validation failed. Exiting.")
        sys.exit(1)

    frames = [frame for frame in (df_ec, df_ecs) if frame is not None]
    if not frames:
        print("No data found. Exiting.")
        sys.exit(1)

    df = pd.concat(frames)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df.sort_values("Timestamp", inplace=True)
    df["Source"] = df["Dimensions"].apply(parse_dimensions)

    summary_stats: list[dict[str, str]] = []

    throughput_mask = (
        (df["Namespace"] == "AWS/ElastiCache")
        & (df["Stat"] == "Sum")
        & (df["MetricName"].isin(["CmdSet", "CmdGet"]))
    )
    df_throughput = df[throughput_mask].copy()
    fig_throughput = go.Figure()
    max_ops = 0.0
    for source in df_throughput["Source"].unique():
        if "Cluster:" not in source and "Node:" not in source:
            continue
        subset = df_throughput[df_throughput["Source"] == source]
        pivoted = subset.pivot_table(index="Timestamp", columns="MetricName", values="Value", aggfunc="sum").fillna(0)
        pivoted["Total"] = pivoted.get("CmdGet", 0) + pivoted.get("CmdSet", 0)
        pivoted["OpsSec"] = pivoted["Total"] / 60.0
        current_max = pivoted["OpsSec"].max()
        if pd.notna(current_max) and current_max > max_ops:
            max_ops = float(current_max)
        fig_throughput.add_trace(go.Scatter(x=pivoted.index, y=pivoted["OpsSec"], mode="lines", name=source))
    fig_throughput.update_layout(height=400, margin=dict(l=20, r=20, t=20, b=20))
    summary_stats.append({"label": "Peak Ops/Sec", "value": f"{int(max_ops):,}"})

    cpu_mask = (
        (df["Namespace"] == "AWS/ElastiCache")
        & (df["Stat"] == "Average")
        & (df["MetricName"].isin(["EngineCPUUtilization", "CPUUtilization"]))
    )
    df_cpu = df[cpu_mask]
    fig_cpu = go.Figure()
    max_cpu = 0.0
    for source in df_cpu["Source"].unique():
        subset = df_cpu[df_cpu["Source"] == source]
        for metric in subset["MetricName"].unique():
            metric_data = subset[subset["MetricName"] == metric]
            current_max = metric_data["Value"].max()
            if pd.notna(current_max) and current_max > max_cpu:
                max_cpu = float(current_max)
            fig_cpu.add_trace(go.Scatter(x=metric_data["Timestamp"], y=metric_data["Value"], mode="lines", name=f"{source} {metric}"))
    fig_cpu.update_layout(height=400, margin=dict(l=20, r=20, t=20, b=20), yaxis_title="Percent")
    summary_stats.append({"label": "Peak Server CPU", "value": f"{max_cpu:.1f}%"})

    ecs_cpu_mask = (
        (df["Namespace"].str.contains("ECS"))
        & (df["Stat"] == "Average")
        & (df["MetricName"].isin(["CpuUtilized", "CPUUtilization"]))
    )
    df_ecs_cpu = df[ecs_cpu_mask]
    fig_ecs = go.Figure()
    for source in df_ecs_cpu["Source"].unique():
        subset = df_ecs_cpu[df_ecs_cpu["Source"] == source]
        fig_ecs.add_trace(go.Scatter(x=subset["Timestamp"], y=subset["Value"], mode="lines", name=source))
    fig_ecs.update_layout(height=400, margin=dict(l=20, r=20, t=20, b=20))

    html_report = generate_s3_html_report(
        cluster_id,
        timestamp,
        summary_stats,
        fig_throughput.to_html(full_html=False, include_plotlyjs=False),
        fig_cpu.to_html(full_html=False, include_plotlyjs=False),
        fig_ecs.to_html(full_html=False, include_plotlyjs=False),
    )

    output_key = f"{prefix}{timestamp}/results_{timestamp}.html"
    print(f"Uploading report to s3://{bucket}/{output_key}")
    s3.put_object(Bucket=bucket, Key=output_key, Body=html_report, ContentType="text/html")
    print("Done.")
