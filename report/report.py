import os
import boto3
import csv
import json
import io
import datetime
from collections import defaultdict

def parse_csv(content):
    reader = csv.DictReader(io.StringIO(content))
    data = []
    for row in reader:
        data.append(row)
    return data

def main():
    s3_bucket = os.environ['S3_BUCKET']
    s3_prefix = os.environ.get('S3_PREFIX', 'exports/')
    cluster_id = os.environ['CLUSTER_ID']
    timestamp = os.environ['TIMESTAMP']

    s3 = boto3.client('s3')

    base_prefix = f"{s3_prefix}{timestamp}"
    metrics_key = f"{base_prefix}/metrics/{cluster_id}.csv"
    ecs_metrics_key = f"{base_prefix}/metrics/{cluster_id}-ecs.csv"

    metrics_data = []
    ecs_data = []

    print(f"Downloading metrics from s3://{s3_bucket}/{metrics_key}")
    try:
        obj = s3.get_object(Bucket=s3_bucket, Key=metrics_key)
        metrics_data = parse_csv(obj['Body'].read().decode('utf-8'))
    except Exception as e:
        print(f"Error reading metrics: {e}")

    try:
        obj = s3.get_object(Bucket=s3_bucket, Key=ecs_metrics_key)
        ecs_data = parse_csv(obj['Body'].read().decode('utf-8'))
    except Exception as e:
        print(f"Error reading ECS metrics: {e}")

    # Process data for Chart.js
    # Structure: { chart_id: { datasets: [{label: '...', data: [{x: ts, y: val}, ...]}], title: '...' } }

    charts = {
        'load': {'title': 'Generated Load (Ops/Sec)', 'datasets': []},
        'cpu': {'title': 'ElastiCache CPU Utilization', 'datasets': []},
        'network': {'title': 'ElastiCache Network Bytes In', 'datasets': []},
        'ecs_cpu': {'title': 'ECS Load Generator CPU', 'datasets': []},
        'hits': {'title': 'Cache Hits', 'datasets': []},
        'misses': {'title': 'Cache Misses', 'datasets': []}
    }

    # Helper to group by metric name and dimension
    def process_series(data_list, metric_name, stat, chart_key, label_prefix, scale=1.0):
        grouped = defaultdict(list)
        for row in data_list:
            if row['MetricName'] == metric_name and row.get('Stat') == stat:
                dim = row['Dimensions']
                ts = row['Timestamp']
                val = float(row['Value']) * scale
                grouped[dim].append({'x': ts, 'y': val})

        for dim, points in grouped.items():
            points.sort(key=lambda p: p['x'])
            charts[chart_key]['datasets'].append({
                'label': f"{label_prefix} {dim}",
                'data': points
            })

    # 1. Generated Load (CmdGet + CmdSet)
    # We need to aggregate Get and Set if possible, or show them separately.
    # Showing separately is easier for now without pandas.
    # Ops/Sec: Value is Sum per minute? Div by 60.
    process_series(metrics_data, 'CmdGet', 'Sum', 'load', 'Get', 1.0/60.0)
    process_series(metrics_data, 'CmdSet', 'Sum', 'load', 'Set', 1.0/60.0)

    # 2. ElastiCache CPU
    process_series(metrics_data, 'CPUUtilization', 'Average', 'cpu', 'CPU')

    # 3. Network
    process_series(metrics_data, 'NetworkBytesIn', 'Average', 'network', 'NetIn')

    # 4. ECS CPU
    process_series(ecs_data, 'CpuUtilized', 'Average', 'ecs_cpu', 'ECS CPU')

    # 5. Hits/Misses
    process_series(metrics_data, 'CacheHits', 'Sum', 'hits', 'Hits')
    process_series(metrics_data, 'CacheMisses', 'Sum', 'misses', 'Misses')

    # HTML Template
    html_template = """
<!DOCTYPE html>
<html>
<head>
    <title>Test Results: {cluster_id}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/luxon"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon"></script>
    <style>
        body {{ font-family: sans-serif; margin: 20px; }}
        .chart-container {{ width: 80%; margin: 20px auto; }}
    </style>
</head>
<body>
    <h1>Test Results: {cluster_id}</h1>
    <p>Timestamp: {timestamp}</p>

    <div class="chart-container"><canvas id="loadChart"></canvas></div>
    <div class="chart-container"><canvas id="cpuChart"></canvas></div>
    <div class="chart-container"><canvas id="networkChart"></canvas></div>
    <div class="chart-container"><canvas id="ecsCpuChart"></canvas></div>
    <div class="chart-container"><canvas id="hitsChart"></canvas></div>
    <div class="chart-container"><canvas id="missesChart"></canvas></div>

    <script>
        const chartData = {json_data};

        function createChart(canvasId, dataKey) {{
            const ctx = document.getElementById(canvasId).getContext('2d');
            const data = chartData[dataKey];
            if (data.datasets.length === 0) return;

            new Chart(ctx, {{
                type: 'line',
                data: {{ datasets: data.datasets }},
                options: {{
                    responsive: true,
                    plugins: {{
                        title: {{ display: true, text: data.title }}
                    }},
                    scales: {{
                        x: {{ type: 'time' }}
                    }}
                }}
            }});
        }}

        createChart('loadChart', 'load');
        createChart('cpuChart', 'cpu');
        createChart('networkChart', 'network');
        createChart('ecsCpuChart', 'ecs_cpu');
        createChart('hitsChart', 'hits');
        createChart('missesChart', 'misses');
    </script>
</body>
</html>
    """

    html_content = html_template.format(
        cluster_id=cluster_id,
        timestamp=timestamp,
        json_data=json.dumps(charts)
    )

    output_key = f"{base_prefix}/results_{timestamp}.html"
    print(f"Uploading report to s3://{s3_bucket}/{output_key}")
    s3.put_object(Bucket=s3_bucket, Key=output_key, Body=html_content.encode('utf-8'), ContentType='text/html')
    print("Done.")

if __name__ == "__main__":
    main()
