"""Build a structured summary dict suitable for JSON serialisation and comparison reports."""

from helpers import (
    metric_filter,
    cache_hit_rate_df,
    client_latency_series,
    select_mem_dims,
    cloudwatch_eviction_series,
    first_positive_timestamp,
)
from report_common import GENERATOR_SCHEMA_VERSION


def _safe(val, decimals=None):
    """Return a JSON-safe scalar, rounding floats when requested."""
    if val is None or (isinstance(val, float) and (val != val)):   # NaN check
        return None
    if isinstance(val, float):
        return round(val, decimals) if decimals is not None else val
    if hasattr(val, 'item'):          # numpy scalar
        return val.item()
    if hasattr(val, 'isoformat'):     # datetime / Timestamp
        return val.isoformat()
    return val


def _percentile(series, pct):
    try:
        return _safe(float(series.quantile(pct / 100.0)), 3)
    except Exception:
        return None


def _metric_agg(df, metric_name, stat, dim_prefix=None):
    """Return {avg, min, max, p95, p99} for a metric over its entire time window."""
    sub = metric_filter(df, metric_name, stat, dim_prefix)
    if sub.empty:
        return None
    vals = sub['Value']
    return {
        'avg': _safe(float(vals.mean()), 3),
        'min': _safe(float(vals.min()), 3),
        'max': _safe(float(vals.max()), 3),
        'p95': _percentile(vals, 95),
        'p99': _percentile(vals, 99),
    }


def build_summary(metrics_df, memtier_minute_df, memtier_totals_df, ecs_df, extra_stats, config, cluster_id, time_range):
    """Return a fully-populated summary dict ready for json.dumps().

    Schema
    ------
    {
      "meta": { cluster_id, time_range, engine_type, engine_version, node_type, node_count, cluster_mode },
      "benchmark": { avg_ops, peak_ops, cv_pct, avg_latency_ms, max_latency_ms,
                     avg_bandwidth_kbs },
      "cache_efficiency": { avg_hit_rate_pct, total_evictions, min_freeable_memory_mb, peak_key_count,
                            first_eviction_ts },
      "oom": { rejection_count, first_rejection_ts },
      "engine_cpu": { avg_pct, max_pct, credit_balance_avg, credit_usage_avg },
      "memory": { avg_usage_pct, max_usage_pct, headroom_pct, fragmentation_avg },
      "network": {
          "cache": { avg_out_kbs, avg_in_kbs },
          "throttling": { bw_in_exceeded_total, bw_out_exceeded_total, pps_exceeded_total }
      },
      "latency_server_us": { get_avg, set_avg, string_avg },
      "client_latency": { p50_ms, p99_ms, p999_ms, worst_stream_p99_ms, worst_stream_p999_ms },
      "connections": { avg, max },
      "ecs": { avg_cpu_pct, max_cpu_pct, peak_mem_mb }
    }
    """
    config = config or {}
    extra_stats = extra_stats or {}

    # ------------------------------------------------------------------ #
    #  meta                                                                #
    # ------------------------------------------------------------------ #
    meta = {
        'cluster_id':      cluster_id,
        'time_range':      time_range,
        'engine_type':     config.get('engine_type', ''),
        'engine_version':  config.get('engine_version', ''),
        'node_type':       config.get('node_type', ''),
        'node_memory_bytes': config.get('node_memory_bytes', ''),
        'redis_hourly_usd': config.get('redis_hourly_usd', ''),
        'node_count':      config.get('node_count', ''),
        'cluster_mode':    str(config.get('cluster_mode', 'false')).lower(),
        'generator_schema_version': GENERATOR_SCHEMA_VERSION,
        'source_mode': extra_stats.get('source_mode', ''),
        'memtier_window_source': extra_stats.get('memtier_window_source', 'memtier_log_messages'),
        'artifact_source': extra_stats.get('artifact_source', ''),
    }
    first_message_ts = extra_stats.get('first_message_ts')
    last_message_ts = extra_stats.get('last_message_ts')
    if first_message_ts is not None:
        meta['report_start'] = _safe(first_message_ts)
    if last_message_ts is not None:
        meta['report_end'] = _safe(last_message_ts)

    # ------------------------------------------------------------------ #
    #  benchmark (memtier logs)                                            #
    # ------------------------------------------------------------------ #
    benchmark = {}
    if not memtier_minute_df.empty and not memtier_totals_df.empty:
        ops = memtier_minute_df['throughput_sum']
        lat = memtier_minute_df['latency_weighted_avg']
        avg_ops = float(memtier_totals_df['throughput_avg'].sum())
        benchmark['avg_ops']         = _safe(avg_ops, 1)
        benchmark['peak_ops']        = _safe(float(ops.max()), 1)
        benchmark['cv_pct']          = _safe(float(ops.std() / avg_ops * 100) if avg_ops > 0 else 0.0, 2)
        weighted_latency = (
            memtier_totals_df['latency_avg_ms'] * memtier_totals_df['throughput_avg']
        ).sum() / memtier_totals_df['throughput_avg'].sum()
        benchmark['avg_latency_ms'] = _safe(float(weighted_latency), 3)
        benchmark['max_latency_ms']  = _safe(float(memtier_minute_df['latency_max'].max()), 3)
        benchmark['p95_latency_ms']  = _percentile(lat, 95)
        benchmark['p99_latency_ms']  = _percentile(lat, 99)

        total_bandwidth = float(memtier_totals_df['total_bandwidth_kbs'].sum())
        benchmark['total_bandwidth_kbs'] = _safe(total_bandwidth, 2)
        benchmark['avg_bandwidth_kbs'] = _safe(total_bandwidth, 2)

    # ------------------------------------------------------------------ #
    #  cache_efficiency                                                    #
    # ------------------------------------------------------------------ #
    cache_efficiency = {}
    if not metrics_df.empty:
        # Hit rate
        hr_df = cache_hit_rate_df(metrics_df)
        if not hr_df.empty:
            avg_hr = float(hr_df['Value'].mean())
            cache_efficiency['avg_hit_rate_pct'] = _safe(avg_hr, 2)

        # Evictions
        ev_df = cloudwatch_eviction_series(metrics_df, cluster_id)
        if not ev_df.empty:
            cache_efficiency['total_evictions'] = int(ev_df['Value'].sum())

        # FreeableMemory min (in MB)
        free_df = metric_filter(metrics_df, 'FreeableMemory', 'Minimum', 'CacheClusterId')
        if not free_df.empty:
            cache_efficiency['min_freeable_memory_mb'] = _safe(
                float(free_df['Value'].min()) / (1024 * 1024), 1)

        # Peak key count
        items_df = metric_filter(metrics_df, 'CurrItems', 'Maximum', 'CacheClusterId')
        if not items_df.empty:
            cache_efficiency['peak_key_count'] = int(items_df['Value'].max())

    ev_df = cloudwatch_eviction_series(metrics_df, cluster_id)
    cache_efficiency['first_eviction_ts'] = _safe(first_positive_timestamp(ev_df))

    oom_df = extra_stats.get('oom_df')
    oom = {
        'rejection_count': int(oom_df['OOM_events'].sum()) if oom_df is not None and not oom_df.empty else 0,
        'first_rejection_ts': _safe(extra_stats.get('first_oom_rejection_ts')),
    }

    # ------------------------------------------------------------------ #
    #  engine_cpu                                                          #
    # ------------------------------------------------------------------ #
    engine_cpu = {}
    if not metrics_df.empty:
        eng = metric_filter(metrics_df, 'EngineCPUUtilization', 'Average', 'CacheClusterId')
        if not eng.empty:
            engine_cpu['avg_pct'] = _safe(float(eng['Value'].mean()), 2)
            engine_cpu['max_pct'] = _safe(float(eng['Value'].max()), 2)

        bal = metric_filter(metrics_df, 'CPUCreditBalance', 'Average', 'CacheClusterId')
        if not bal.empty:
            engine_cpu['credit_balance_avg'] = _safe(float(bal['Value'].mean()), 2)
            engine_cpu['credit_balance_min'] = _safe(float(bal['Value'].min()), 2)

        use = metric_filter(metrics_df, 'CPUCreditUsage', 'Average', 'CacheClusterId')
        if not use.empty:
            engine_cpu['credit_usage_avg'] = _safe(float(use['Value'].mean()), 4)

    # ------------------------------------------------------------------ #
    #  memory                                                              #
    # ------------------------------------------------------------------ #
    memory = {}
    if not metrics_df.empty:
        for mname in ('DatabaseMemoryUsageCountedForEvictPercentage',
                      'DatabaseCapacityUsageCountedForEvictPercentage'):
            mem_df = metric_filter(metrics_df, mname, 'Average')
            if not mem_df.empty:
                nc = config.get('node_count', 1)
                keep = select_mem_dims(mem_df['Dimensions'], nc)
                mem_df = mem_df[mem_df['Dimensions'].isin(keep)]
                max_v = float(mem_df['Value'].max())
                memory['avg_usage_pct']   = _safe(float(mem_df['Value'].mean()), 2)
                memory['max_usage_pct']   = _safe(max_v, 2)
                memory['headroom_pct']    = _safe(100.0 - max_v, 2)
                break

        frag = metric_filter(metrics_df, 'MemoryFragmentationRatio', 'Average', 'CacheClusterId')
        if not frag.empty:
            memory['fragmentation_avg'] = _safe(float(frag['Value'].mean()), 3)
            memory['fragmentation_max'] = _safe(float(frag['Value'].max()), 3)

        swap = metric_filter(metrics_df, 'SwapUsage', 'Maximum', 'CacheClusterId')
        if not swap.empty:
            memory['swap_max_bytes'] = _safe(float(swap['Value'].max()), 0)

    # ------------------------------------------------------------------ #
    #  network                                                             #
    # ------------------------------------------------------------------ #
    network = {'cache': {}, 'throttling': {}}
    if not metrics_df.empty:
        out_df = metric_filter(metrics_df, 'NetworkBytesOut', 'Sum', 'CacheClusterId')
        if not out_df.empty:
            # sum per minute bucket → mean KB/min
            agg = out_df.groupby('Timestamp')['Value'].sum() / 1024.0
            network['cache']['avg_out_kbs'] = _safe(float(agg.mean()), 2)

        in_df = metric_filter(metrics_df, 'NetworkBytesIn', 'Sum', 'CacheClusterId')
        if not in_df.empty:
            agg = in_df.groupby('Timestamp')['Value'].sum() / 1024.0
            network['cache']['avg_in_kbs'] = _safe(float(agg.mean()), 2)

        for key, mname in [
            ('bw_in_exceeded_total',  'NetworkBandwidthInAllowanceExceeded'),
            ('bw_out_exceeded_total', 'NetworkBandwidthOutAllowanceExceeded'),
            ('pps_exceeded_total',    'NetworkPacketsPerSecondAllowanceExceeded'),
        ]:
            t_df = metric_filter(metrics_df, mname, 'Sum', 'CacheClusterId')
            network['throttling'][key] = int(t_df['Value'].sum()) if not t_df.empty else 0

    # ------------------------------------------------------------------ #
    #  latency_server_us (command-level, server-side)                     #
    # ------------------------------------------------------------------ #
    latency_server_us = {}
    if not metrics_df.empty:
        for key, mname in [
            ('get_avg', 'GetTypeCmdsLatency'),
            ('set_avg', 'SetTypeCmdsLatency'),
            ('string_avg', 'StringBasedCmdsLatency'),
        ]:
            agg = _metric_agg(metrics_df, mname, 'Average', 'CacheClusterId')
            if agg:
                latency_server_us[key] = agg['avg']

    # ------------------------------------------------------------------ #
    #  client_latency (ECS load-generator EMF percentiles)                #
    # ------------------------------------------------------------------ #
    client_latency = {}
    latency_df = client_latency_series(ecs_df)
    if not latency_df.empty:
        for key in ('p50_ms', 'p99_ms', 'p999_ms'):
            vals = latency_df[key].dropna()
            if not vals.empty:
                client_latency[key] = _safe(float(vals.mean()), 3)
        for key in ('worst_stream_p99_ms', 'worst_stream_p999_ms'):
            vals = latency_df[key].dropna()
            if not vals.empty:
                client_latency[key] = _safe(float(vals.max()), 3)

    # ------------------------------------------------------------------ #
    #  connections                                                         #
    # ------------------------------------------------------------------ #
    connections = {}
    if not metrics_df.empty:
        conn_df = metric_filter(metrics_df, 'CurrConnections', 'Average', 'CacheClusterId')
        if not conn_df.empty:
            agg = conn_df.groupby('Timestamp')['Value'].mean()
            connections['avg'] = _safe(float(agg.mean()), 1)
            connections['max'] = _safe(float(agg.max()), 1)

    # ------------------------------------------------------------------ #
    #  ecs                                                                 #
    # ------------------------------------------------------------------ #
    ecs = {}
    if not ecs_df.empty:
        cpu_df = metric_filter(ecs_df, 'CPUUtilization', 'Average')
        if not cpu_df.empty:
            ecs['avg_cpu_pct'] = _safe(float(cpu_df['Value'].mean()), 2)
            ecs['max_cpu_pct'] = _safe(float(cpu_df['Value'].max()), 2)

        mem_d = metric_filter(ecs_df, 'MemoryUtilized', 'Average')
        if not mem_d.empty:
            ecs['peak_mem_mb'] = _safe(float(mem_d['Value'].max()), 1)

    return {
        'meta':               meta,
        'benchmark':          benchmark,
        'cache_efficiency':   cache_efficiency,
        'oom':                oom,
        'engine_cpu':         engine_cpu,
        'memory':             memory,
        'network':            network,
        'latency_server_us':  latency_server_us,
        'client_latency':     client_latency,
        'connections':        connections,
        'ecs':                ecs,
    }
