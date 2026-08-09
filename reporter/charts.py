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
    client_latency_series, ecs_task_metric_distribution, ecs_task_metric_rows,
    ecs_task_index_map,
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


def _plot_times_with_counts(timestamps, counts):
    return [[time, int(count)] for time, count in zip(_plot_times(timestamps), counts)]


def _task_distribution_hover(unit, value_format=":.1f"):
    return (
        f"%{{customdata[0]}}<br><b>%{{y{value_format}}} {unit}</b>"
        "<br>sources: %{customdata[1]:.0f}<extra></extra>"
    )


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

def _add_ecs_task_latency_traces(fig, ecs_df, row, legend="legend5"):
    """Add ECS task EMF latency percentiles to an existing subplot row."""
    series = client_latency_series(ecs_df)
    traces = (
        ("p50_ms", "ECS task p50", C_LAT_P50, "solid"),
        ("p99_ms", "ECS task p99", C_LAT_P99, "solid"),
        ("p999_ms", "ECS task p99.9", C_LAT_P999, "solid"),
        ("worst_stream_p99_ms", "Worst ECS task p99", C_LAT_WORST99, "dash"),
        ("worst_stream_p999_ms", "Worst ECS task p99.9", C_LAT_WORST999, "dash"),
    )
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
                legend=legend,
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.3f}} ms</b><extra></extra>",
            ), row=row, col=1)
            shown = True
    if not shown:
        _add_empty_panel_annotation(fig, row, "No ECS task latency datapoints")
    return shown


EMPTY_STATE_ANNOTATION_NAME = 'empty-state'


def _add_empty_panel_annotation(fig, row, text):
    """Place an empty-state message in the centre of one subplot row.

    Tagged with ``name=EMPTY_STATE_ANNOTATION_NAME`` so ``build_infra_panels``
    can identify and copy it structurally, rather than by matching the
    message text (which would silently break for any future empty-state
    message that doesn't happen to start with "No ").
    """
    subplot_ref = fig._grid_ref[row - 1][0][0]
    xaxis = subplot_ref.trace_kwargs['xaxis']
    yaxis = subplot_ref.trace_kwargs['yaxis']
    fig.add_annotation(
        text=text,
        name=EMPTY_STATE_ANNOTATION_NAME,
        x=0.5,
        y=0.5,
        xref=f"{xaxis} domain",
        yref=f"{yaxis} domain",
        showarrow=False,
    )


def _legend_id_for_row(row):
    """Return the Plotly legend id dedicated to one infrastructure row."""
    return "legend" if row == 1 else f"legend{row}"


def build_infra_figure(
    ecs_df,
    metrics_df,
    cluster_id,
    config,
    x_min=None,
    x_max=None,
    task_az_map=None,
    task_index_by_id=None,
    elasticache_az=None,
    elasticache_az_source=None,
):
    """Build infrastructure panels with dynamic per-AZ ECS CPU detail.

    Returns a Plotly Figure ready for ``to_html()``.
    """
    config = config or {}
    task_cpu_rows = ecs_task_metric_rows(
        ecs_df, 'TaskCpuUtilization', 'Average', task_az_map=task_az_map
    )
    known_cpu_rows = task_cpu_rows[
        task_cpu_rows['AvailabilityZone'].astype(str) != 'unknown'
    ].copy() if not task_cpu_rows.empty else task_cpu_rows
    chart_task_az = dict(task_az_map or {})
    if not task_cpu_rows.empty:
        chart_task_az.update(
            task_cpu_rows.groupby('TaskId')['AvailabilityZone'].last().astype(str).to_dict()
        )
    chart_task_ids = (
        task_cpu_rows['TaskId'].astype(str).unique()
        if not task_cpu_rows.empty else []
    )
    all_task_ids = sorted({*map(str, chart_task_ids), *map(str, (task_index_by_id or {}).keys())})
    default_task_indexes = ecs_task_index_map(
        all_task_ids,
        task_az_map=chart_task_az,
        elasticache_az=elasticache_az,
    )
    supplied_indexes = task_index_by_id or {}
    used_indexes = set()
    supplied_applied = set()
    for task_id in all_task_ids:
        supplied = supplied_indexes.get(task_id)
        if isinstance(supplied, int) and supplied > 0 and supplied not in used_indexes:
            default_task_indexes[task_id] = supplied
            used_indexes.add(supplied)
            supplied_applied.add(task_id)
    next_index = 1
    for task_id in all_task_ids:
        if task_id in supplied_applied:
            continue
        while next_index in used_indexes:
            next_index += 1
        default_task_indexes[task_id] = next_index
        used_indexes.add(next_index)
    task_index_by_id = default_task_indexes
    az_groups = [
        (az, group.copy())
        for az, group in known_cpu_rows.groupby('AvailabilityZone', sort=False)
    ] if not known_cpu_rows.empty else []
    az_groups.sort(key=lambda item: (
        0 if item[0] == elasticache_az else 1,
        item[0],
    ))
    az_count = len(az_groups)
    network_row = 2 + az_count
    ecs_memory_row = network_row + 1
    cache_memory_row = network_row + 2
    ecs_latency_row = cache_memory_row + 1
    row_count = ecs_latency_row
    subplot_titles = ["ECS Tasks — CPU Across Tasks (%)"]
    for az, group in az_groups:
        title = f"{az} — {group['TaskId'].nunique()} tasks"
        if az == elasticache_az:
            if elasticache_az_source == 'inferred_from_memtier_task_p50_latency':
                title += " — ElastiCache AZ (inferred from memtier task latency)"
            else:
                title += " — ElastiCache AZ"
        subplot_titles.append(title)
    subplot_titles.extend((
        "ECS Tasks — Network TX (KB/min)",
        "ECS Tasks — Memory (MB)",
        "ElastiCache Memory Usage (%)",
        "ECS Task Latency (ms)",
    ))

    fig = make_subplots(
        rows=row_count, cols=1,
        shared_xaxes=False,
        vertical_spacing=min(0.04, 0.24 / max(1, row_count - 1)),
        specs=(
            [[{"secondary_y": True}]]
            + [[{"secondary_y": False}] for _ in az_groups]
            + [
                [{"secondary_y": True}],
                [{"secondary_y": False}],
                [{"secondary_y": False}],
                [{"secondary_y": False}],
            ]
        ),
        subplot_titles=tuple(subplot_titles),
        row_heights=[0.20] + ([0.14] * az_count) + [0.18, 0.16, 0.17, 0.15],
    )

    # ---- Row 1: ECS CPU + EngineCPU overlay ----
    if not ecs_df.empty:
        cpu_dist = ecs_task_metric_distribution(ecs_df, 'TaskCpuUtilization', 'Average')
        if not cpu_dist.empty:
            cpu_custom = _plot_times_with_counts(cpu_dist['Timestamp'], cpu_dist['source_count'])
            for column, label, color, dash in (
                ('avg', 'CPU avg/task', C_CPU_ECS, 'solid'),
                ('median', 'CPU median/task', C_CPU_ECS, 'dash'),
                ('min', 'CPU min task', '#7cb342', 'dot'),
                ('max', 'CPU max task', '#0b8043', 'dashdot'),
            ):
                fig.add_trace(go.Scatter(
                    x=_plot_x(cpu_dist['Timestamp']), y=cpu_dist[column],
                    customdata=cpu_custom,
                    name=label, mode='lines',
                    line=dict(**LINE_OPTS, color=color, dash=dash),
                    legend=_legend_id_for_row(1),
                    hovertemplate=_task_distribution_hover('%'),
                ), row=1, col=1, secondary_y=False)
            fig.add_trace(go.Scatter(
                x=_plot_x(cpu_dist['Timestamp']), y=cpu_dist['source_count'],
                customdata=_plot_times(cpu_dist['Timestamp']),
                name="CPU source count", mode='lines',
                line=dict(**LINE_OPTS, color='#546e7a', dash='dot'),
                legend=_legend_id_for_row(1),
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.0f}} sources</b><extra></extra>"
            ), row=1, col=1, secondary_y=True)
        else:
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
                        legend=_legend_id_for_row(1),
                        hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}}%</b><extra></extra>"
                    ), row=1, col=1, secondary_y=False)
            else:
                _add_empty_panel_annotation(fig, 1, "No ECS CPU Metrics")
    else:
        _add_empty_panel_annotation(fig, 1, "No CPU Data")

    if not metrics_df.empty:
        eng_cpu_df = metric_filter(metrics_df, 'EngineCPUUtilization', 'Average', 'CacheClusterId')
        if not eng_cpu_df.empty:
            keep_dims = select_mem_dims(
                eng_cpu_df['Dimensions'], config.get('node_count', 1)
            )
            eng_cpu_df = eng_cpu_df[eng_cpu_df['Dimensions'].isin(keep_dims)]
            for dim, group in eng_cpu_df.groupby('Dimensions'):
                fig.add_trace(go.Scatter(
                    x=_plot_x(group['Timestamp']), y=group['Value'],
                    customdata=_plot_times(group['Timestamp']),
                    name=f"EngineCPU – {shorten_dim(dim, cluster_id)}", mode='lines',
                    line=dict(**LINE_OPTS, color=C_ENGINE_CPU, dash='dot'),
                    legend=_legend_id_for_row(1),
                    hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}}%</b><extra></extra>"
                ), row=1, col=1, secondary_y=False)

    if not task_cpu_rows.empty:
        fig.add_hline(
            y=85, line_color='#f9ab00', line_dash='dash', line_width=2,
            annotation_text='85% ECS task CPU threshold', annotation_position='top left',
            row=1, col=1, secondary_y=False,
        )
        fig.add_hline(
            y=100, line_color='#d93025', line_dash='dot', line_width=2,
            annotation_text='100% task quota', annotation_position='top right',
            row=1, col=1, secondary_y=False,
        )

    task_colors = (
        '#1a73e8', '#d93025', '#188038', '#9334e6', '#f9ab00', '#00897b',
        '#5f6368', '#e8710a', '#3949ab', '#c2185b', '#00796b', '#6d4c41',
    )
    for az_index, (az, az_rows) in enumerate(az_groups, start=2):
        for task_id, group in az_rows.groupby('TaskId', sort=True):
            group = group.sort_values('Timestamp')
            task_index = task_index_by_id[str(task_id)]
            task_label = str(task_index)
            customdata = [
                [timestamp, task_label]
                for timestamp in _plot_times(group['Timestamp'])
            ]
            fig.add_trace(go.Scatter(
                x=_plot_x(group['Timestamp']), y=group['Value'],
                customdata=customdata,
                name=task_label, mode='lines',
                line=dict(
                    **LINE_OPTS,
                    color=task_colors[((task_index or 1) - 1) % len(task_colors)],
                ),
                legend=_legend_id_for_row(az_index),
                hovertemplate=(
                    "%{customdata[0]}<br>ECS task #%{customdata[1]}"
                    "<br><b>%{y:.1f}%</b><extra></extra>"
                ),
            ), row=az_index, col=1)
        fig.add_hline(
            y=85, line_color='#f9ab00', line_dash='dash', line_width=1,
            row=az_index, col=1,
        )
        fig.add_hline(
            y=100, line_color='#d93025', line_dash='dot', line_width=1,
            row=az_index, col=1,
        )

    # ---- ECS Network TX + ElastiCache NetworkBytesOut overlay ----
    if not ecs_df.empty:
        tx_dist = ecs_task_metric_distribution(ecs_df, 'NetworkTxBytes', 'Sum', value_scale=1 / 1024.0)
        if not tx_dist.empty:
            tx_custom = _plot_times_with_counts(tx_dist['Timestamp'], tx_dist['source_count'])
            for column, label, color, dash in (
                ('sum', 'Network TX total — ECS tasks', C_NET_TX_ECS, 'solid'),
                ('avg', 'Network TX avg/task', '#4db6ac', 'solid'),
                ('median', 'Network TX median/task', '#4db6ac', 'dash'),
                ('min', 'Network TX min task', '#80cbc4', 'dot'),
                ('max', 'Network TX max task', '#00796b', 'dashdot'),
            ):
                fig.add_trace(go.Scatter(
                    x=_plot_x(tx_dist['Timestamp']), y=tx_dist[column],
                    customdata=tx_custom,
                    name=label, mode='lines',
                    line=dict(**LINE_OPTS, color=color, dash=dash),
                    legend=_legend_id_for_row(network_row),
                    hovertemplate=_task_distribution_hover('KB/min'),
                ), row=network_row, col=1, secondary_y=False)
            fig.add_trace(go.Scatter(
                x=_plot_x(tx_dist['Timestamp']), y=tx_dist['source_count'],
                customdata=_plot_times(tx_dist['Timestamp']),
                name="Network TX source count", mode='lines',
                line=dict(**LINE_OPTS, color='#546e7a', dash='dot'),
                legend=_legend_id_for_row(network_row),
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.0f}} sources</b><extra></extra>"
            ), row=network_row, col=1, secondary_y=True)
        else:
            tx_df = metric_filter(ecs_df, 'NetworkTxBytes', 'Sum')
            if not tx_df.empty:
                tx_agg = tx_df.groupby('Timestamp')['Value'].sum().reset_index()
                tx_agg['Value'] = tx_agg['Value'] / 1024.0
                fig.add_trace(go.Scatter(
                    x=_plot_x(tx_agg['Timestamp']), y=tx_agg['Value'],
                    customdata=_plot_times(tx_agg['Timestamp']),
                    name="Network TX — ECS tasks", mode='lines',
                    line=dict(**LINE_OPTS, color=C_NET_TX_ECS),
                    legend=_legend_id_for_row(network_row),
                    hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} KB/min</b><extra></extra>"
                ), row=network_row, col=1, secondary_y=False)
            else:
                _add_empty_panel_annotation(fig, network_row, "No Network TX Metrics")
    else:
        _add_empty_panel_annotation(fig, network_row, "No ECS Network TX Data")

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
                legend=_legend_id_for_row(network_row),
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} KB/min</b><extra></extra>"
            ), row=network_row, col=1, secondary_y=False)

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
                name="ECS task memory", mode='lines',
                line=dict(**LINE_OPTS, color=C_ECS_MEM),
                legend=_legend_id_for_row(ecs_memory_row),
                hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.1f}} MB</b><extra></extra>"
            ), row=ecs_memory_row, col=1)
        else:
            _add_empty_panel_annotation(fig, ecs_memory_row, "No ECS Memory Metrics")
    else:
        _add_empty_panel_annotation(fig, ecs_memory_row, "No ECS Memory Data")

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
                    legend=_legend_id_for_row(cache_memory_row),
                    hovertemplate=f"{ABS_TIME_HOVER}<br><b>%{{y:.2f}}%</b><extra></extra>"
                ), row=cache_memory_row, col=1)
        else:
            _add_empty_panel_annotation(fig, cache_memory_row, "No Memory Metrics")
    else:
        _add_empty_panel_annotation(fig, cache_memory_row, "No CloudWatch Metrics")

    # ---- Final row: ECS task latency from EMF ----
    _add_ecs_task_latency_traces(
        fig, ecs_df, ecs_latency_row, legend=_legend_id_for_row(ecs_latency_row)
    )

    # ---- Axes styling ----
    fig.update_yaxes(title_text="%",      row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="sources", row=1, col=1, secondary_y=True, showgrid=False)
    for az_index in range(2, 2 + az_count):
        fig.update_yaxes(title_text="%", row=az_index, col=1)
    fig.update_yaxes(title_text="KB/min", row=network_row, col=1,
                     title_font=dict(color=C_NET_TX_ECS), tickfont=dict(color=C_NET_TX_ECS),
                     secondary_y=False)
    fig.update_yaxes(title_text="sources", row=network_row, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="MB",     row=ecs_memory_row, col=1,
                     title_font=dict(color=C_ECS_MEM), tickfont=dict(color=C_ECS_MEM))
    fig.update_yaxes(title_text="%",      row=cache_memory_row, col=1)
    fig.update_yaxes(title_text="ms",     row=ecs_latency_row, col=1)
    _set_absolute_xaxes(fig, range(1, row_count + 1), x_min, x_max)
    fig.update_yaxes(showgrid=True, gridcolor='#f0f0f0', zeroline=False)
    layout_base = {
        **LAYOUT_BASE,
        "margin": {**LAYOUT_BASE["margin"], "r": 30, "b": 70},
    }
    fig.update_layout(
        **layout_base,
        height=1280 + (150 * az_count),
        showlegend=False,
    )
    return fig


def _axis_props_for_single_panel(axis):
    """Copy axis presentation without combined-subplot placement fields."""
    props = axis.to_plotly_json()
    for key in ('anchor', 'domain', 'overlaying', 'position', 'side'):
        props.pop(key, None)
    return props


def _legend_item(trace):
    line = getattr(trace, 'line', None)
    return {
        'name': str(trace.name or ''),
        'color': str(getattr(line, 'color', None) or '#5f6368'),
        'dash': str(getattr(line, 'dash', None) or 'solid'),
    }


def build_infra_panels(
    ecs_df,
    metrics_df,
    cluster_id,
    config,
    x_min=None,
    x_max=None,
    task_az_map=None,
    task_index_by_id=None,
    elasticache_az=None,
    elasticache_az_source=None,
):
    """Return equal-height Infrastructure plots with external HTML legends."""
    combined = build_infra_figure(
        ecs_df,
        metrics_df,
        cluster_id,
        config,
        x_min,
        x_max,
        task_az_map=task_az_map,
        task_index_by_id=task_index_by_id,
        elasticache_az=elasticache_az,
        elasticache_az_source=elasticache_az_source,
    )
    panels = []
    row_count = len(combined._grid_ref)
    titles = [
        str(annotation.text)
        for annotation in list(combined.layout.annotations or ())[:row_count]
    ]

    for row in range(1, row_count + 1):
        subplot_refs = combined._grid_ref[row - 1][0]
        primary_ref = subplot_refs[0]
        secondary_ref = subplot_refs[1] if len(subplot_refs) > 1 else None
        primary_yref = primary_ref.trace_kwargs['yaxis']
        secondary_yref = secondary_ref.trace_kwargs['yaxis'] if secondary_ref else None
        panel = make_subplots(
            rows=1,
            cols=1,
            specs=[[{'secondary_y': secondary_ref is not None}]],
        )
        legend_items = []

        for trace in combined.data:
            trace_yref = trace.yaxis or 'y'
            if trace_yref not in {primary_yref, secondary_yref}:
                continue
            trace_json = trace.to_plotly_json()
            trace_json.pop('xaxis', None)
            trace_json.pop('yaxis', None)
            trace_json.pop('legend', None)
            trace_json['showlegend'] = False
            panel_trace = go.Figure(data=[trace_json]).data[0]
            panel.add_trace(
                panel_trace,
                row=1,
                col=1,
                secondary_y=secondary_yref is not None and trace_yref == secondary_yref,
            )
            legend_items.append(_legend_item(trace))

        source_subplot = combined.get_subplot(row, 1)
        panel.update_xaxes(**_axis_props_for_single_panel(source_subplot.xaxis), row=1, col=1)
        panel.update_yaxes(
            **_axis_props_for_single_panel(source_subplot.yaxis),
            row=1,
            col=1,
            secondary_y=False,
        )
        if secondary_ref is not None:
            source_secondary_axis = combined.layout[secondary_ref.layout_keys[1]]
            panel.update_yaxes(
                **_axis_props_for_single_panel(source_secondary_axis),
                row=1,
                col=1,
                secondary_y=True,
            )

        primary_xref = primary_ref.trace_kwargs['xaxis']
        for annotation in list(combined.layout.annotations or ())[row_count:]:
            if annotation.name != EMPTY_STATE_ANNOTATION_NAME:
                continue
            xref = str(annotation.xref or '')
            yref = str(annotation.yref or '')
            if xref not in {primary_xref, f'{primary_xref} domain'} or yref not in {
                primary_yref, f'{primary_yref} domain'
            }:
                continue
            annotation_json = annotation.to_plotly_json()
            annotation_json.update(x=0.5, y=0.5, xref='paper', yref='paper')
            panel.add_annotation(**annotation_json)

        for shape in combined.layout.shapes or ():
            if shape.yref != primary_yref:
                continue
            annotation_text = None
            annotation_position = None
            if row == 1 and float(shape.y0) == 85:
                annotation_text = '85% ECS task CPU threshold'
                annotation_position = 'top left'
            elif row == 1 and float(shape.y0) == 100:
                annotation_text = '100% task quota'
                annotation_position = 'top right'
            panel.add_hline(
                y=shape.y0,
                line_color=shape.line.color,
                line_dash=shape.line.dash,
                line_width=shape.line.width,
                annotation_text=annotation_text,
                annotation_position=annotation_position,
            )

        panel.update_layout(
            **{
                **LAYOUT_BASE,
                'margin': {**LAYOUT_BASE['margin'], 'r': 30, 't': 10, 'b': 55},
            },
            height=320,
            showlegend=False,
        )
        panels.append({
            'title': titles[row - 1] if row - 1 < len(titles) else '',
            'figure': panel,
            'legend_items': legend_items,
        })

    return panels


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
