"""Plotly figure builders for memtier benchmark and infrastructure chart groups."""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from helpers import (
    LAYOUT_BASE, LEGEND_H, LINE_OPTS,
    C_THROUGHPUT, C_LATENCY, C_OOM_BAR, C_EVICTION_CW, C_HIT_RATE,
    C_CPU_ECS, C_ENGINE_CPU, C_NET_TX_ECS, C_NET_TX_CACHE, C_ECS_MEM,
    MEM_COLORS,
    C_CREDIT_BAL, C_CREDIT_USE,
    C_LAT_GET, C_LAT_SET, C_LAT_STR,
    C_THROTTLE_IN, C_THROTTLE_OUT, C_THROTTLE_PPS,
    C_CURR_CONN, C_MEM_FRAG,
    metric_filter, cache_hit_rate_df, shorten_dim, select_mem_dims,
)


# ------------------------------------------------------------------ #
#  GROUP 1 — Memtier Benchmark figure                                  #
# ------------------------------------------------------------------ #

def build_memtier_figure(logs_resampled, oom_df, metrics_df, x_min, x_max):
    """Build a 3-row figure: throughput+latency, eviction pressure, cache hit rate.

    Returns a Plotly Figure ready for ``to_html()``.
    """
    has_evictions = not oom_df.empty and oom_df['OOM_per_min'].sum() > 0

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=False,
        vertical_spacing=0.18,
        specs=[[{"secondary_y": True}], [{"secondary_y": False}], [{"secondary_y": False}]],
        subplot_titles=(
            "Throughput & Latency (1-min avg)",
            "Eviction Pressure (OOM events / min)",
            "Cache Hit Rate (%)",
        ),
        row_heights=[0.50, 0.27, 0.23],
    )

    # ---- Row 1: Throughput + Latency ----
    if not logs_resampled.empty:
        fig.add_trace(go.Scatter(
            x=logs_resampled['Timestamp'], y=logs_resampled['Ops/sec'],
            name="Throughput", mode='lines',
            line=dict(**LINE_OPTS, color=C_THROUGHPUT),
            legend="legend",
            hovertemplate="%{x|%H:%M}<br><b>%{y:,.0f} ops/sec</b><extra></extra>"
        ), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=logs_resampled['Timestamp'], y=logs_resampled['Latency (ms)'],
            name="Latency", mode='lines',
            line=dict(**LINE_OPTS, color=C_LATENCY, dash='dot'),
            legend="legend",
            hovertemplate="%{x|%H:%M}<br><b>%{y:.2f} ms</b><extra></extra>"
        ), row=1, col=1, secondary_y=True)
    else:
        fig.add_annotation(text="No Log Data", xref="paper", yref="y",
                           x=0.5, y=0.5, showarrow=False, row=1, col=1)

    # ---- Row 2: OOM bar + CW Evictions overlay ----
    if has_evictions:
        oom_plot = oom_df.copy()
        if x_min is not None:
            oom_plot = oom_plot[(oom_plot['Timestamp'] >= x_min) & (oom_plot['Timestamp'] <= x_max)]
        fig.add_trace(go.Bar(
            x=oom_plot['Timestamp'], y=oom_plot['OOM_per_min'],
            name="OOM events/min", marker_color=C_OOM_BAR,
            legend="legend2",
            hovertemplate="%{x|%H:%M}<br><b>%{y:,} OOM/min</b><extra></extra>"
        ), row=2, col=1)
    else:
        if x_min is not None and x_max is not None:
            oom_plot = pd.DataFrame({
                'Timestamp': pd.date_range(x_min, x_max, freq='1min'),
                'OOM_per_min': 0,
            })
            fig.add_trace(go.Bar(
                x=oom_plot['Timestamp'], y=oom_plot['OOM_per_min'],
                name="OOM events/min", marker_color=C_OOM_BAR,
                legend="legend2",
                hovertemplate="%{x|%H:%M}<br><b>%{y:,} OOM/min</b><extra></extra>"
            ), row=2, col=1)
        fig.add_annotation(text="No eviction pressure detected",
                           xref="paper", yref="paper",
                           x=0.5, y=0.32, showarrow=False)

    if not metrics_df.empty:
        ev_df = metric_filter(metrics_df, 'Evictions', 'Sum', 'CacheClusterId')
        if not ev_df.empty:
            ev_agg = ev_df.groupby('Timestamp')['Value'].sum().reset_index()
            fig.add_trace(go.Scatter(
                x=ev_agg['Timestamp'], y=ev_agg['Value'],
                name="Evictions (CW)", mode='lines',
                line=dict(**LINE_OPTS, color=C_EVICTION_CW, dash='dash'),
                legend="legend2",
                hovertemplate="%{x|%H:%M}<br><b>%{y:,.0f} evictions</b><extra></extra>"
            ), row=2, col=1)

    # ---- Row 3: Cache Hit Rate ----
    if not metrics_df.empty:
        hr_df = cache_hit_rate_df(metrics_df)
        if not hr_df.empty:
            hr_agg = hr_df.groupby('Timestamp')['Value'].mean().reset_index()
            fig.add_trace(go.Scatter(
                x=hr_agg['Timestamp'], y=hr_agg['Value'],
                name="Cache Hit Rate", mode='lines',
                line=dict(**LINE_OPTS, color=C_HIT_RATE),
                legend="legend3",
                hovertemplate="%{x|%H:%M}<br><b>%{y:.1f}%</b><extra></extra>"
            ), row=3, col=1)
        else:
            fig.add_annotation(text="No CacheHitRate metric", xref="paper", yref="paper",
                               x=0.5, y=0.08, showarrow=False)
    else:
        fig.add_annotation(text="No ElastiCache metrics", xref="paper", yref="paper",
                           x=0.5, y=0.08, showarrow=False)

    # ---- Axes styling ----
    fig.update_yaxes(title_text="ops/sec", row=1, col=1, secondary_y=False,
                     title_font=dict(color=C_THROUGHPUT), tickfont=dict(color=C_THROUGHPUT))
    fig.update_yaxes(title_text="ms", row=1, col=1, secondary_y=True,
                     title_font=dict(color=C_LATENCY), tickfont=dict(color=C_LATENCY),
                     showgrid=False)
    fig.update_yaxes(title_text="OOM/min", row=2, col=1,
                     title_font=dict(color=C_OOM_BAR), tickfont=dict(color=C_OOM_BAR))
    fig.update_yaxes(title_text="%", row=3, col=1,
                     title_font=dict(color=C_HIT_RATE), tickfont=dict(color=C_HIT_RATE))
    if x_min is not None and x_max is not None:
        for r in range(1, 4):
            fig.update_xaxes(range=[x_min, x_max], row=r, col=1)
    fig.update_xaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)

    fig.update_layout(
        **LAYOUT_BASE, height=880,
        legend =dict(**LEGEND_H, x=0.5, y=0.64),
        legend2=dict(**LEGEND_H, x=0.5, y=0.30),
        legend3=dict(**LEGEND_H, x=0.5, y=-0.04),
    )
    return fig


# ------------------------------------------------------------------ #
#  GROUP 2 — Infrastructure figure                                     #
# ------------------------------------------------------------------ #

def build_infra_figure(ecs_df, metrics_df, cluster_id, config, x_min=None, x_max=None):
    """Build a 4-row figure: CPU, Network TX, ECS Memory, ElastiCache Memory.

    Returns a Plotly Figure ready for ``to_html()``.
    """
    config = config or {}

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=False,
        vertical_spacing=0.15,
        subplot_titles=(
            "ECS Load Generator — CPU (%)",
            "ECS Load Generator — Network TX (KB/min)",
            "ECS Load Generator — Memory (MB)",
            "ElastiCache Memory Usage (%)",
        ),
        row_heights=[0.25, 0.25, 0.25, 0.25],
    )

    # ---- Row 1: ECS CPU + EngineCPU overlay ----
    if not ecs_df.empty:
        cpu_df = metric_filter(ecs_df, 'CPUUtilization', 'Average')
        if cpu_df.empty:
            cpu_df = metric_filter(ecs_df, 'TaskCpuUtilization', 'Average')
        if not cpu_df.empty:
            for dim, group in cpu_df.groupby('Dimensions'):
                fig.add_trace(go.Scatter(
                    x=group['Timestamp'], y=group['Value'],
                    name=f"CPU – {shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**LINE_OPTS, color=C_CPU_ECS),
                    legend="legend",
                    hovertemplate="%{x|%H:%M}<br><b>%{y:.1f}%</b><extra></extra>"
                ), row=1, col=1)
        else:
            fig.add_annotation(text="No ECS CPU Metrics", xref="paper", yref="paper",
                               x=0.5, y=0.75, showarrow=False)
    else:
        fig.add_annotation(text="No CPU Data", xref="paper", yref="paper",
                           x=0.5, y=0.75, showarrow=False)

    if not metrics_df.empty:
        eng_cpu_df = metric_filter(metrics_df, 'EngineCPUUtilization', 'Average', 'CacheClusterId')
        if not eng_cpu_df.empty:
            for dim, group in eng_cpu_df.groupby('Dimensions'):
                fig.add_trace(go.Scatter(
                    x=group['Timestamp'], y=group['Value'],
                    name=f"EngineCPU – {shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**LINE_OPTS, color=C_ENGINE_CPU, dash='dot'),
                    legend="legend",
                    hovertemplate="%{x|%H:%M}<br><b>%{y:.1f}%</b><extra></extra>"
                ), row=1, col=1)

    # ---- Row 2: ECS Network TX + ElastiCache NetworkBytesOut overlay ----
    if not ecs_df.empty:
        tx_df = metric_filter(ecs_df, 'NetworkTxBytes', 'Sum')
        if not tx_df.empty:
            tx_agg = tx_df.groupby('Timestamp')['Value'].sum().reset_index()
            tx_agg['Value'] = tx_agg['Value'] / 1024.0
            fig.add_trace(go.Scatter(
                x=tx_agg['Timestamp'], y=tx_agg['Value'],
                name="Network TX – loadgen", mode='lines',
                line=dict(**LINE_OPTS, color=C_NET_TX_ECS),
                legend="legend2",
                hovertemplate="%{x|%H:%M}<br><b>%{y:.1f} KB/min</b><extra></extra>"
            ), row=2, col=1)
        else:
            fig.add_annotation(text="No Network TX Metrics", xref="paper", yref="paper",
                               x=0.5, y=0.5, showarrow=False)
    else:
        fig.add_annotation(text="No ECS Network TX Data", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False)

    if not metrics_df.empty:
        ec_tx_df = metric_filter(metrics_df, 'NetworkBytesOut', 'Sum', 'CacheClusterId')
        if not ec_tx_df.empty:
            ec_tx_agg = ec_tx_df.groupby('Timestamp')['Value'].sum().reset_index()
            ec_tx_agg['Value'] = ec_tx_agg['Value'] / 1024.0
            fig.add_trace(go.Scatter(
                x=ec_tx_agg['Timestamp'], y=ec_tx_agg['Value'],
                name="Network TX – cache", mode='lines',
                line=dict(**LINE_OPTS, color=C_NET_TX_CACHE, dash='dot'),
                legend="legend2",
                hovertemplate="%{x|%H:%M}<br><b>%{y:.1f} KB/min</b><extra></extra>"
            ), row=2, col=1)

    # ---- Row 3: ECS Memory (MB) ----
    if not ecs_df.empty:
        ecs_mem_df = metric_filter(ecs_df, 'MemoryUtilized', 'Average')
        if ecs_mem_df.empty:
            ecs_mem_df = metric_filter(ecs_df, 'ContainerMemoryUtilized', 'Average')
        if not ecs_mem_df.empty:
            mem_agg = ecs_mem_df.groupby('Timestamp')['Value'].sum().reset_index()
            if mem_agg['Value'].max() > 10000:
                mem_agg['Value'] = mem_agg['Value'] / (1024 * 1024)
            fig.add_trace(go.Scatter(
                x=mem_agg['Timestamp'], y=mem_agg['Value'],
                name="ECS Mem – loadgen", mode='lines',
                line=dict(**LINE_OPTS, color=C_ECS_MEM),
                legend="legend3",
                hovertemplate="%{x|%H:%M}<br><b>%{y:.1f} MB</b><extra></extra>"
            ), row=3, col=1)
        else:
            fig.add_annotation(text="No ECS Memory Metrics", xref="paper", yref="paper",
                               x=0.5, y=0.38, showarrow=False)
    else:
        fig.add_annotation(text="No ECS Memory Data", xref="paper", yref="paper",
                           x=0.5, y=0.38, showarrow=False)

    # ---- Row 4: ElastiCache Memory Usage (%) ----
    if not metrics_df.empty:
        mem_df = metric_filter(metrics_df, 'DatabaseMemoryUsageCountedForEvictPercentage', 'Average')
        if mem_df.empty:
            mem_df = metric_filter(metrics_df, 'DatabaseCapacityUsageCountedForEvictPercentage', 'Average')
        if not mem_df.empty:
            keep_dims = select_mem_dims(mem_df['Dimensions'], config.get('node_count', 1))
            mem_df = mem_df[mem_df['Dimensions'].isin(keep_dims)]
            for i, (dim, group) in enumerate(mem_df.groupby('Dimensions')):
                fig.add_trace(go.Scatter(
                    x=group['Timestamp'], y=group['Value'],
                    name=f"Mem – {shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**LINE_OPTS, color=MEM_COLORS[i % len(MEM_COLORS)]),
                    legend="legend4",
                    hovertemplate="%{x|%H:%M}<br><b>%{y:.2f}%</b><extra></extra>"
                ), row=4, col=1)
        else:
            fig.add_annotation(text="No Memory Metrics", xref="paper", yref="paper",
                               x=0.5, y=0.12, showarrow=False)
    else:
        fig.add_annotation(text="No CloudWatch Metrics", xref="paper", yref="paper",
                           x=0.5, y=0.12, showarrow=False)

    # ---- Axes styling ----
    fig.update_yaxes(title_text="%",      row=1, col=1)
    fig.update_yaxes(title_text="KB/min", row=2, col=1,
                     title_font=dict(color=C_NET_TX_ECS), tickfont=dict(color=C_NET_TX_ECS))
    fig.update_yaxes(title_text="MB",     row=3, col=1,
                     title_font=dict(color=C_ECS_MEM), tickfont=dict(color=C_ECS_MEM))
    fig.update_yaxes(title_text="%",      row=4, col=1)
    fig.update_xaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)
    if x_min is not None and x_max is not None:
        for r in range(1, 5):
            fig.update_xaxes(range=[x_min, x_max], row=r, col=1)
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)
    fig.update_layout(
        **LAYOUT_BASE, height=1300,
        legend =dict(**LEGEND_H, x=0.5, y=0.83),
        legend2=dict(**LEGEND_H, x=0.5, y=0.56),
        legend3=dict(**LEGEND_H, x=0.5, y=0.27),
        legend4=dict(**LEGEND_H, x=0.5, y=-0.03),
    )
    return fig


# ------------------------------------------------------------------ #
#  GROUP 3 — ElastiCache Deep-Dive figure                              #
# ------------------------------------------------------------------ #

def build_elasticache_deep_dive_figure(metrics_df, cluster_id, config=None, x_min=None, x_max=None):
    """Build a 4-row deep-dive figure for configuration comparison.

    Rows: CPU Credits | Command Latency | Network Throttling | Connections & Fragmentation.
    """
    config = config or {}

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=False,
        vertical_spacing=0.14,
        specs=[
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
            [{"secondary_y": True}],
        ],
        subplot_titles=(
            "CPU Credit Balance & Usage",
            "Server-Side Command Latency (\u00b5s avg)",
            "Network Throttling Events / min",
            "Current Connections & Memory Fragmentation Ratio",
        ),
        row_heights=[0.25, 0.25, 0.25, 0.25],
    )

    if metrics_df.empty:
        fig.add_annotation(text="No ElastiCache metrics available",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(**LAYOUT_BASE, height=1100)
        return fig

    # ---- Row 1: CPU Credits ----
    bal_df = metric_filter(metrics_df, 'CPUCreditBalance', 'Average', 'CacheClusterId')
    use_df = metric_filter(metrics_df, 'CPUCreditUsage',  'Average', 'CacheClusterId')
    if not bal_df.empty:
        bal_agg = bal_df.groupby('Timestamp')['Value'].mean().reset_index()
        fig.add_trace(go.Scatter(
            x=bal_agg['Timestamp'], y=bal_agg['Value'],
            name="Credit Balance", mode='lines',
            line=dict(**LINE_OPTS, color=C_CREDIT_BAL),
            legend="legend",
            hovertemplate="%{x|%H:%M}<br><b>%{y:.1f} credits</b><extra></extra>"
        ), row=1, col=1, secondary_y=False)
    if not use_df.empty:
        use_agg = use_df.groupby('Timestamp')['Value'].mean().reset_index()
        fig.add_trace(go.Scatter(
            x=use_agg['Timestamp'], y=use_agg['Value'],
            name="Credit Usage", mode='lines',
            line=dict(**LINE_OPTS, color=C_CREDIT_USE, dash='dot'),
            legend="legend",
            hovertemplate="%{x|%H:%M}<br><b>%{y:.3f} vCPU·min</b><extra></extra>"
        ), row=1, col=1, secondary_y=True)
    if bal_df.empty and use_df.empty:
        fig.add_annotation(text="No CPU credit data (non-burstable instance?)",
                           xref="paper", yref="paper", x=0.5, y=0.875, showarrow=False)

    # ---- Row 2: Command Latency ----
    get_df = metric_filter(metrics_df, 'GetTypeCmdsLatency',    'Average', 'CacheClusterId')
    set_df = metric_filter(metrics_df, 'SetTypeCmdsLatency',    'Average', 'CacheClusterId')
    str_df = metric_filter(metrics_df, 'StringBasedCmdsLatency','Average', 'CacheClusterId')
    lat_shown = False
    for mdf, label, color, dash in [
        (get_df, "GET latency",    C_LAT_GET, 'solid'),
        (set_df, "SET latency",    C_LAT_SET, 'dot'),
        (str_df, "String latency", C_LAT_STR, 'dash'),
    ]:
        if not mdf.empty:
            agg = mdf.groupby('Timestamp')['Value'].mean().reset_index()
            fig.add_trace(go.Scatter(
                x=agg['Timestamp'], y=agg['Value'],
                name=label, mode='lines',
                line=dict(**LINE_OPTS, color=color, dash=dash),
                legend="legend2",
                hovertemplate="%{x|%H:%M}<br><b>%{y:.1f} \u00b5s</b><extra></extra>"
            ), row=2, col=1)
            lat_shown = True
    if not lat_shown:
        fig.add_annotation(text="No command latency data",
                           xref="paper", yref="paper", x=0.5, y=0.625, showarrow=False)

    # ---- Row 3: Network Throttling ----
    bw_in_df  = metric_filter(metrics_df, 'NetworkBandwidthInAllowanceExceeded',      'Sum', 'CacheClusterId')
    bw_out_df = metric_filter(metrics_df, 'NetworkBandwidthOutAllowanceExceeded',     'Sum', 'CacheClusterId')
    pps_df    = metric_filter(metrics_df, 'NetworkPacketsPerSecondAllowanceExceeded', 'Sum', 'CacheClusterId')
    throttle_shown = False
    for mdf, label, color in [
        (bw_in_df,  "BW In exceeded",  C_THROTTLE_IN),
        (bw_out_df, "BW Out exceeded", C_THROTTLE_OUT),
        (pps_df,    "PPS exceeded",    C_THROTTLE_PPS),
    ]:
        if not mdf.empty:
            agg = mdf.groupby('Timestamp')['Value'].sum().reset_index()
            if agg['Value'].sum() > 0:
                fig.add_trace(go.Scatter(
                    x=agg['Timestamp'], y=agg['Value'],
                    name=label, mode='lines',
                    line=dict(**LINE_OPTS, color=color),
                    legend="legend3",
                    hovertemplate="%{x|%H:%M}<br><b>%{y:,}</b><extra></extra>"
                ), row=3, col=1)
                throttle_shown = True
    if not throttle_shown:
        fig.add_annotation(text="No network throttling detected",
                           xref="paper", yref="paper", x=0.5, y=0.375, showarrow=False)

    # ---- Row 4: Connections & Memory Fragmentation Ratio ----
    conn_df = metric_filter(metrics_df, 'CurrConnections',         'Average', 'CacheClusterId')
    frag_df = metric_filter(metrics_df, 'MemoryFragmentationRatio','Average', 'CacheClusterId')
    if not conn_df.empty:
        conn_agg = conn_df.groupby('Timestamp')['Value'].mean().reset_index()
        fig.add_trace(go.Scatter(
            x=conn_agg['Timestamp'], y=conn_agg['Value'],
            name="Connections", mode='lines',
            line=dict(**LINE_OPTS, color=C_CURR_CONN),
            legend="legend4",
            hovertemplate="%{x|%H:%M}<br><b>%{y:,.0f} conns</b><extra></extra>"
        ), row=4, col=1, secondary_y=False)
    if not frag_df.empty:
        frag_agg = frag_df.groupby('Timestamp')['Value'].mean().reset_index()
        fig.add_trace(go.Scatter(
            x=frag_agg['Timestamp'], y=frag_agg['Value'],
            name="Frag Ratio", mode='lines',
            line=dict(**LINE_OPTS, color=C_MEM_FRAG, dash='dot'),
            legend="legend4",
            hovertemplate="%{x|%H:%M}<br><b>%{y:.2f}x</b><extra></extra>"
        ), row=4, col=1, secondary_y=True)
    if conn_df.empty and frag_df.empty:
        fig.add_annotation(text="No connection/fragmentation data",
                           xref="paper", yref="paper", x=0.5, y=0.125, showarrow=False)

    # ---- Axes styling ----
    fig.update_yaxes(title_text="credits",  row=1, col=1, secondary_y=False,
                     title_font=dict(color=C_CREDIT_BAL), tickfont=dict(color=C_CREDIT_BAL))
    fig.update_yaxes(title_text="vCPU\u00b7min", row=1, col=1, secondary_y=True,
                     title_font=dict(color=C_CREDIT_USE), tickfont=dict(color=C_CREDIT_USE),
                     showgrid=False)
    fig.update_yaxes(title_text="\u00b5s",      row=2, col=1)
    fig.update_yaxes(title_text="events",   row=3, col=1)
    fig.update_yaxes(title_text="conns",    row=4, col=1, secondary_y=False,
                     title_font=dict(color=C_CURR_CONN), tickfont=dict(color=C_CURR_CONN))
    fig.update_yaxes(title_text="ratio",    row=4, col=1, secondary_y=True,
                     title_font=dict(color=C_MEM_FRAG),  tickfont=dict(color=C_MEM_FRAG),
                     showgrid=False)
    fig.update_xaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)
    if x_min is not None and x_max is not None:
        for r in range(1, 5):
            fig.update_xaxes(range=[x_min, x_max], row=r, col=1)
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)

    fig.update_layout(
        **LAYOUT_BASE, height=1100,
        legend =dict(**LEGEND_H, x=0.5, y=0.83),
        legend2=dict(**LEGEND_H, x=0.5, y=0.56),
        legend3=dict(**LEGEND_H, x=0.5, y=0.27),
        legend4=dict(**LEGEND_H, x=0.5, y=-0.03),
    )
    return fig
