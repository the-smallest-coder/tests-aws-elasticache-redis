"""Build a structured summary dict suitable for JSON serialisation and comparison reports."""

from helpers import metric_filter, cache_hit_rate_df, select_mem_dims, aggregate_memtier_progress


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


def build_summary(metrics_df, logs_df, ecs_df, extra_stats, config, cluster_id, time_range):
    """Return a fully-populated summary dict ready for json.dumps().

    Schema
    ------
    {
      "meta": { cluster_id, time_range, engine_type, engine_version, node_type, node_count, cluster_mode },
      "benchmark": { avg_ops, peak_ops, cv_pct, avg_latency_ms, max_latency_ms,
                     avg_bandwidth_kbs },
      "cache_efficiency": { avg_hit_rate_pct, total_evictions, min_freeable_memory_mb, peak_key_count,
                            first_eviction_ts },
      "engine_cpu": { avg_pct, max_pct, credit_balance_avg, credit_usage_avg },
      "memory": { avg_usage_pct, max_usage_pct, headroom_pct, fragmentation_avg },
      "network": {
          "cache": { avg_out_kbs, avg_in_kbs },
          "throttling": { bw_in_exceeded_total, bw_out_exceeded_total, pps_exceeded_total }
      },
      "latency_server_us": { get_avg, set_avg, string_avg },
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
        'node_count':      config.get('node_count', ''),
        'cluster_mode':    str(config.get('cluster_mode', 'false')).lower(),
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
    if not logs_df.empty:
        progress_agg = aggregate_memtier_progress(logs_df)
        ops = progress_agg['Overall Ops/sec']
        lat = progress_agg['Overall Latency (ms)']
        final_totals_df = extra_stats.get('final_totals_df')
        has_final_totals = final_totals_df is not None and not final_totals_df.empty
        avg_ops = float(final_totals_df['Ops/sec'].sum()) if has_final_totals else float(ops.mean())
        benchmark['avg_ops']         = _safe(avg_ops, 1)
        benchmark['peak_ops']        = _safe(float(ops.max()), 1)
        benchmark['cv_pct']          = _safe(float(ops.std() / avg_ops * 100) if avg_ops > 0 else 0.0, 2)
        if has_final_totals:
            weighted_latency = (
                final_totals_df['Latency (ms)'] * final_totals_df['Ops/sec']
            ).sum() / final_totals_df['Ops/sec'].sum()
            benchmark['avg_latency_ms'] = _safe(float(weighted_latency), 3)
        else:
            benchmark['avg_latency_ms'] = _safe(float(lat.mean()), 3)
        benchmark['max_latency_ms']  = _safe(float(lat.max()), 3)
        benchmark['p95_latency_ms']  = _percentile(lat, 95)
        benchmark['p99_latency_ms']  = _percentile(lat, 99)

        if has_final_totals:
            benchmark['avg_bandwidth_kbs'] = _safe(float(final_totals_df['Bandwidth_KBs'].sum()), 2)
        elif 'Bandwidth_KBs' in logs_df.columns and logs_df['Bandwidth_KBs'].notna().any():
            benchmark['avg_bandwidth_kbs'] = _safe(float(logs_df['Bandwidth_KBs'].mean()), 2)

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
        ev_df = metric_filter(metrics_df, 'Evictions', 'Sum', 'CacheClusterId')
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

    # First eviction timestamp
    first_eviction_ts = extra_stats.get('first_eviction_ts')
    if first_eviction_ts is not None:
        cache_efficiency['first_eviction_ts'] = _safe(first_eviction_ts)
    else:
        cache_efficiency['first_eviction_ts'] = None

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
        'engine_cpu':         engine_cpu,
        'memory':             memory,
        'network':            network,
        'latency_server_us':  latency_server_us,
        'connections':        connections,
        'ecs':                ecs,
    }
