# Tech Debt Audit — tests-aws-elasticache-redis

**Date:** 2026-06-23  
**Scope:** reporter pipeline, exporter, download tooling, test coverage  
**Priority formula:** `(Impact + Risk) × (6 − Effort)` — higher = fix sooner

All findings are grounded in actual run data from `results/`.

> **Verification pass (2026-06-23):** every item was re-checked against the source and
> local `results/` data. Items **#1, #4, #5, #6, #8** confirmed as written. Items **#2,
> #3, #7** had incorrect diagnoses or evidence and have been corrected below, and
> re-scored where the priority changed. No AWS calls were needed for verification — all
> evidence is derivable from local `results/` data plus the code. The one open question
> (do #1/#7's metrics actually emit on the current **Valkey** cluster? `metrics_all_ec.txt`
> is stale, from a Feb **Redis** run) needs a live `list-metrics` — see notes under #1/#7.

---

## Summary Table

| # | Item | Type | Priority | Effort |
|---|------|------|----------|--------|
| 1 | ElastiCache metric list incomplete in `exporter.py` | Code | **50** | Low |
| 2 | Silent export failures — 3/5 historical runs have 2 metrics | Infrastructure | **20** ~~36~~ | Low |
| 3 | `summary.py` doesn't compute TODO.md fields | Code | **24** | Medium |
| 4 | ECS Container Insights metrics collected but not used | Code | **24** | Medium |
| 5 | `summary.py` has zero test coverage | Test | **24** | Medium |
| 6 | `fetch_missing_metrics.py` is an untested manual workaround | Code | **25** | Low |
| 7 | Dashboard/exporter mismatch — 4 command metrics never collected | Code | **10** ~~15~~ | Low |
| 8 | `download_results.sh` is copy-only, not sync | Infrastructure | **12** | Low |

---

## #1 — ElastiCache metric list incomplete in `exporter.py`

**Type:** Code debt  
**Priority: 50** — Impact 5, Risk 5, Effort 1

### Evidence

`REQUIRED_ELASTICACHE_METRICS` in `exporter.py` has 28 metrics. The latest run
(`20260609-060813`) has exactly those 30 (28 required + 2 optional credit metrics).
The manually-backfilled run (`20260307-093716`) has 57 after `fetch_missing_metrics.py`
was run against it. `TODO.md` lists ~15 summary fields that cannot be computed
because the source metrics are never collected automatically:

| Missing metric | Needed for (`TODO.md`) |
|---|---|
| `BytesUsedForCache` | `memory.bytes_used_max_mb`, `memory.bytes_per_key_at_peak` |
| `TrafficManagementActive` | `traffic_management.ever_active/active_sample_count` |
| `ErrorCount` | error/network summary |
| `NetworkMaxBytesIn/Out` | network burst summary |
| `Reclaimed` | `evictions_per_million_ops` denominator |
| `SaveInProgress` | outlier detection during RDB saves |
| `ReplicationLag` | replication health (also in CW dashboard, never saved) |
| `ReplicationBytes` | replication cost |
| `NonKeyTypeCmds/Latency` | admin command overhead baseline |
| `NetworkPacketsIn/Out` | full network picture |
| `NetworkMaxPacketsIn/Out` | packet burst detection |
| `MasterLinkHealthStatus` | replication link health |

`fetch_missing_metrics.py` exists solely to paper over this gap with a manual
post-run step. It has no tests and requires manually providing correct `--start`/`--end`
timestamps.

### Fix

Add the 13 metrics above to `REQUIRED_ELASTICACHE_METRICS` in `exporter.py`.
Delete `fetch_missing_metrics.py` — it becomes obsolete.
Update `test_exporter_metric_filters.py` to assert the new list.

> **Verify against the live cluster first.** All 13 are present in `metrics_all_ec.txt`,
> **but that file is from a Feb 2026 Redis run** (cluster `...27125324`) and the project is
> now on **Valkey**. Replication metrics (`ReplicationLag`, `ReplicationBytes`,
> `MasterLinkHealthStatus`) and `SaveInProgress` only emit when there are replicas / RDB
> saves, so a single-node cluster may not publish them. Run the `list-metrics` command
> below and add only the names AWS actually returns.

---

## #2 — Silent export failures: 3/5 historical runs have 2 metrics

**Type:** Infrastructure / Code debt  
**Priority: 20** — Impact 3, Risk 2, Effort 2 — *re-scored after verification (was 36)*

### Evidence

```
20260104-172326  →  2 metrics only
20260227-140039  →  2 metrics only
20260307-221840  →  2 metrics only
20260307-093716  →  57 metrics (manually backfilled)
20260609-060813  →  30 metrics (current baseline)
```

The 2-metric runs contain only `DatabaseCapacityUsageCountedForEvictPercentage` and
`DatabaseMemoryUsageCountedForEvictPercentage` — which are the replication-group-level
dimension metrics. The per-node `CacheClusterId`-dimension metrics (everything useful)
are absent.

Root cause (mechanism): `export_metric_sources_to_s3()` in `exporter.py` wraps every
`get_metric_statistics` call in `try/except` and silently continues with only a
`print()` ([exporter.py:255](reporter/exporter.py:255), [:285](reporter/exporter.py:285)).
If the `CacheClusterId`-level source fails (wrong cluster ID, permissions, timing), the
per-node data set disappears.

> **Correction (verified 2026-06-23):** the "report still generates, `report_status.json`
> still shows `complete: true`" claim is **no longer accurate**. `main()` now computes
> `status["complete"]` from `_metric_contract_status()` and **raises `RuntimeError`,
> refusing to generate an empty report** ([exporter.py:884-898](reporter/exporter.py:884)),
> writing the failure to `report_status.json` first. `EngineCPUUtilization` is already in
> `REPORT_CONTRACT_METRICS` ([exporter.py:90](reporter/exporter.py:90)). The three
> 2-metric runs are pre-gate artifacts (Jan–Mar 2026) and have **no `report_status.json`
> at all** — they predate the mechanism entirely. This item is therefore reduced from an
> open silent-failure bug to incremental hardening of an already-working gate.

### Fix

1. After export, validate that at least N datapoints exist for `EngineCPUUtilization`
   (the single most reliable per-node metric). Raise if zero.
2. Promote the existing `_metric_contract_status()` check to include a per-node
   dimension check, not just metric-name presence.
3. Consider surfacing the error in `report_status.json` as a new `warnings` array
   rather than only in the ECS task logs.

---

## #3 — `summary.py` doesn't compute TODO.md fields

**Type:** Code debt  
**Priority: 24** — Impact 5, Risk 3, Effort 3

### Evidence

Actual `results_20260609-060813.json` shows what is and isn't computed:

**Present:**
`benchmark.*`, `cache_efficiency.*`, `engine_cpu.*`, `memory.avg/max_usage_pct`,
`memory.fragmentation_*`, `network.cache.avg_in/out_kbs`, `network.throttling.*`,
`latency_server_us.get/set/string_avg`, `client_latency.p50/p99/p999_ms`, `connections.*`, `ecs.avg/max_cpu_pct`

**Absent (all in `TODO.md`):**

| Missing field | Source data available? |
|---|---|
| `memory.bytes_used_max_mb` | No — needs `BytesUsedForCache` (debt #1) |
| `memory.bytes_per_key_at_peak` | No — needs `BytesUsedForCache` |
| `memory.memory_amplification` | No — needs `BytesUsedForCache` |
| `memory.estimated_maxmemory_mb` | Partial — needs `node_memory_bytes` + fragmentation |
| `memory.memory_pressure_sample_pct` | No — needs `BytesUsedForCache` |
| `latency_server_us.read_p99` / `write_p99` | Yes — `GetTypeCmdsLatency`/`SetTypeCmdsLatency` p99 available |
| `evictions_per_million_ops` | Yes — `Evictions` Sum + `GetTypeCmds`/`SetTypeCmds` Sum in CSV |
| `evictions_per_10k_writes` | Yes |
| `cache_misses_per_million_ops` | Yes |
| `traffic_management.ever_active` | No — needs `TrafficManagementActive` (debt #1) |
| `cost.ops_per_dollar_hour` | Yes — `redis_hourly_usd` + `avg_ops` both in summary |
| `cost.cost_per_million_ops` | Yes |
| `cost.eviction_free_ops_per_dollar_hour` | Yes |
| `loadgen.expected_task_count` | Yes — but **not** from `memtier.task_count` (it is `""`); use ECS `TaskCount`/`RunningTaskCount` or memtier stream count |
| `loadgen.min_task_count_present` | Yes — `RunningTaskCount` in ECS CSV |
| `loadgen.throughput_task_skew_p90_to_p10` | Yes — per-stream totals in `memtier_totals_df` |
| `loadgen.generator_cpu_p95_pct` | Yes — `CPUUtilization` in ECS CSV |
| `host_cpu.avg/max/p95_pct` | Yes — `CPUUtilization` (host-level) in ECS CSV |

Also: `ecs.task_count` is `""` (empty string) in the result.

> **Correction (verified 2026-06-23):** the cause is **not** the `enrich_summary_meta()`
> guard, and `summary.py` does **not** write `task_count` at all (so the earlier "if
> `summary.py` already wrote `""`" theory is wrong). The guard
> `if "task_count" not in summary.get("ecs", {})` ([report_common.py:275](reporter/report_common.py:275))
> is therefore `True` and the function *does* run — but it copies
> `memtier.get("task_count")`, which is **itself `""`** in `cluster_details.json`
> (sourced from an empty `TASK_COUNT` env var at export time,
> [exporter.py:920](reporter/exporter.py:920)). Tweaking the guard cannot help; the source
> value is empty. The real fix is to derive task count from a populated source:
> `RunningTaskCount`/`TaskCount` in the ECS CSV (see debt #4) or the memtier stream count
> (`memtier_etl.py` `task_count_present`).

### Fix

Fields that only need existing CSV data (no dependency on debt #1):
`latency_server_us.read_p99/write_p99`, `evictions_per_million_ops`,
`evictions_per_10k_writes`, `cache_misses_per_million_ops`, `cost.*`,
`loadgen.*`, `host_cpu.*` — implement in `summary.py` in one pass.

Fix `ecs.task_count` by sourcing it from a populated field — ECS `RunningTaskCount`/
`TaskCount` or the memtier stream count — **not** by tweaking the
`enrich_summary_meta()` guard, which faithfully copies an already-empty value.

Fields dependent on debt #1 (`memory.bytes_*`, `traffic_management.*`) — implement
after #1 lands.

---

## #4 — ECS Container Insights metrics collected but not used in `summary.py`

**Type:** Code debt  
**Priority: 24** — Impact 4, Risk 2, Effort 2

### Evidence

The ECS CSV for `20260609-060813` contains:

```
ContainerCpuUtilization, ContainerMemoryUtilization
ContainerNetworkRxBytes, ContainerNetworkTxBytes
CpuUtilized, MemoryUtilized
NetworkRxBytes, NetworkTxBytes
RunningTaskCount, TaskCount
TaskCpuUtilization, TaskMemoryUtilization
ClientLatency (p50/p99/p99.9)
```

`summary.py` reads only `CPUUtilization` (service-level %) and `MemoryUtilized`
for the `ecs` section. The per-task Container Insights metrics and the richer
utilization breakdown are downloaded and ignored.

`TODO.md` already calls for `loadgen.min_task_count_present` (from `RunningTaskCount`),
`loadgen.generator_cpu_p95_pct` (from `CpuUtilized` per-task), and
`loadgen.throughput_task_skew_p90_to_p10` (from memtier totals).

### Fix

Extend `summary.py`'s `ecs` section to aggregate:
- `RunningTaskCount` → `loadgen.min_task_count_present`
- `CpuUtilized` (Container Insights, per-task) → `host_cpu.avg/max/p95_pct`
- `NetworkRxBytes`/`NetworkTxBytes` → ECS network rates
- `TaskCpuUtilization` → task-level CPU skew

These are all already in the downloaded CSV — zero new AWS calls needed.

---

## #5 — `summary.py` has zero test coverage

**Type:** Test debt  
**Priority: 24** — Impact 4, Risk 4, Effort 3

### Evidence

`tests/` has 15 files covering: download logic, exporter metric filters,
template escaping, ECS distribution report, client latency, multirun lifecycle,
report readiness, run uniqueness, terraform run ID, loadgen EMF contract,
schedule shutdown.

`summary.py` — which builds the entire JSON structure that every comparison report,
blog post stat card, and benchmark table depends on — has **no test file**.

`cards.py`, `charts.py`, `helpers.py` (cache hit rate derivation, eviction series,
client latency series) also have no direct tests. A regression in `cache_hit_rate_df()`
or `cloudwatch_eviction_series()` would ship silently.

### Fix

Add `tests/test_summary.py` with fixture DataFrames covering:
- Normal case: all metrics present
- Degraded case: `CacheHitRate` absent, derived from `CacheHits`/`CacheMisses`
- Edge case: empty ECS DataFrame, zero evictions, zero OOM
- Computed field correctness: `evictions_per_million_ops`, `cost.ops_per_dollar_hour`,
  `loadgen.throughput_task_skew_p90_to_p10`

---

## #6 — `fetch_missing_metrics.py` is an untested manual workaround

**Type:** Code debt  
**Priority: 25** — Impact 3, Risk 2, Effort 1

### Evidence

`reporter/fetch_missing_metrics.py` exists as a one-off backfill script. It:
- Has no tests
- Requires manually specifying `--start` / `--end` timestamps (easy to get wrong)
- Appends to an existing CSV without deduplicating (run it twice = duplicate rows)
- Has no validation that the appended data falls within the report window
- Is the only reason `20260307-093716` has 57 metrics

### Fix

This file becomes **obsolete once debt #1 is resolved**. Delete it then.
Until #1 is fixed, add a `--dry-run` flag and deduplication guard at minimum.

---

## #7 — Dashboard/exporter mismatch: 4 command-type metrics never collected

**Type:** Code debt  
**Priority: 10** — Impact 1, Risk 1, Effort 1 — *re-scored after verification (was 15)*

### Evidence

`cloudwatch.tf` dashboard includes `HashBasedCmds`, `ListBasedCmds`, `SetBasedCmds`,
`SortedSetBasedCmds` ([cloudwatch.tf:523](cloudwatch.tf:523)). None appear in any result
CSV — confirmed.

> **Correction (verified 2026-06-23):** the claim that these are "available from AWS
> (present in `metrics_all_ec.txt`)" is **false** — they are **not** in
> [metrics_all_ec.txt](reporter/metrics_all_ec.txt) (the list of metrics CloudWatch
> actually returned for the cluster). The memtier workload issues GET/SET on string keys,
> so ElastiCache almost certainly never publishes Hash/List/Set/SortedSet command metrics
> for these runs. Adding them to the exporter would likely collect **zero datapoints**,
> which is why the priority is dropped to 10.

This means the live dashboard has widgets for a command-type breakdown that produces no
data for this workload — a mismatch that may be better fixed by *removing* the widgets
than by collecting the metrics.

### Fix

**Blocked on AWS confirmation** — run the `list-metrics` command (see #1) first. Only if
AWS confirms the four names emit data should they be added to
`REQUIRED_ELASTICACHE_METRICS`. If they are absent for the live cluster (expected), fix
the mismatch in the other direction: remove the four from the `cloudwatch.tf` dashboard
widgets.

---

## #8 — `download_results.sh` is copy-only, not sync

**Type:** Infrastructure debt  
**Priority: 12** — Impact 2, Risk 1, Effort 2

### Evidence

`TODO.md` already tracks this explicitly:
> "Change from unconditional copy to sync-style behavior: skip local files that
> already exist with matching S3 size/metadata, add a `--force` option for full
> re-downloads."

Re-running the script re-downloads and overwrites all files, wasting bandwidth
and overwriting any local edits (e.g. manually-backfilled CSVs from debt #6).

### Fix

Before `aws s3 cp`, check if local file exists and compare size against S3
`ContentLength`. Skip if matching; add `--force` to override.

---

## Remediation Plan

### Phase 1 — Low effort, high return (do alongside next test run)

0. **(Prereq)** Run the `list-metrics` command below against the live Valkey cluster to confirm which of #1/#7's metrics actually emit. `metrics_all_ec.txt` is stale (Feb Redis run).
1. **#1** Add the AWS-confirmed ElastiCache metrics to `REQUIRED_ELASTICACHE_METRICS`; delete `fetch_missing_metrics.py`. The 4 command metrics from #7 are **excluded** unless step 0 shows they emit.
2. **#2** *(downgraded — already largely done)* The hard-fail gate already exists ([exporter.py:884](reporter/exporter.py:884)). Remaining: tighten `_metric_contract_status()` to a per-`CacheClusterId`-dimension datapoint check and surface a `warnings` array in `report_status.json`.
3. **#6** Resolved by #1.

### Phase 2 — Fill the summary gap

4. **#3 (partial)** Implement all TODO.md fields that have data today: `cost.*`, `evictions_per_million_ops`, `cache_misses_per_million_ops`, `latency_server_us.read_p99/write_p99`, `loadgen.*`, `host_cpu.*`, fix `ecs.task_count` empty string.
5. **#4** Extend `summary.py` ECS section to use Container Insights per-task metrics.
6. **#5** Write `tests/test_summary.py` in parallel with Phase 2 changes.

### Phase 3 — Remaining metrics and polish

7. **#3 (remainder)** Add `memory.bytes_*` and `traffic_management.*` fields once Phase 1 metrics land in a real run.
8. **#8** Sync behaviour in `download_results.sh`.
9. **#7** Only if the prereq `list-metrics` confirms the 4 command metrics emit data; otherwise remove them from the `cloudwatch.tf` dashboard instead.
