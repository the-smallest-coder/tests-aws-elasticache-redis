import argparse
import os
import sys
import re
from io import StringIO

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def read_file_content(path):
    """Read file content from a local path or an S3 URI (s3://bucket/key)."""
    if path.startswith('s3://'):
        import boto3
        parts = path[5:].split('/', 1)
        if len(parts) != 2 or not parts[1]:
            raise ValueError(f"Invalid S3 URI: {path}")
        bucket, key = parts
        obj = boto3.client('s3').get_object(Bucket=bucket, Key=key)
        return obj['Body'].read().decode('utf-8')
    else:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

def parse_memtier_logs(log_content):
    data = []
    # Pattern to extract timestamp and memtier output from CloudWatch-exported logs.
    # CloudWatch export format: [ISO_TIMESTAMP] [stream_name] message
    # Memtier actual output example:
    #   [RUN #1 0%,   1 secs]  2 threads:  6939 (avg: 6939) ops/sec, 906.82KB/sec (avg: 906.82KB/sec),  0.29 (avg: 0.29) msec latency

    lines = log_content.split('\n')
    for line in lines:
        # Match lines containing memtier benchmark progress output (case-insensitive)
        if 'ops/sec' not in line.lower() or 'latency' not in line.lower():
            continue

        # Extract timestamp from the CloudWatch log prefix: [2026-02-27T13:29:28.717000]
        ts_match = re.search(r'^\[([\d\-T:\.]+)\]', line)
        if not ts_match:
            continue
        timestamp = ts_match.group(1)

        # Extract average ops/sec:  "6939 (avg:    6939) ops/sec"
        # We capture the avg value inside parentheses for a smoother time-series.
        ops_match = re.search(r'\(avg:\s*([\d\.]+)\)\s*ops/sec', line)
        if not ops_match:
            # Fallback: number immediately before "ops/sec"
            ops_match = re.search(r'([\d\.]+)\s*ops/sec', line)
        ops_sec = float(ops_match.group(1)) if ops_match else None

        # Extract average latency:  "0.29 (avg:  0.29) msec latency"
        lat_match = re.search(r'\(avg:\s*([\d\.]+)\)\s*msec latency', line)
        if not lat_match:
            # Fallback: number immediately before "msec latency"
            lat_match = re.search(r'([\d\.]+)\s*msec latency', line)
        latency = float(lat_match.group(1)) if lat_match else None

        # Extract average bandwidth: "906.82KB/sec (avg: 906.82KB/sec)" or "1.01MB/sec (avg: 1.01MB/sec)"
        bw_match = re.search(r'\(avg:\s*([\d\.]+)(KB|MB)/sec\)', line)
        if bw_match:
            bw_val = float(bw_match.group(1))
            bw_kbs = bw_val * 1024 if bw_match.group(2) == 'MB' else bw_val
        else:
            bw_kbs = None

        if ops_sec is not None and latency is not None:
            data.append({
                'Timestamp': timestamp,
                'Ops/sec': ops_sec,
                'Latency (ms)': latency,
                'Bandwidth_KBs': bw_kbs,
            })

    df = pd.DataFrame(data)
    if not df.empty:
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], format='ISO8601')
    return df

def parse_memtier_extra_stats(log_content):
    """Extract scalar statistics and per-minute OOM series from raw memtier log.

    Returns a dict with:
      first_eviction_ts  – datetime of first -OOM (nearest preceding CW timestamp)
      oom_df             – DataFrame with columns [Timestamp, OOM_per_min] (1-min buckets)
    """
    ts_pat = re.compile(r'^\[([\d\-T:\.]+)\]')
    last_ts = None
    first_eviction_ts = None
    oom_events = []  # list of approx datetimes for each OOM line

    for line in log_content.splitlines():
        m = ts_pat.match(line)
        if m:
            try:
                last_ts = pd.to_datetime(m.group(1), format='ISO8601')
            except Exception:
                pass
        if '-OOM command not allowed' in line and last_ts is not None:
            if first_eviction_ts is None:
                first_eviction_ts = last_ts
            oom_events.append(last_ts)

    # Build per-minute bucket DataFrame
    if oom_events:
        oom_series = pd.Series(
            [1] * len(oom_events),
            index=pd.DatetimeIndex(oom_events)
        )
        oom_df = oom_series.resample('1min').sum().rename('OOM_per_min').reset_index()
        oom_df.columns = ['Timestamp', 'OOM_per_min']
        # Strip timezone so it's consistent with logs_df timestamps
        if oom_df['Timestamp'].dt.tz is not None:
            oom_df['Timestamp'] = oom_df['Timestamp'].dt.tz_localize(None)
    else:
        oom_df = pd.DataFrame(columns=['Timestamp', 'OOM_per_min'])

    return {
        'first_eviction_ts': first_eviction_ts,
        'oom_df': oom_df,
    }


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
    
    if not df.empty:
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], format='ISO8601')
    return df

def _shorten_dim(dim, cluster_id=''):
    """Return a concise label from a CloudWatch Dimensions string.

    Prefers NodeGroupId value (e.g. '0001') or strips the cluster_id prefix
    from a ClusterName/ServiceName value so legends stay readable.
    """
    kv = dict(part.split('=', 1) for part in dim.split(';') if '=' in part)
    # Prefer the most specific identifiers
    for key in ('NodeGroupId', 'CacheClusterId'):
        if key in kv:
            return kv[key]
    # For ClusterName / ServiceName strip the cluster_id prefix to reduce noise
    for key in ('ServiceName', 'ClusterName'):
        if key in kv:
            val = kv[key]
            if cluster_id and val.startswith(cluster_id):
                suffix = val[len(cluster_id):].lstrip('-') or val
                return suffix if suffix else val
            return val
    # Fallback: value of the first key
    first_val = next(iter(kv.values()), dim)
    return first_val


def _resample_logs(logs_df, rule='1min'):
    """Resample memtier log data to `rule` intervals using mean, to smooth the dense signal."""
    if logs_df.empty:
        return logs_df
    df = logs_df.set_index('Timestamp').sort_index()
    df = df.resample(rule).mean().dropna().reset_index()
    return df


def create_report(metrics_df, logs_df, cluster_id, suffix, ecs_metrics_df=None, config=None, extra_stats=None):
    ecs_df = ecs_metrics_df if ecs_metrics_df is not None else pd.DataFrame()
    config = config or {}
    extra_stats = extra_stats or {}

    # Resample dense log data to 1-minute averages for clean lines
    logs_resampled = _resample_logs(logs_df)

    # Determine the memtier benchmark time window to frame all charts
    if not logs_resampled.empty:
        x_min = logs_resampled['Timestamp'].min()
        x_max = logs_resampled['Timestamp'].max()
    else:
        x_min = x_max = None

    # 4 rows:
    #   [1] Throughput + Latency dual-Y  (memtier window)
    #   [2] Eviction Pressure OOM/min    (memtier window, hidden if no OOMs)
    #   [3] ECS Load Generator CPU       (full CloudWatch window)
    #   [4] ElastiCache Memory           (full CloudWatch window)
    # Rows are NOT shared so memtier rows can be clipped independently.
    oom_df = extra_stats.get('oom_df', pd.DataFrame())
    has_evictions = not oom_df.empty and oom_df['OOM_per_min'].sum() > 0

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=False,
        vertical_spacing=0.08,
        specs=[
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
        ],
        subplot_titles=(
            "Memtier — Throughput & Latency (1-min avg)",
            "Memtier — Eviction Pressure (OOM events / min)",
            "ECS Load Generator CPU (%)",
            "ElastiCache Memory Usage (%)",
        ),
        row_heights=[0.30, 0.20, 0.25, 0.25],
    )

    line_opts = dict(width=2)

    # --- Row 1: Throughput (primary Y) + Latency (secondary Y) ---
    if not logs_resampled.empty:
        fig.add_trace(go.Scatter(
            x=logs_resampled['Timestamp'], y=logs_resampled['Ops/sec'],
            name="Throughput", mode='lines', line=dict(**line_opts, color='#1f77b4'),
            hovertemplate="%{x|%H:%M}<br><b>%{y:,.0f} ops/sec</b><extra></extra>"
        ), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=logs_resampled['Timestamp'], y=logs_resampled['Latency (ms)'],
            name="Latency", mode='lines', line=dict(**line_opts, color='#d62728', dash='dot'),
            hovertemplate="%{x|%H:%M}<br><b>%{y:.2f} ms</b><extra></extra>"
        ), row=1, col=1, secondary_y=True)
    else:
        fig.add_annotation(text="No Log Data", xref="paper", yref="y",
                           x=0.5, y=0.5, showarrow=False, row=1, col=1)

    # --- Row 2: Eviction Pressure (OOM events / min) ---
    if has_evictions:
        # Clip to memtier window so it aligns visually with row 1
        oom_plot = oom_df.copy()
        if x_min is not None:
            oom_plot = oom_plot[(oom_plot['Timestamp'] >= x_min) & (oom_plot['Timestamp'] <= x_max)]
        fig.add_trace(go.Bar(
            x=oom_plot['Timestamp'], y=oom_plot['OOM_per_min'],
            name="OOM events/min", marker_color='#ef5350',
            hovertemplate="%{x|%H:%M}<br><b>%{y:,} OOM/min</b><extra></extra>"
        ), row=2, col=1)
    else:
        fig.add_annotation(text="No eviction pressure detected", xref="x2", yref="y2",
                           x=0.5, y=0.5, showarrow=False, row=2, col=1)

    # --- Row 3: ECS CPU ---
    if not ecs_df.empty:
        cpu_df = ecs_df[(ecs_df['MetricName'] == 'CPUUtilization') & (ecs_df['Stat'] == 'Average')]
        if cpu_df.empty:
            cpu_df = ecs_df[(ecs_df['MetricName'] == 'TaskCpuUtilization') & (ecs_df['Stat'] == 'Average')]
        if not cpu_df.empty:
            for dim, group in cpu_df.groupby('Dimensions'):
                fig.add_trace(go.Scatter(
                    x=group['Timestamp'], y=group['Value'],
                    name=f"CPU – {_shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**line_opts, color='#2ca02c'),
                    hovertemplate="%{x|%H:%M}<br><b>%{y:.1f}%</b><extra></extra>"
                ), row=3, col=1)
        else:
            fig.add_annotation(text="No ECS CPU Metrics", xref="paper", yref="y3",
                               x=0.5, y=0.5, showarrow=False, row=3, col=1)
    elif not metrics_df.empty:
        cpu_df = metrics_df[(metrics_df['MetricName'] == 'EngineCPUUtilization') & (metrics_df['Stat'] == 'Average')]
        if not cpu_df.empty:
            for dim, group in cpu_df.groupby('Dimensions'):
                fig.add_trace(go.Scatter(
                    x=group['Timestamp'], y=group['Value'],
                    name=f"EngineCPU – {_shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**line_opts, color='#2ca02c'),
                    hovertemplate="%{x|%H:%M}<br><b>%{y:.1f}%</b><extra></extra>"
                ), row=3, col=1)
        else:
            fig.add_annotation(text="No CPU Metrics", xref="paper", yref="y3",
                               x=0.5, y=0.5, showarrow=False, row=3, col=1)
    else:
        fig.add_annotation(text="No CPU Data", xref="paper", yref="y3",
                           x=0.5, y=0.5, showarrow=False, row=3, col=1)

    # --- Row 4: ElastiCache Memory ---
    if not metrics_df.empty:
        mem_df = metrics_df[
            (metrics_df['MetricName'] == 'DatabaseMemoryUsageCountedForEvictPercentage') &
            (metrics_df['Stat'] == 'Average')]
        if mem_df.empty:
            mem_df = metrics_df[
                (metrics_df['MetricName'] == 'DatabaseCapacityUsageCountedForEvictPercentage') &
                (metrics_df['Stat'] == 'Average')]
        if not mem_df.empty:
            colors = ['#9467bd', '#ff7f0e', '#8c564b', '#e377c2']
            for i, (dim, group) in enumerate(mem_df.groupby('Dimensions')):
                fig.add_trace(go.Scatter(
                    x=group['Timestamp'], y=group['Value'],
                    name=f"Mem – {_shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**line_opts, color=colors[i % len(colors)]),
                    hovertemplate="%{x|%H:%M}<br><b>%{y:.2f}%</b><extra></extra>"
                ), row=4, col=1)
        else:
            fig.add_annotation(text="No Memory Metrics", xref="paper", yref="y4",
                               x=0.5, y=0.5, showarrow=False, row=4, col=1)
    else:
        fig.add_annotation(text="No CloudWatch Metrics", xref="paper", yref="y4",
                           x=0.5, y=0.5, showarrow=False, row=4, col=1)

    # --- Axis labels ---
    fig.update_yaxes(title_text="ops/sec", row=1, col=1, secondary_y=False,
                     title_font=dict(color='#1f77b4'), tickfont=dict(color='#1f77b4'))
    fig.update_yaxes(title_text="ms", row=1, col=1, secondary_y=True,
                     title_font=dict(color='#d62728'), tickfont=dict(color='#d62728'),
                     showgrid=False)
    fig.update_yaxes(title_text="OOM/min", row=2, col=1,
                     title_font=dict(color='#ef5350'), tickfont=dict(color='#ef5350'))
    fig.update_yaxes(title_text="%", row=3, col=1)
    fig.update_yaxes(title_text="%", row=4, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=1, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=2, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=3, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=4, col=1)

    # Lock rows 1 & 2 (memtier group) to benchmark window; rows 3-4 are free.
    if x_min is not None and x_max is not None:
        fig.update_xaxes(range=[x_min, x_max], row=1, col=1)
        fig.update_xaxes(range=[x_min, x_max], row=2, col=1)

    fig.update_layout(
        template='plotly_white',
        height=1300,
        title=None,
        legend=dict(
            orientation='v',
            x=1.01, y=1,
            xanchor='left',
            font=dict(size=12),
            bgcolor='rgba(255,255,255,0.85)',
            bordercolor='#e8eaed',
            borderwidth=1
        ),
        hovermode='x unified',
        margin=dict(l=70, r=200, t=40, b=60),
        plot_bgcolor='#ffffff',
        paper_bgcolor='#ffffff',
        font=dict(family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif", size=12)
    )
    fig.update_xaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)

    # Range sliders on infrastructure rows (independent of memtier rows)
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.03), row=3, col=1)
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.03), row=4, col=1)

    chart_html = fig.to_html(include_plotlyjs='cdn', full_html=False)

    # Determine time range from ALL data sources, not just log timestamps
    time_range = ''
    all_timestamps = []
    for df, col in [(logs_df, 'Timestamp'), (metrics_df, 'Timestamp'), (ecs_df, 'Timestamp')]:
        if not df.empty and col in df.columns:
            ts_min = df[col].min()
            ts_max = df[col].max()
            # Normalize to tz-naive UTC so tz-aware (CloudWatch) and tz-naive (logs) can be compared
            if hasattr(ts_min, 'tzinfo') and ts_min.tzinfo is not None:
                ts_min = ts_min.replace(tzinfo=None)
                ts_max = ts_max.replace(tzinfo=None)
            all_timestamps.extend([ts_min, ts_max])
    if all_timestamps:
        t0 = min(all_timestamps)
        t1 = max(all_timestamps)
        duration_min = int((t1 - t0).total_seconds() / 60)
        time_range = f"{t0.strftime('%Y-%m-%d %H:%M UTC')} – {t1.strftime('%H:%M UTC')} ({duration_min} min)"

    cluster_mode = str((config or {}).get('cluster_mode', 'false')).lower() == 'true'
    id_label = 'Cluster' if cluster_mode else 'Replication Group'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ElastiCache Report — {cluster_id}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f4f6f9;
      color: #202124;
      font-size: 14px;
    }}
    .page-header {{
      background: linear-gradient(135deg, #1a56db 0%, #0d47a1 100%);
      color: #fff;
      padding: 24px 32px 20px;
    }}
    .page-header h1 {{
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -.2px;
      margin-bottom: 4px;
    }}
    .page-header .meta {{
      font-size: 12px;
      opacity: .75;
      margin-bottom: 12px;
    }}
    .page-header .pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .pill {{
      background: rgba(255,255,255,.15);
      border: 1px solid rgba(255,255,255,.3);
      border-radius: 4px;
      padding: 3px 10px;
      font-size: 12px;
      font-weight: 600;
    }}
    .pill span {{ font-weight: 400; opacity: .85; }}
    .content {{ max-width: 1400px; margin: 0 auto; padding: 24px 28px; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 14px;
      margin-bottom: 28px;
    }}
    .card {{
      background: #fff;
      border: 1px solid #e0e4ea;
      border-radius: 10px;
      padding: 16px 18px;
      box-shadow: 0 1px 3px rgba(0,0,0,.06);
    }}
    .card-label {{
      font-size: 10px;
      font-weight: 700;
      color: #80868b;
      text-transform: uppercase;
      letter-spacing: .6px;
      margin-bottom: 8px;
    }}
    .card-value {{
      font-size: 26px;
      font-weight: 700;
      line-height: 1;
    }}
    .card-unit {{
      font-size: 12px;
      font-weight: 400;
      color: #aaa;
      margin-left: 3px;
    }}
    .chart-wrap {{
      background: #fff;
      border: 1px solid #e0e4ea;
      border-radius: 10px;
      padding: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,.06);
    }}
  </style>
</head>
<body>

<div class="page-header">
  <h1>ElastiCache Performance Report</h1>
  <div class="meta">{id_label}: {cluster_id}&nbsp;&nbsp;·&nbsp;&nbsp;Run: {suffix}&nbsp;&nbsp;{'·&nbsp;&nbsp;' + time_range if time_range else ''}</div>
  {_header_pills(config)}
</div>

<div class="content">
  {_stat_cards_html(logs_df, metrics_df, ecs_df, extra_stats)}
  <div class="chart-wrap">
    {chart_html}
  </div>
</div>

</body>
</html>"""

def _header_pills(config):
    config = config or {}
    cluster_mode = str(config.get('cluster_mode', 'false')).lower() == 'true'
    mode_label = 'Cluster Mode' if cluster_mode else 'Non-Cluster'
    items = [
        ('Engine', config.get('engine_type')),
        ('Version', config.get('engine_version')),
        ('Node type', config.get('node_type')),
        ('Nodes', config.get('node_count')),
        ('Mode', mode_label),
    ]
    pills = ''.join(
        f"<div class='pill'>{label}: <span>{val}</span></div>"
        for label, val in items if val
    )
    return f"<div class='pills'>{pills}</div>" if pills else ''


def _stat_cards_html(logs_df, metrics_df, ecs_df, extra_stats=None):
    extra_stats = extra_stats or {}
    # Each card: (label, value_str, unit, color[, tooltip])
    cards = []

    if not logs_df.empty:
        # --- Throughput ---
        avg_ops = logs_df['Ops/sec'].mean()
        cards.append(('Avg Throughput', f"{avg_ops:,.0f}", 'ops/sec', '#1a56db', ''))
        cards.append(('Peak Throughput', f"{logs_df['Ops/sec'].max():,.0f}", 'ops/sec', '#1a56db', ''))

        # --- Stability: Coefficient of Variation (lower = more stable) ---
        if avg_ops > 0:
            cv = logs_df['Ops/sec'].std() / avg_ops * 100
            cv_color = '#188038' if cv < 10 else ('#e8710a' if cv < 25 else '#d93025')
            cards.append(('Throughput CV', f"{cv:.1f}", '%', cv_color,
                          'Coefficient of Variation of ops/sec. Lower = more stable.'))

        # --- Latency ---
        cards.append(('Avg Latency', f"{logs_df['Latency (ms)'].mean():.2f}", 'ms', '#e8710a', ''))
        cards.append(('Max Latency', f"{logs_df['Latency (ms)'].max():.2f}", 'ms', '#e8710a', ''))

        # --- Bandwidth ---
        if 'Bandwidth_KBs' in logs_df.columns and logs_df['Bandwidth_KBs'].notna().any():
            avg_bw = logs_df['Bandwidth_KBs'].mean()
            if avg_bw >= 1024:
                bw_str, bw_unit = f"{avg_bw / 1024:.2f}", 'MB/s'
            else:
                bw_str, bw_unit = f"{avg_bw:.0f}", 'KB/s'
            cards.append(('Avg Bandwidth', bw_str, bw_unit, '#0097a7',
                          'Average network throughput reported by memtier.'))

        # --- Benchmark duration ---
        duration_min = (logs_df['Timestamp'].max() - logs_df['Timestamp'].min()).total_seconds() / 60
        cards.append(('Benchmark Duration', f"{duration_min:.0f}", 'min', '#5c6bc0',
                      'Actual memtier benchmark runtime (excludes key pre-population).'))

    # --- ECS CPU ---
    if not ecs_df.empty:
        cpu_df = ecs_df[(ecs_df['MetricName'] == 'CPUUtilization') & (ecs_df['Stat'] == 'Average')]
        if not cpu_df.empty:
            cards.append(('Avg ECS CPU', f"{cpu_df['Value'].mean():.1f}", '%', '#188038', ''))
            cards.append(('Peak ECS CPU', f"{cpu_df['Value'].max():.1f}", '%', '#188038', ''))

    # --- ElastiCache Memory ---
    if not metrics_df.empty:
        for metric in ('DatabaseMemoryUsageCountedForEvictPercentage',
                       'DatabaseCapacityUsageCountedForEvictPercentage'):
            mem_df = metrics_df[(metrics_df['MetricName'] == metric) & (metrics_df['Stat'] == 'Average')]
            if not mem_df.empty:
                max_mem = mem_df['Value'].max()
                headroom = 100.0 - max_mem
                headroom_color = '#188038' if headroom > 10 else ('#e8710a' if headroom >= 0 else '#d93025')
                cards.append(('Avg Memory', f"{mem_df['Value'].mean():.2f}", '%', '#a142f4', ''))
                cards.append(('Max Memory', f"{max_mem:.2f}", '%', '#a142f4', ''))
                cards.append(('Mem Headroom', f"{headroom:+.1f}", '%', headroom_color,
                              '100 − peak memory usage. Negative means eviction territory.'))
                break

    # --- Time to first eviction (OOM) ---
    first_eviction_ts = extra_stats.get('first_eviction_ts')
    if first_eviction_ts is not None and not logs_df.empty:
        bench_start = logs_df['Timestamp'].min()
        # Normalize tzinfo for subtraction
        fets = first_eviction_ts
        if hasattr(fets, 'tzinfo') and fets.tzinfo is not None:
            fets = fets.replace(tzinfo=None)
        delta_min = (fets - bench_start).total_seconds() / 60
        oom_label = f"{fets.strftime('%H:%M')} UTC (+{delta_min:.0f} min)"
        oom_color = '#d93025'
        oom_tip = 'Approximate time the Redis instance first ran out of memory. "+N min" is relative to benchmark start.'
    else:
        oom_label, oom_color = 'None', '#188038'
        oom_tip = 'No -OOM rejections. The instance had sufficient memory for the entire benchmark.'
    cards.append(('First Eviction', oom_label, '', oom_color, oom_tip))

    if not cards:
        return ''

    html = ''.join(
        f"<div class='card' title='{tip}'>"
        f"<div class='card-label'>{label}</div>"
        f"<div class='card-value' style='color:{color}'>{val}"
        f"<span class='card-unit'>{unit}</span></div>"
        f"</div>"
        for label, val, unit, color, tip in cards
    )
    return f"<div class='cards'>{html}</div>"


def parse_args():
    """Parse CLI arguments, falling back to environment variables for ECS usage."""
    parser = argparse.ArgumentParser(
        description='Generate HTML performance report from ElastiCache test results.',
        epilog=(
            'Local example:\n'
            '  python report_generator.py \\\n'
            '    --metrics-csv results/20260227-140039/metrics/cluster.csv \\\n'
            '    --ecs-metrics-csv results/20260227-140039/metrics/cluster-ecs.csv \\\n'
            '    --logs-txt results/20260227-140039/logs/cluster.txt \\\n'
            '    --output results/20260227-140039/report.html \\\n'
            '    --cluster-id my-cluster'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--metrics-csv', default=os.environ.get('METRICS_CSV'),
                        help='ElastiCache metrics CSV (local path or s3:// URI). Env: METRICS_CSV')
    parser.add_argument('--ecs-metrics-csv', default=os.environ.get('ECS_METRICS_CSV', ''),
                        help='ECS metrics CSV (local path or s3:// URI, optional). Env: ECS_METRICS_CSV')
    parser.add_argument('--logs-txt', default=os.environ.get('LOGS_TXT'),
                        help='Memtier log file (local path or s3:// URI). Env: LOGS_TXT')
    parser.add_argument('--output', default=None,
                        help='Local output HTML path. If omitted, uploads to S3 using OUTPUT_BUCKET.')
    parser.add_argument('--output-bucket', default=os.environ.get('OUTPUT_BUCKET'),
                        help='S3 bucket for report upload (ECS mode). Env: OUTPUT_BUCKET')
    parser.add_argument('--output-prefix', default=os.environ.get('OUTPUT_PREFIX', ''),
                        help='S3 key prefix. Env: OUTPUT_PREFIX')
    parser.add_argument('--suffix', default=os.environ.get('SUFFIX', 'report'),
                        help='Report filename suffix. Env: SUFFIX')
    parser.add_argument('--cluster-id', default=os.environ.get('CLUSTER_ID', 'Unknown'),
                        help='ElastiCache replication group ID. Env: CLUSTER_ID')
    parser.add_argument('--cluster-mode', default=os.environ.get('CLUSTER_MODE', 'false'),
                        help='Whether cluster mode is enabled (true/false). Env: CLUSTER_MODE')
    parser.add_argument('--engine-type', default=os.environ.get('ENGINE_TYPE', ''),
                        help='Engine type (e.g. redis). Env: ENGINE_TYPE')
    parser.add_argument('--engine-version', default=os.environ.get('ENGINE_VERSION', ''),
                        help='Engine version. Env: ENGINE_VERSION')
    parser.add_argument('--node-type', default=os.environ.get('NODE_TYPE', ''),
                        help='ElastiCache node type. Env: NODE_TYPE')
    parser.add_argument('--node-count', default=os.environ.get('NODE_COUNT', ''),
                        help='Number of nodes. Env: NODE_COUNT')
    return parser.parse_args()


def main():
    try:
        args = parse_args()

        if not args.metrics_csv:
            print("Error: --metrics-csv (or METRICS_CSV env) is required")
            sys.exit(1)
        if not args.logs_txt:
            print("Error: --logs-txt (or LOGS_TXT env) is required")
            sys.exit(1)
        if not args.output and not args.output_bucket:
            print("Error: --output (local) or --output-bucket (S3) is required")
            sys.exit(1)

        print(f"Starting report generation for {args.cluster_id}")

        # --- Load ElastiCache metrics ---
        print(f"Reading metrics from {args.metrics_csv}")
        try:
            metrics_content = read_file_content(args.metrics_csv)
        except Exception as e:
            print(f"Error: Failed to read metrics from {args.metrics_csv}: {e}")
            sys.exit(1)
        metrics_df = parse_metrics_csv(metrics_content)

        # --- Load memtier logs ---
        print(f"Reading logs from {args.logs_txt}")
        try:
            logs_content = read_file_content(args.logs_txt)
        except Exception as e:
            print(f"Error: Failed to read logs from {args.logs_txt}: {e}")
            sys.exit(1)
        logs_df = parse_memtier_logs(logs_content)
        extra_stats = parse_memtier_extra_stats(logs_content)

        # --- Load ECS metrics (optional) ---
        ecs_metrics_df = pd.DataFrame()
        if args.ecs_metrics_csv:
            print(f"Reading ECS metrics from {args.ecs_metrics_csv}")
            try:
                ecs_content = read_file_content(args.ecs_metrics_csv)
                ecs_metrics_df = parse_metrics_csv(ecs_content)
            except Exception as e:
                print(f"Warning: Failed to read ECS metrics: {e}, continuing without them.")

        # --- Generate report ---
        print("Generating report...")
        config = {
            'engine_type': args.engine_type,
            'engine_version': args.engine_version,
            'node_type': args.node_type,
            'node_count': args.node_count,
            'cluster_mode': args.cluster_mode,
        }
        html_content = create_report(metrics_df, logs_df, args.cluster_id, args.suffix, ecs_metrics_df, config,
                                      extra_stats=extra_stats)

        # --- Write output ---
        if args.output:
            # Local mode: write to file
            os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"Report written to {args.output}")
        else:
            # S3 mode (ECS): upload to bucket
            import boto3
            timestamp_match = re.search(r'\d{8}-\d{6}', args.metrics_csv or '')
            if timestamp_match:
                timestamp = timestamp_match.group(0)
                output_key = f"{args.output_prefix}{timestamp}/results_{args.suffix}.html"
            else:
                output_key = f"{args.output_prefix}results_{args.suffix}.html"

            print(f"Uploading report to s3://{args.output_bucket}/{output_key}")
            s3 = boto3.client('s3')
            s3.put_object(
                Bucket=args.output_bucket,
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
