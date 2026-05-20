# Plan 3: Strict Report Time Window Enforcement

## Goal

Ensure every report uses the strict memtier log window and never falls back to metric-derived or duration-derived windows.

## Hard Requirements

- Report window start is the absolute timestamp of the very first memtier log message across all memtier streams.
- Report window end is the absolute timestamp of the very latest memtier log message across all memtier streams.
- Throughput/progress records do not define the report window.
- Metric timestamps do not define the report window.
- No prefill/active-window/duration-derived fields are allowed as report-window semantics.
- The only approved relative display is the `First Eviction` top-card value, shown as elapsed time from the fixed report start.

## Current Evidence

- `results/20260227-140039/results_local.html` shows `2026-02-27 12:57 - 14:00 (63 min)`.
- The raw memtier first message is `2026-02-27T13:00:12.390000`.
- The raw memtier last message is `2026-02-27T14:00:39.089000`.
- The `12:57` start matches the earliest ElastiCache metric timestamp, not a memtier message timestamp.
- `results_local.json` for that run contains legacy relative fields such as `active_window_min`, `prefill_min`, and `first_eviction_offset_min`.

## Implementation Direction

1. Audit all report-window assignment paths.
   - Local generation.
   - Uploaded/ECS generation.
   - Comparison loading.
   - Legacy JSON loading.

2. Make memtier first/last message timestamps mandatory for single-run report generation.
   - If missing, fail clearly.
   - Do not fall back to metric min/max.

3. Add a hard validation before report rendering:
   - `first_message_ts` must be present.
   - `last_message_ts` must be present.
   - `first_message_ts <= last_message_ts`.

4. Remove or quarantine legacy relative report fields from newly generated summaries.
   - No new `active_window_min`.
   - No new `prefill_min`.
   - No new `first_eviction_offset_min`.

5. Keep metric data clipped to the memtier-defined report window.
   - Metrics outside the memtier log window are not part of report plots or top cards.

6. Keep `First Eviction` JSON absolute.
   - HTML card may display elapsed time from report start.
   - Tooltip should preserve the absolute CloudWatch eviction timestamp.

## Validation

1. Use a fixture where metrics start before memtier logs.
2. Verify header uses memtier first/last message timestamps, not metric min/max.
3. Verify plotted metric data is clipped to the memtier window.
4. Verify generated JSON has no legacy relative window fields.
5. Verify `20260227-140039` regeneration would show:
   - start `2026-02-27 13:00:12.390 UTC`
   - end `2026-02-27 14:00:39.089 UTC`

## Out Of Scope

- Treating first throughput/progress event as report start.
- Inferring timestamps for buffered progress records.
- Modifying old artifacts without an explicit regeneration step.
