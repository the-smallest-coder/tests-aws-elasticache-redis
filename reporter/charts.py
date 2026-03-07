"""Plotly figure builders for memtier benchmark and infrastructure chart groups."""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from helpers import (
    LAYOUT_BASE, LEGEND_H, LINE_OPTS,
    C_THROUGHPUT, C_LATENCY, C_OOM_BAR, C_EVICTION_CW, C_HIT_RATE,
    C_CPU_ECS, C_ENGINE_CPU, C_NET_TX_ECS, C_NET_TX_CACHE, C_ECS_MEM,
    MEM_COLORS,
    metric_filter, shorten_dim, select_mem_dims,
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
        hr_df = metric_filter(metrics_df, 'CacheHitRate', 'Average', 'CacheClusterId')
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

def build_infra_figure(ecs_df, metrics_df, cluster_id, config):
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
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)
    for r in range(1, 5):
        fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.03), row=r, col=1)

    fig.update_layout(
        **LAYOUT_BASE, height=1300,
        legend =dict(**LEGEND_H, x=0.5, y=0.83),
        legend2=dict(**LEGEND_H, x=0.5, y=0.56),
        legend3=dict(**LEGEND_H, x=0.5, y=0.27),
        legend4=dict(**LEGEND_H, x=0.5, y=-0.03),
    )
    return fig
