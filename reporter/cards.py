"""Stat-cards and header-pills HTML builders."""

import pandas as pd

from helpers import metric_filter, cache_hit_rate_df, select_mem_dims


def header_pills(config):
    """Return the header pills HTML for the config badge row."""
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


def stat_cards_html(logs_df, metrics_df, ecs_df, extra_stats=None, config=None):
    """Build the stat-cards grid HTML from all data sources."""
    extra_stats = extra_stats or {}
    config = config or {}
    cards = []  # tuples: (label, value_str, unit, color, tooltip)

    # ---- Memtier throughput / latency / bandwidth ----
    if not logs_df.empty:
        avg_ops = logs_df['Ops/sec'].mean()
        cards.append(('Avg Throughput', f"{avg_ops:,.0f}", 'ops/sec', '#1a56db', ''))
        cards.append(('Peak Throughput', f"{logs_df['Ops/sec'].max():,.0f}", 'ops/sec', '#1a56db', ''))

        if avg_ops > 0:
            cv = logs_df['Ops/sec'].std() / avg_ops * 100
            cv_color = '#188038' if cv < 10 else ('#e8710a' if cv < 25 else '#d93025')
            cards.append(('Throughput CV', f"{cv:.1f}", '%', cv_color,
                          'Coefficient of Variation of ops/sec. Lower = more stable.'))

        cards.append(('Avg Latency', f"{logs_df['Latency (ms)'].mean():.2f}", 'ms', '#e8710a', ''))
        cards.append(('Max Latency', f"{logs_df['Latency (ms)'].max():.2f}", 'ms', '#e8710a', ''))

        if 'Bandwidth_KBs' in logs_df.columns and logs_df['Bandwidth_KBs'].notna().any():
            avg_bw = logs_df['Bandwidth_KBs'].mean()
            if avg_bw >= 1024:
                bw_str, bw_unit = f"{avg_bw / 1024:.2f}", 'MB/s'
            else:
                bw_str, bw_unit = f"{avg_bw:.0f}", 'KB/s'
            cards.append(('Avg Bandwidth', bw_str, bw_unit, '#0097a7',
                          'Average network throughput reported by memtier.'))

        active_min = (logs_df['Timestamp'].max() - logs_df['Timestamp'].min()).total_seconds() / 60
        cards.append(('Active Load Window', f"{active_min:.0f}", 'min', '#5c6bc0',
                      'Time with measurable benchmark traffic (ops/sec stats window). '
                      'Excludes silent key pre-population phase.'))

        process_start_ts = extra_stats.get('process_start_ts')
        prefill_value = 'n/a'
        prefill_tip = (
            'Pre-fill duration could not be derived because the CloudWatch startup '
            'timestamp was not earlier than the reconstructed benchmark start.'
        )
        if process_start_ts is not None:
            bench_start = logs_df['Timestamp'].min()
            ps = process_start_ts
            if hasattr(ps, 'tzinfo') and ps.tzinfo is not None:
                ps = ps.replace(tzinfo=None)
            bs = bench_start
            if hasattr(bs, 'tzinfo') and bs.tzinfo is not None:
                bs = bs.replace(tzinfo=None)
            prefill_min = (bs - ps).total_seconds() / 60
            if prefill_min >= 0:
                prefill_value = f"{prefill_min:.0f}"
                prefill_tip = (
                    'Time memtier spent silently loading keys before benchmark traffic began. '
                    'Scales with keyspace size relative to instance memory.'
                )
        cards.append(('Pre-fill Duration', prefill_value, 'min', '#78909c', prefill_tip))

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

    # ---- Cache Hit Rate (benchmark window) ----
    if not metrics_df.empty and not logs_df.empty:
        hr_df = cache_hit_rate_df(metrics_df)
        if not hr_df.empty:
            bench_start = logs_df['Timestamp'].min()
            hr_ts = hr_df['Timestamp']
            if hr_ts.dt.tz is not None:
                hr_ts = hr_ts.dt.tz_localize(None)
            avg_hr = hr_df[hr_ts >= bench_start]['Value'].mean()
            if pd.isna(avg_hr):
                avg_hr = hr_df['Value'].mean()
            hr_color = '#188038' if avg_hr >= 90 else ('#e8710a' if avg_hr >= 70 else '#d93025')
            cards.append(('Cache Hit Rate', f"{avg_hr:.1f}", '%', hr_color,
                          'Avg Redis CacheHitRate during benchmark window. <70% = significant misses/evictions.'))

    # ---- Total Evictions (CloudWatch) ----
    if not metrics_df.empty:
        ev_df = metric_filter(metrics_df, 'Evictions', 'Sum', 'CacheClusterId')
        if not ev_df.empty:
            total_ev = int(ev_df['Value'].sum())
            ev_color = '#188038' if total_ev == 0 else '#d93025'
            cards.append(('Total Evictions', f"{total_ev:,}", '', ev_color,
                          'Total Redis key evictions reported by CloudWatch. Confirms OOM log data.'))

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

    # ---- Time to first eviction (OOM) ----
    first_eviction_ts = extra_stats.get('first_eviction_ts')
    if first_eviction_ts is not None and not logs_df.empty:
        bench_start = logs_df['Timestamp'].min()
        fets = first_eviction_ts
        if hasattr(fets, 'tzinfo') and fets.tzinfo is not None:
            fets = fets.replace(tzinfo=None)
        delta_min = (fets - bench_start).total_seconds() / 60
        offset_str = f" (+{delta_min:.0f} min)" if delta_min > 0 else ""
        oom_label = f"{fets.strftime('%H:%M')} UTC{offset_str}"
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
