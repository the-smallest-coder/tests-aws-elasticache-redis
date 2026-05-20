# Plan 1: Memtier Log Event Granularity

## Goal

Make future memtier progress output arrive in CloudWatch as separate timestamped log events instead of large buffered carriage-return blocks.

## Hard Requirements

- Report window semantics do not change.
- Report start remains the first absolute memtier CloudWatch event timestamp.
- Report end remains the latest absolute memtier CloudWatch event timestamp.
- Do not reconstruct timestamps from `[RUN #1 ..., N secs]`.
- Any fix must operate before or at the container stdout/log-driver boundary.

## Current Evidence

- Old run `20260227-140039` had three memtier streams start by `2026-02-27T13:00:35.723000`.
- First progress output appeared as one CloudWatch event at `2026-02-27T13:29:28.717000`.
- That event contained 1,756 carriage-return progress records.
- Current ECS command already uses a FIFO and `awk` with `RS = "[\r\n]+"` plus `fflush()`, which is the right type of fix for carriage-return progress.

## Implementation Direction

1. Confirm the current loadgen container command is deployed exactly as repo code shows:
   - `memtier_benchmark > "$FIFO" 2>&1`
   - `awk 'BEGIN { RS = "[\r\n]+" } NF { print; fflush() }' < "$FIFO"`

2. Add a small buffering guard around memtier if the image supports it:
   - Prefer `stdbuf -o0 -e0 memtier_benchmark ...`
   - Fall back to plain `memtier_benchmark ...` if `stdbuf` is not present.

3. Keep the FIFO/awk splitter even if `stdbuf` is added.
   - Line buffering is not enough because memtier progress records are carriage-return records, not newline records.
   - The splitter is what turns `\r` progress records into newline-delimited records for `awslogs`.

4. Do not use `--realtime-latencies`.
   - Memtier does not have a native `--realtime-latencies` command-line flag.
   - For tail-latency observability, use real memtier options instead:
     - `--print-percentiles 90,99,99.9,99.99` for selected CLI percentile output.
     - `--hdr-file-prefix <prefix>` for full histogram data that can be processed after the run.
   - These options are latency-observability features, not fixes for stdout carriage-return buffering.

5. Avoid PTY/TTY-based fixes as the first option.
   - `script`, `unbuffer`, and ECS `pseudoTerminal` can introduce terminal control behavior and less predictable parsing.

## Validation

1. Run a new short test.
2. Download per-stream logs.
3. Verify each memtier stream has regular CloudWatch events during the run, not one huge delayed progress event.
4. Verify progress lines are individual records in downloaded logs.
5. Verify report window still uses first and latest memtier CloudWatch event timestamps.

## Out Of Scope

- Changing report time-window semantics.
- Deriving timestamps from memtier elapsed seconds.
- Broad parsing of all memtier warning/error strings.
