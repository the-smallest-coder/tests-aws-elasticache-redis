# TODO

## Reporting metrics backlog

- Add `BytesUsedForCache`, `TrafficManagementActive`, `SuccessfulReadRequestLatency`, `SuccessfulWriteRequestLatency`, `ErrorCount`, `NetworkMaxBytesIn`, and `NetworkMaxBytesOut`.
- Add host CPU summary: `host_cpu.avg_pct`, `host_cpu.max_pct`, `host_cpu.p95_pct`, `host_cpu_vs_single_thread_threshold_pct`.
- Add true client latency summary from memtier totals: `client_latency.p50_ms`, `client_latency.p99_ms`, `client_latency.p999_ms`, `client_latency.worst_stream_p99_ms`, `client_latency.worst_stream_p999_ms`.
- Add memory summary: `memory.bytes_used_max_mb`, `memory.bytes_per_key_at_peak`, `memory.memory_amplification`, `memory.estimated_maxmemory_mb`, `memory_pressure_sample_pct`.
- Add normalized eviction and miss summary: `evictions_per_million_ops`, `evictions_per_10k_writes`, `cache_misses_per_million_ops`, `first_eviction_ts`.
- Add traffic management summary: `traffic_management.ever_active`, `traffic_management.active_sample_count`.
- Add error and network summary from `ErrorCount`, `NetworkMaxBytesIn`, and `NetworkMaxBytesOut`.
- Add server request latency summary: `server_request_latency_us.read_avg`, `server_request_latency_us.read_p99`, `server_request_latency_us.write_avg`, `server_request_latency_us.write_p99`.
- Add load generator health summary: `loadgen.expected_task_count`, `loadgen.min_task_count_present`, `loadgen.samples_with_missing_tasks`, `loadgen.throughput_task_skew_p90_to_p10`, `loadgen.generator_cpu_p95_pct`.
- Add cost-normalized summary: `cost.ops_per_dollar_hour`, `cost.cost_per_million_ops`, `cost.peak_keys_per_dollar_hour`, `cost.eviction_free_ops_per_dollar_hour`.
- Add actual read/write command mix summary.

## Eviction pressure test
At t+30min of benchmark, launch a second fill task (pure writes, sequential keys, `--ratio 0:1 --key-pattern S:S`) to overflow memory and sustain eviction pressure for the remaining test window.
Implement as a `tfvars` flag so it applies consistently across all future node type / Valkey comparisons.

## Planned test matrix
- Different instance types (same load, same config)
- Valkey vs Redis (same node type, same load)
- Multi-node / cluster mode

## Results download sync
Change `scripts/download_results.sh` from unconditional copy to sync-style behavior:
skip local files that already exist with matching S3 size/metadata, add a `--force`
option for full re-downloads, and keep `--latest` scoped to the latest run while
still downloading only missing or changed artifacts.
