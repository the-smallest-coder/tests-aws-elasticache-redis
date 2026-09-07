"""Build a structured summary dict suitable for JSON serialisation and comparison reports."""

from helpers import (
    metric_filter,
    cache_hit_rate_df,
    client_latency_series,
    select_mem_dims,
    select_node_dimension_rows,
    cloudwatch_eviction_series,
    first_positive_timestamp,
)
from report_common import GENERATOR_SCHEMA_VERSION
from loadgen_analysis import build_loadgen_summary


# WP1 D1: fields double-written under a new, unit-correct name whose old key
# is kept, computed exactly as before, and frozen. avg_bandwidth_kbs is the
# one exception (D5): its value was always correct, only its name lied, so
# it is deprecated in place with no new field.
DEPRECATED_FIELDS = (
    "avg_in_kbs",
    "avg_out_kbs",
    "bw_in_exceeded_total",
    "bw_out_exceeded_total",
    "pps_exceeded_total",
    "p50_ms",
    "p99_ms",
    "p999_ms",
    "worst_stream_p99_ms",
    "worst_stream_p999_ms",
    "avg_bandwidth_kbs",
)


def _dedup_metric_rows(df, metric_name, cluster_id, stat='Sum'):
    """Rows for one metric/stat, deduplicated across the CacheClusterId /
    CacheNodeId dimension axis (D6). Every field below that sums or counts
    metrics_df rows goes through this, not a raw metric_filter() call."""
    sub = metric_filter(df, metric_name, stat, 'CacheClusterId')
    return select_node_dimension_rows(sub, cluster_id)


def _metric_sum(df, metric_name, cluster_id, stat='Sum'):
    """Total of one metric across its whole window, deduplicated (D6)."""
    selected = _dedup_metric_rows(df, metric_name, cluster_id, stat)
    return float(selected['Value'].sum()) if not selected.empty else None


def _network_kib_per_sec(df, metric_name, cluster_id):
    """Mean KiB/s across the run: dedup, sum per minute bucket, /60/1024, then mean.

    NetworkBytesOut/In are a Sum at Period=60 -- bytes accumulated *within*
    that 60s bucket, not a byte count that only needs a KiB conversion. The
    previous code divided by 1024 alone (KiB, correctly named) but not by 60,
    so every value was 60x too high; it also summed cluster- and node-level
    duplicate rows first, doubling that again. See PLAN_2.md WP1 step 2.
    """
    selected = _dedup_metric_rows(df, metric_name, cluster_id)
    if selected.empty:
        return None
    per_minute = selected.groupby('Timestamp')['Value'].sum() / 60.0 / 1024.0
    return _safe(float(per_minute.mean()), 2)


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
          "cache": { avg_out_kbs, avg_in_kbs (legacy, D1), out_kib_per_sec, in_kib_per_sec },
          "throttling": { bw_in_exceeded_total, bw_out_exceeded_total, pps_exceeded_total (legacy, D1),
                          bw_in_exceeded_count, bw_out_exceeded_count, pps_exceeded_count }
      },
      "latency_server_us": { get_avg, set_avg, string_avg, percentile_basis },
      "client_latency": { p50_ms, p99_ms, p999_ms, worst_stream_p99_ms, worst_stream_p999_ms (legacy, D1),
                          task_median_p50_ms, task_median_p99_ms, task_median_p999_ms,
                          worst_task_p99_ms, worst_task_p999_ms },
      "connections": { avg, max },
      "ecs": { service_cpu_time_avg_pct, service_cpu_time_peak_pct, peak_mem_mb, task_count },
      "loadgen": { per-task CPU p95, per-task throughput medians, per-AZ skew, validity }
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
        'node_hourly_usd': config.get('node_hourly_usd', ''),
        'node_hourly_usd_source': config.get('node_hourly_usd_source', ''),
        'node_hourly_usd_reason': config.get('node_hourly_usd_reason', ''),
        'node_count':      config.get('node_count', ''),
        'cluster_mode':    str(config.get('cluster_mode', 'false')).lower(),
        'generator_schema_version': GENERATOR_SCHEMA_VERSION,
        'source_mode': extra_stats.get('source_mode', ''),
        'memtier_window_source': extra_stats.get('memtier_window_source', 'memtier_log_messages'),
        'artifact_source': extra_stats.get('artifact_source', ''),
        'deprecated_fields': list(DEPRECATED_FIELDS),
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
        # Legacy keys: same old code, unchanged, frozen (D1). They carry the
        # 120x-too-high value (2x duplicate rows x 60x missing /60) on purpose.
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

        # New keys: deduplicated and correctly scaled (D1, WP1 step 2).
        out_kib = _network_kib_per_sec(metrics_df, 'NetworkBytesOut', cluster_id)
        if out_kib is not None:
            network['cache']['out_kib_per_sec'] = out_kib
        in_kib = _network_kib_per_sec(metrics_df, 'NetworkBytesIn', cluster_id)
        if in_kib is not None:
            network['cache']['in_kib_per_sec'] = in_kib

        for key, mname in [
            ('bw_in_exceeded_count',  'NetworkBandwidthInAllowanceExceeded'),
            ('bw_out_exceeded_count', 'NetworkBandwidthOutAllowanceExceeded'),
            ('pps_exceeded_count',    'NetworkPacketsPerSecondAllowanceExceeded'),
        ]:
            total = _metric_sum(metrics_df, mname, cluster_id)
            network['throttling'][key] = int(total) if total is not None else 0

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
        if latency_server_us:
            # D5: values are already a correct mean of the per-minute
            # CloudWatch Average; this just states the basis explicitly.
            latency_server_us['percentile_basis'] = 'minute_average'

    # ------------------------------------------------------------------ #
    #  client_latency (ECS load-generator EMF percentiles)                #
    # ------------------------------------------------------------------ #
    client_latency = {}
    latency_df = client_latency_series(ecs_df)
    if not latency_df.empty:
        # Legacy keys, frozen (D1): mean of per-minute EMF percentiles across
        # tasks -- an average of percentiles, which is not itself a percentile.
        for key in ('p50_ms', 'p99_ms', 'p999_ms'):
            vals = latency_df[key].dropna()
            if not vals.empty:
                client_latency[key] = _safe(float(vals.mean()), 3)
        for key in ('worst_stream_p99_ms', 'worst_stream_p999_ms'):
            vals = latency_df[key].dropna()
            if not vals.empty:
                client_latency[key] = _safe(float(vals.max()), 3)
        client_latency['percentile_basis'] = 'minute_mean_of_task_percentiles'

    # New keys (WP1 step 3): one aggregation (median/max across tasks) of the
    # already-final per-task memtier Totals, instead of two (mean-of-minutes
    # then mean/max-across-tasks). Still not a true run-wide percentile --
    # memtier reports no such thing without an HDR histogram merge (out of
    # scope, see PLAN_2.md "Извън обхвата") -- hence "task_median", not "run_p*".
    if memtier_totals_df is not None and not memtier_totals_df.empty:
        for new_key, column, agg in (
            ('task_median_p50_ms', 'p50_latency_ms', 'median'),
            ('task_median_p99_ms', 'p99_latency_ms', 'median'),
            ('task_median_p999_ms', 'p999_latency_ms', 'median'),
            ('worst_task_p99_ms', 'p99_latency_ms', 'max'),
            ('worst_task_p999_ms', 'p999_latency_ms', 'max'),
        ):
            if column not in memtier_totals_df.columns:
                continue
            vals = memtier_totals_df[column].dropna()
            if vals.empty:
                continue
            value = vals.median() if agg == 'median' else vals.max()
            client_latency[new_key] = _safe(float(value), 3)
        if any(key.startswith('task_median_') or key.startswith('worst_task_') for key in client_latency):
            client_latency['task_percentile_basis'] = 'memtier_task_totals'

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
            ecs['service_cpu_time_avg_pct'] = _safe(float(cpu_df['Value'].mean()), 2)
            ecs['service_cpu_time_peak_pct'] = _safe(float(cpu_df['Value'].max()), 2)

        mem_d = metric_filter(ecs_df, 'MemoryUtilized', 'Average')
        if not mem_d.empty:
            ecs['peak_mem_mb'] = _safe(float(mem_d['Value'].max()), 1)

    loadgen = build_loadgen_summary(
        extra_stats.get('memtier_samples_df'),
        memtier_minute_df,
        ecs_df,
        extra_stats.get('container_insights_task_df'),
        extra_stats.get('container_insights_service_df'),
        first_message_ts,
        last_message_ts,
        measured_elasticache_az=config.get('elasticache_availability_zone'),
        memtier_totals_df=memtier_totals_df,
    ) if first_message_ts is not None and last_message_ts is not None else {}
    if loadgen.get('expected_task_count') is not None:
        ecs['task_count'] = loadgen['expected_task_count']

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
        'loadgen':            loadgen,
    }
