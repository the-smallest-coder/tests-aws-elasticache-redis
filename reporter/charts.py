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
    C_LAT_P50, C_LAT_P99, C_LAT_P999, C_LAT_WORST99, C_LAT_WORST999,
    metric_filter, cache_hit_rate_df, shorten_dim, select_mem_dims, cloudwatch_eviction_series,
    client_latency_series,
)

ABS_TIME_HOVER = "%{customdata}"


def _normalize_timestamp(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def _format_timestamp(value):
    ts = _normalize_timestamp(value)
    if ts.microsecond:
        return f"{ts.strftime('%Y-%m-%d %H:%M:%S')}.{ts.microsecond:06d} UTC"
    return f"{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC"


def _epoch_ms(value):
    return int(_normalize_timestamp(value).value // 1_000_000)


def _plot_x(values):
    return [_epoch_ms(value) for value in values]


def _plot_times(values):
    return [_format_timestamp(value) for value in values]


def _set_absolute_xaxes(fig, rows, x_min, x_max):
    axis = {
        "type": "linear",
        "showgrid": True,
        "gridcolor": "#f0f0f0",
        "zeroline": False,
    }
    if x_min is not None and x_max is not None:
        tickvals = [_epoch_ms(x_min), _epoch_ms(x_max)]
        axis.update({
            "range": tickvals,
            "tickmode": "array",
            "tickvals": tickvals,
            "ticktext": [_format_timestamp(x_min), _format_timestamp(x_max)],
        })
    for row in rows:
        fig.update_xaxes(row=row, col=1, **axis)


# ------------------------------------------------------------------ #
#  GROUP 1 — Memtier Benchmark figure                                  #
# ------------------------------------------------------------------ #

def build_memtier_figure(memtier_df, oom_df, metrics_df, x_min, x_max):
    """Build memtier aggregate views plus eviction pressure and cache hit rate.

    Returns a Plotly Figure ready for ``to_html()``.
    """
    has_oom_rejections = not oom_df.empty and oom_df['OOM_events'].sum() > 0

    fig = make_subplots(
        rows=5, cols=1,
        shared_xaxes=False,
        vertical_spacing=0.11,
        specs=[
            [{"secondary_y": True}],
            [{"secondary_y": True}],
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
        ],
        subplot_titles=(
            "Overall Load",
            "Task Distribution",
            "Task Extremes",
            "Memory Pressure Signals",
            "Cache Hit Rate (%)",
        ),
        row_heights=[0.24, 0.24, 0.20, 0.17, 0.15],
    )

    # ---- Rows 1-3: Meaningful cross-task memtier aggregates ----
    if not memtier_df.empty:
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df['Timestamp']), y=memtier_df['throughput_sum'],
            customdata=_plot_times(memtier_df['Timestamp']),
            name="Throughput", mode='lines',
            line=dict(**LINE_OPTS, color=C_THROUGHPUT),
            legend="legend",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:,.0f}} ops/sec</b><extra></extra>"
        ), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df['Timestamp']), y=memtier_df['latency_weighted_avg'],
            customdata=_plot_times(memtier_df['Timestamp']),
            name="Latency", mode='lines',
            line=dict(**LINE_OPTS, color=C_LATENCY, dash='dot'),
            legend="legend",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.2f}} ms</b><extra></extra>"
        ), row=1, col=1, secondary_y=True)

        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["throughput_p90"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Throughput p10-p90", mode="lines", line=dict(width=0),
            showlegend=True, legend="legend2",
            hoverinfo="skip",
        ), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["throughput_p10"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Throughput p10-p90", mode="lines", line=dict(width=0),
            fill="tonexty", fillcolor="rgba(31,119,180,0.18)",
            showlegend=False,
            hoverinfo="skip",
        ), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["throughput_median"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Throughput median", mode="lines",
            line=dict(**LINE_OPTS, color=C_THROUGHPUT),
            legend="legend4",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:,.0f}} ops/sec</b><extra></extra>",
        ), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["throughput_avg"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Throughput average", mode="lines",
            line=dict(width=1, color=C_THROUGHPUT, dash="dash"),
            legend="legend2",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:,.0f}} ops/sec</b><extra></extra>",
        ), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["latency_p90"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Latency p10-p90", mode="lines", line=dict(width=0),
            showlegend=True, legend="legend2",
            hoverinfo="skip",
        ), row=2, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["latency_p10"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Latency p10-p90", mode="lines", line=dict(width=0),
            fill="tonexty", fillcolor="rgba(214,39,40,0.16)",
            showlegend=False,
            hoverinfo="skip",
        ), row=2, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["latency_median"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Latency median", mode="lines",
            line=dict(**LINE_OPTS, color=C_LATENCY),
            legend="legend2",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.2f}} ms</b><extra></extra>",
        ), row=2, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(
            x=_plot_x(memtier_df["Timestamp"]), y=memtier_df["latency_avg"],
            customdata=_plot_times(memtier_df["Timestamp"]),
            name="Latency average", mode="lines",
            line=dict(width=1, color=C_LATENCY, dash="dash"),
            legend="legend2",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.2f}} ms</b><extra></extra>",
        ), row=2, col=1, secondary_y=True)

        for column, name, color, secondary in (
            ("throughput_min", "Throughput min", C_THROUGHPUT, False),
            ("throughput_max", "Throughput max", C_THROUGHPUT, False),
            ("latency_min", "Latency min", C_LATENCY, True),
            ("latency_max", "Latency max", C_LATENCY, True),
        ):
            fig.add_trace(go.Scatter(
                x=_plot_x(memtier_df["Timestamp"]), y=memtier_df[column],
                customdata=_plot_times(memtier_df["Timestamp"]),
                name=name, mode="lines",
                line=dict(**LINE_OPTS, color=color, dash="dash" if "min" in name else "solid"),
                legend="legend3",
                hovertemplate=(
                    f"{ABS_TIME_HOVER}<br><b>%{{y:,.0f}} ops/sec</b><extra></extra>"
                    if not secondary else f"{ABS_TIME_HOVER}<br><b>%{{y:.2f}} ms</b><extra></extra>"
                ),
            ), row=3, col=1, secondary_y=secondary)

    # ---- Row 4: OOM bar + CW Evictions overlay ----
    if has_oom_rejections:
        oom_plot = oom_df.copy()
        if x_min is not None and x_max is not None:
            oom_plot = oom_plot[(oom_plot['Timestamp'] >= x_min) & (oom_plot['Timestamp'] <= x_max)]
        fig.add_trace(go.Bar(
            x=_plot_x(oom_plot['Timestamp']), y=oom_plot['OOM_events'],
            customdata=_plot_times(oom_plot['Timestamp']),
            name="OOM rejections", marker_color=C_OOM_BAR,
            legend="legend2",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:,}} OOM rejections</b><extra></extra>"
        ), row=4, col=1)

    ev_df = cloudwatch_eviction_series(metrics_df)
    if not ev_df.empty:
        fig.add_trace(go.Scatter(
            x=_plot_x(ev_df['Timestamp']), y=ev_df['Value'],
            customdata=_plot_times(ev_df['Timestamp']),
            name="Evictions (CW)", mode='lines',
            line=dict(**LINE_OPTS, color=C_EVICTION_CW, dash='dash'),
            legend="legend4",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:,.0f}} evictions</b><extra></extra>"
        ), row=4, col=1)
    if not has_oom_rejections and (ev_df.empty or ev_df['Value'].sum() == 0):
        fig.add_annotation(text="No OOM rejections or CloudWatch evictions detected",
                           xref="paper", yref="paper",
                           x=0.5, y=0.19, showarrow=False)

    # ---- Row 5: Cache Hit Rate ----
    if not metrics_df.empty:
        hr_df = cache_hit_rate_df(metrics_df)
        if not hr_df.empty:
            hr_agg = hr_df.groupby('Timestamp')['Value'].mean().reset_index()
            fig.add_trace(go.Scatter(
                x=_plot_x(hr_agg['Timestamp']), y=hr_agg['Value'],
                customdata=_plot_times(hr_agg['Timestamp']),
                name="Cache Hit Rate", mode='lines',
                line=dict(**LINE_OPTS, color=C_HIT_RATE),
                legend="legend5",
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}}%</b><extra></extra>"
            ), row=5, col=1)
        else:
            fig.add_annotation(text="No CacheHitRate metric", xref="paper", yref="paper",
                               x=0.5, y=0.04, showarrow=False)
    else:
        fig.add_annotation(text="No ElastiCache metrics", xref="paper", yref="paper",
                           x=0.5, y=0.04, showarrow=False)

    # ---- Axes styling ----
    fig.update_yaxes(title_text="ops/sec", row=1, col=1, secondary_y=False,
                     title_font=dict(color=C_THROUGHPUT), tickfont=dict(color=C_THROUGHPUT))
    fig.update_yaxes(title_text="ms", row=1, col=1, secondary_y=True,
                     title_font=dict(color=C_LATENCY), tickfont=dict(color=C_LATENCY),
                     showgrid=False)
    for row in (2, 3):
        fig.update_yaxes(title_text="ops/sec", row=row, col=1, secondary_y=False,
                         title_font=dict(color=C_THROUGHPUT), tickfont=dict(color=C_THROUGHPUT))
        fig.update_yaxes(title_text="ms", row=row, col=1, secondary_y=True,
                         title_font=dict(color=C_LATENCY), tickfont=dict(color=C_LATENCY),
                         showgrid=False)
    fig.update_yaxes(title_text="events", row=4, col=1,
                     title_font=dict(color=C_OOM_BAR), tickfont=dict(color=C_OOM_BAR))
    fig.update_yaxes(title_text="%", row=5, col=1,
                     title_font=dict(color=C_HIT_RATE), tickfont=dict(color=C_HIT_RATE))
    _set_absolute_xaxes(fig, range(1, 6), x_min, x_max)
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)

    fig.update_layout(
        **LAYOUT_BASE, height=1380,
        legend =dict(**LEGEND_H, x=0.5, y=0.81),
        legend2=dict(**LEGEND_H, x=0.5, y=0.58),
        legend3=dict(**LEGEND_H, x=0.5, y=0.37),
        legend4=dict(**LEGEND_H, x=0.5, y=0.18),
        legend5=dict(**LEGEND_H, x=0.5, y=-0.04),
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
                    x=_plot_x(group['Timestamp']), y=group['Value'],
                    customdata=_plot_times(group['Timestamp']),
                    name=f"CPU – {shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**LINE_OPTS, color=C_CPU_ECS),
                    legend="legend",
                    hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}}%</b><extra></extra>"
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
                    x=_plot_x(group['Timestamp']), y=group['Value'],
                    customdata=_plot_times(group['Timestamp']),
                    name=f"EngineCPU – {shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**LINE_OPTS, color=C_ENGINE_CPU, dash='dot'),
                    legend="legend",
                    hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}}%</b><extra></extra>"
                ), row=1, col=1)

    # ---- Row 2: ECS Network TX + ElastiCache NetworkBytesOut overlay ----
    if not ecs_df.empty:
        tx_df = metric_filter(ecs_df, 'NetworkTxBytes', 'Sum')
        if not tx_df.empty:
            tx_agg = tx_df.groupby('Timestamp')['Value'].sum().reset_index()
            tx_agg['Value'] = tx_agg['Value'] / 1024.0
            fig.add_trace(go.Scatter(
                x=_plot_x(tx_agg['Timestamp']), y=tx_agg['Value'],
                customdata=_plot_times(tx_agg['Timestamp']),
                name="Network TX – loadgen", mode='lines',
                line=dict(**LINE_OPTS, color=C_NET_TX_ECS),
                legend="legend2",
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} KB/min</b><extra></extra>"
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
                x=_plot_x(ec_tx_agg['Timestamp']), y=ec_tx_agg['Value'],
                customdata=_plot_times(ec_tx_agg['Timestamp']),
                name="Network TX – cache", mode='lines',
                line=dict(**LINE_OPTS, color=C_NET_TX_CACHE, dash='dot'),
                legend="legend2",
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} KB/min</b><extra></extra>"
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
                x=_plot_x(mem_agg['Timestamp']), y=mem_agg['Value'],
                customdata=_plot_times(mem_agg['Timestamp']),
                name="ECS Mem – loadgen", mode='lines',
                line=dict(**LINE_OPTS, color=C_ECS_MEM),
                legend="legend3",
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} MB</b><extra></extra>"
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
                    x=_plot_x(group['Timestamp']), y=group['Value'],
                    customdata=_plot_times(group['Timestamp']),
                    name=f"Mem – {shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**LINE_OPTS, color=MEM_COLORS[i % len(MEM_COLORS)]),
                    legend="legend4",
                    hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.2f}}%</b><extra></extra>"
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
    _set_absolute_xaxes(fig, range(1, 5), x_min, x_max)
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

def build_client_latency_figure(ecs_df, x_min=None, x_max=None):
    """Build the ECS load-generator EMF client latency percentile figure."""
    fig = make_subplots(
        rows=1, cols=1,
        subplot_titles=("Client Latency",),
    )

    series = client_latency_series(ecs_df)
    traces = [
        ("p50_ms", "p50", C_LAT_P50, "solid"),
        ("p99_ms", "p99", C_LAT_P99, "solid"),
        ("p999_ms", "p99.9", C_LAT_P999, "solid"),
        ("worst_stream_p99_ms", "worst_stream_p99", C_LAT_WORST99, "dash"),
        ("worst_stream_p999_ms", "worst_stream_p999", C_LAT_WORST999, "dash"),
    ]
    shown = False
    if not series.empty:
        for column, label, color, dash in traces:
            points = series[["Timestamp", column]].dropna()
            if points.empty:
                continue
            fig.add_trace(go.Scatter(
                x=_plot_x(points["Timestamp"]), y=points[column],
                customdata=_plot_times(points["Timestamp"]),
                name=label, mode="lines",
                line=dict(**LINE_OPTS, color=color, dash=dash),
                legend="legend",
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.3f}} ms</b><extra></extra>",
            ), row=1, col=1)
            shown = True

    if not shown:
        fig.add_annotation(
            text="No ECS client latency datapoints",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
        )

    fig.update_yaxes(title_text="ms", showgrid=True, gridcolor="#f0f0f0", zeroline=False)
    _set_absolute_xaxes(fig, [1], x_min, x_max)
    fig.update_layout(
        **LAYOUT_BASE, height=420,
        legend=dict(**LEGEND_H, x=0.5, y=-0.18),
    )
    return fig


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
            x=_plot_x(bal_agg['Timestamp']), y=bal_agg['Value'],
            customdata=_plot_times(bal_agg['Timestamp']),
            name="Credit Balance", mode='lines',
            line=dict(**LINE_OPTS, color=C_CREDIT_BAL),
            legend="legend",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} credits</b><extra></extra>"
        ), row=1, col=1, secondary_y=False)
    if not use_df.empty:
        use_agg = use_df.groupby('Timestamp')['Value'].mean().reset_index()
        fig.add_trace(go.Scatter(
            x=_plot_x(use_agg['Timestamp']), y=use_agg['Value'],
            customdata=_plot_times(use_agg['Timestamp']),
            name="Credit Usage", mode='lines',
            line=dict(**LINE_OPTS, color=C_CREDIT_USE, dash='dot'),
            legend="legend",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.3f}} vCPU·min</b><extra></extra>"
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
                x=_plot_x(agg['Timestamp']), y=agg['Value'],
                customdata=_plot_times(agg['Timestamp']),
                name=label, mode='lines',
                line=dict(**LINE_OPTS, color=color, dash=dash),
                legend="legend2",
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} \u00b5s</b><extra></extra>"
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
                    x=_plot_x(agg['Timestamp']), y=agg['Value'],
                    customdata=_plot_times(agg['Timestamp']),
                    name=label, mode='lines',
                    line=dict(**LINE_OPTS, color=color),
                    legend="legend3",
                    hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:,}}</b><extra></extra>"
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
            x=_plot_x(conn_agg['Timestamp']), y=conn_agg['Value'],
            customdata=_plot_times(conn_agg['Timestamp']),
            name="Connections", mode='lines',
            line=dict(**LINE_OPTS, color=C_CURR_CONN),
            legend="legend4",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:,.0f}} conns</b><extra></extra>"
        ), row=4, col=1, secondary_y=False)
    if not frag_df.empty:
        frag_agg = frag_df.groupby('Timestamp')['Value'].mean().reset_index()
        fig.add_trace(go.Scatter(
            x=_plot_x(frag_agg['Timestamp']), y=frag_agg['Value'],
            customdata=_plot_times(frag_agg['Timestamp']),
            name="Frag Ratio", mode='lines',
            line=dict(**LINE_OPTS, color=C_MEM_FRAG, dash='dot'),
            legend="legend4",
            hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.2f}}x</b><extra></extra>"
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
    _set_absolute_xaxes(fig, range(1, 5), x_min, x_max)
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)

    fig.update_layout(
        **LAYOUT_BASE, height=1100,
        legend =dict(**LEGEND_H, x=0.5, y=0.83),
        legend2=dict(**LEGEND_H, x=0.5, y=0.56),
        legend3=dict(**LEGEND_H, x=0.5, y=0.27),
        legend4=dict(**LEGEND_H, x=0.5, y=-0.03),
    )
    return fig
