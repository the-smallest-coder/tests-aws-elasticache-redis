"""Stat-cards and header-pills HTML builders."""

from html import escape

import pandas as pd

from helpers import (
    metric_filter,
    cache_hit_rate_df,
    select_mem_dims,
    cloudwatch_eviction_series,
    first_positive_timestamp,
)


def _html(value):
    return escape(str(value), quote=True)


def _format_gib(byte_value):
    try:
        gib = float(byte_value) / (1024 ** 3)
    except (TypeError, ValueError):
        return None
    decimals = 3 if gib < 1 else 2
    text = f"{gib:,.{decimals}f}".rstrip('0').rstrip('.')
    return text or "0"


def _format_usd_hour(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return f"${price:,.3f}".rstrip('0').rstrip('.')


def _format_elapsed(delta):
    """Return compact elapsed text suitable for a stat card."""
    total_seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


def header_pills(config):
    """Return the header pills HTML for the config badge row."""
    config = config or {}
    cluster_mode = str(config.get('cluster_mode', 'false')).lower() == 'true'
    mode_label = 'Cluster Mode' if cluster_mode else 'Non-Cluster'
    node_memory = _format_gib(config.get('node_memory_bytes'))
    redis_hourly = _format_usd_hour(config.get('redis_hourly_usd'))
    items = [
        ('Engine', config.get('engine_type')),
        ('Version', config.get('engine_version')),
        ('Node type', config.get('node_type')),
        ('Node memory', f"{node_memory} GiB" if node_memory else None),
        ('Redis hourly', redis_hourly),
        ('Nodes', config.get('node_count')),
        ('Mode', mode_label),
    ]
    pills = ''.join(
        f"<div class='pill'>{_html(label)}: "
        f"<span>{_html(val)}</span></div>"
        for label, val in items if val
    )
    return f"<div class='pills'>{pills}</div>" if pills else ''


def stat_cards_html(
    memtier_minute_df,
    memtier_totals_df,
    metrics_df,
    ecs_df,
    extra_stats=None,
    config=None,
    cluster_id=None,
):
    """Build the stat-cards grid HTML from all data sources."""
    extra_stats = extra_stats or {}
    config = config or {}
    cards = []  # tuples: (label, value_str, unit, color, tooltip)

    node_memory = _format_gib(config.get('node_memory_bytes'))
    if node_memory:
        cards.append(('Node Memory', node_memory, 'GiB', '#546e7a',
                      'Configured ElastiCache node memory used to compute memtier keyspace.'))

    redis_hourly = _format_usd_hour(config.get('redis_hourly_usd'))
    if redis_hourly:
        cards.append(('Redis Cost', redis_hourly, '/h', '#546e7a',
                      'Hourly Redis node price captured with the run metadata.'))

    # ---- Memtier throughput / latency / bandwidth ----
    if not memtier_minute_df.empty and not memtier_totals_df.empty:
        avg_ops = memtier_totals_df['throughput_avg'].sum()
        cards.append(('Avg Throughput', f"{avg_ops:,.0f}", 'ops/sec', '#1a56db', ''))
        cards.append(('Peak Throughput', f"{memtier_minute_df['throughput_sum'].max():,.0f}", 'ops/sec', '#1a56db', ''))

        if avg_ops > 0:
            cv = memtier_minute_df['throughput_sum'].std() / avg_ops * 100
            cv_color = '#188038' if cv < 10 else ('#e8710a' if cv < 25 else '#d93025')
            cards.append(('Throughput CV', f"{cv:.1f}", '%', cv_color,
                          'Coefficient of Variation of ops/sec. Lower = more stable.'))

        avg_latency = (
            (memtier_totals_df['latency_avg_ms'] * memtier_totals_df['throughput_avg']).sum()
            / memtier_totals_df['throughput_avg'].sum()
        )
        cards.append(('Avg Latency', f"{avg_latency:.2f}", 'ms', '#e8710a', ''))
        cards.append(('Max Latency', f"{memtier_minute_df['latency_max'].max():.2f}", 'ms', '#e8710a', ''))

        total_bw = memtier_totals_df['total_bandwidth_kbs'].sum()
        if total_bw >= 1024:
            bw_str, bw_unit = f"{total_bw / 1024:.2f}", 'MB/s'
        else:
            bw_str, bw_unit = f"{total_bw:.0f}", 'KB/s'
        cards.append(('Total Bandwidth', bw_str, bw_unit, '#0097a7',
                      'Total network throughput reported by memtier totals artifacts.'))

    # ---- ECS CPU / Memory ----
    if not ecs_df.empty:
        cpu_df = metric_filter(ecs_df, 'CPUUtilization', 'Average')
        if not cpu_df.empty:
            cards.append(('Avg ECS CPU', f"{cpu_df['Value'].mean():.1f}", '%', '#188038', ''))
            cards.append(('Peak ECS CPU', f"{cpu_df['Value'].max():.1f}", '%', '#188038', ''))

        mem_used_df = metric_filter(ecs_df, 'MemoryUtilized', 'Average')
        if not mem_used_df.empty:
            peak_mem_mb = mem_used_df['Value'].max()
            reserved_df = ecs_df[ecs_df['MetricName'] == 'MemoryReserved']
            reserved_mb = reserved_df['Value'].max() if not reserved_df.empty else None
            tip = f"Peak loadgen container memory. Reserved: {reserved_mb:.0f} MB." if reserved_mb else 'Peak loadgen container memory.'
            cards.append(('ECS Mem Peak', f"{peak_mem_mb:.0f}", 'MB', '#0097a7', tip))

        task_df = metric_filter(ecs_df, 'RunningTaskCount', 'Average')
        if not task_df.empty:
            cards.append(('Loadgen Tasks', f"{int(task_df['Value'].max())}", '', '#5c6bc0',
                          'Peak number of concurrent loadgen ECS tasks during the test.'))

    # ---- ElastiCache Memory ----
    if not metrics_df.empty:
        for metric in ('DatabaseMemoryUsageCountedForEvictPercentage',
                       'DatabaseCapacityUsageCountedForEvictPercentage'):
            mem_df = metric_filter(metrics_df, metric, 'Average')
            if not mem_df.empty:
                keep_dims = select_mem_dims(mem_df['Dimensions'], config.get('node_count', 1))
                mem_df = mem_df[mem_df['Dimensions'].isin(keep_dims)]
                max_mem = mem_df['Value'].max()
                headroom = 100.0 - max_mem
                headroom_color = '#188038' if headroom > 10 else ('#e8710a' if headroom >= 0 else '#d93025')
                cards.append(('Avg Memory', f"{mem_df['Value'].mean():.2f}", '%', '#a142f4', ''))
                cards.append(('Max Memory', f"{max_mem:.2f}", '%', '#a142f4', ''))
                cards.append(('Mem Headroom', f"{headroom:+.1f}", '%', headroom_color,
                              '100 − peak memory usage. Negative means eviction territory.'))
                break

    # ---- Redis Engine CPU ----
    if not metrics_df.empty:
        eng_cpu_df = metric_filter(metrics_df, 'EngineCPUUtilization', 'Average', 'CacheClusterId')
        if not eng_cpu_df.empty:
            cards.append(('Engine CPU Peak', f"{eng_cpu_df['Value'].max():.1f}", '%', '#e65100',
                          'Peak Redis engine thread CPU utilization. More precise than host CPU for burstable T-type instances.'))

    # ---- Cache Hit Rate (report window) ----
    if not metrics_df.empty:
        hr_df = cache_hit_rate_df(metrics_df)
        if not hr_df.empty:
            avg_hr = hr_df['Value'].mean()
            hr_color = '#188038' if avg_hr >= 90 else ('#e8710a' if avg_hr >= 70 else '#d93025')
            cards.append(('Cache Hit Rate', f"{avg_hr:.1f}", '%', hr_color,
                          'Avg Redis CacheHitRate in the report window. <70% = significant misses/evictions.'))

    # ---- Total Evictions (CloudWatch) ----
    if not metrics_df.empty:
        ev_df = cloudwatch_eviction_series(metrics_df, cluster_id)
        if not ev_df.empty:
            total_ev = int(ev_df['Value'].sum())
            ev_color = '#188038' if total_ev == 0 else '#d93025'
            cards.append(('Total Evictions', f"{total_ev:,}", '', ev_color,
                          'Total Redis key evictions reported by CloudWatch.'))

    # ---- FreeableMemory minimum ----
    if not metrics_df.empty:
        free_df = metric_filter(metrics_df, 'FreeableMemory', 'Minimum', 'CacheClusterId')
        if not free_df.empty:
            min_free_mb = free_df['Value'].min() / (1024 * 1024)
            free_color = '#188038' if min_free_mb > 50 else ('#e8710a' if min_free_mb > 10 else '#d93025')
            cards.append(('Min Free Mem', f"{min_free_mb:.0f}", 'MB', free_color,
                          'Minimum FreeableMemory observed. Near-zero = cache fully saturated.'))

    # ---- Peak key count ----
    if not metrics_df.empty:
        items_df = metric_filter(metrics_df, 'CurrItems', 'Maximum', 'CacheClusterId')
        if not items_df.empty:
            cards.append(('Peak Key Count', f"{int(items_df['Value'].max()):,}", '', '#546e7a',
                          'Peak number of keys in cache. Drops below expected if eviction removed keys.'))

    # ---- CloudWatch eviction timing ----
    ev_df = cloudwatch_eviction_series(metrics_df, cluster_id)
    first_eviction_ts = first_positive_timestamp(ev_df)
    if first_eviction_ts is not None:
        fets = first_eviction_ts
        if hasattr(fets, 'tzinfo') and fets.tzinfo is not None:
            fets = fets.replace(tzinfo=None)
        first_message_ts = extra_stats.get('first_message_ts')
        if first_message_ts is not None:
            fmts = pd.Timestamp(first_message_ts)
            if fmts.tzinfo is not None:
                fmts = fmts.tz_convert(None)
            eviction_label = _format_elapsed(fets - fmts)
            eviction_tip = (
                'Elapsed time from report start to the first positive CloudWatch '
                f"Evictions datapoint. Event timestamp: {fets.strftime('%Y-%m-%d %H:%M:%S')} UTC."
            )
        else:
            eviction_label = 'Unavailable'
            eviction_tip = (
                'Report start timestamp unavailable; first positive CloudWatch '
                f"Evictions datapoint occurred at {fets.strftime('%Y-%m-%d %H:%M:%S')} UTC."
            )
        eviction_color = '#d93025'
    else:
        eviction_label, eviction_color = 'None', '#188038'
        eviction_tip = 'No positive CloudWatch Evictions datapoints in the report window.'
    cards.append(('First Eviction', eviction_label, '', eviction_color, eviction_tip))

    # ---- Memtier OOM rejections ----
    oom_df = extra_stats.get('oom_df')
    oom_count = int(oom_df['OOM_events'].sum()) if oom_df is not None and not oom_df.empty else 0
    oom_color = '#d93025' if oom_count else '#188038'
    cards.append(('OOM Rejections', f"{oom_count:,}", '', oom_color,
                  'Count of memtier log events containing -OOM command not allowed.'))

    first_oom_ts = extra_stats.get('first_oom_rejection_ts')
    if first_oom_ts is not None:
        fots = first_oom_ts
        if hasattr(fots, 'tzinfo') and fots.tzinfo is not None:
            fots = fots.replace(tzinfo=None)
        oom_label = f"{fots.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        oom_tip = 'Absolute timestamp of the first memtier -OOM command rejection.'
    else:
        oom_label = 'None'
        oom_tip = 'No memtier -OOM command rejections in the report window.'
    cards.append(('First OOM Rejection', oom_label, '', oom_color, oom_tip))

    if not cards:
        return ''

    html = ''.join(
        f"<div class='card' title='{_html(tip)}'>"
        f"<div class='card-label'>{_html(label)}</div>"
        f"<div class='card-value' style='color:{_html(color)}'>{_html(val)}"
        f"<span class='card-unit'>{_html(unit)}</span></div>"
        f"</div>"
        for label, val, unit, color, tip in cards
    )
    return f"<div class='cards'>{html}</div>"
