# Plan 2: Local And ECS Report Generation Parity

## Goal

Make the uploaded/ECS report generation path use the same data preparation model as local report generation.

## Hard Requirements

- Same raw inputs should produce the same benchmark sections locally and in ECS.
- Uploaded/ECS report generation must not silently miss memtier throughput/latency because sidecar artifacts were never generated.
- Report time rules remain absolute and memtier-log based.
- No local-only behavior should be required for a valid uploaded report.

## Current Evidence

- `run_uploaded_report()` reads memtier sidecar artifacts:
  - `_memtier.minute.csv`
  - `*.totals.json`
- The benchmark summary is populated only when both minute and totals artifacts exist.
- The exporter currently exports CloudWatch loadgen streams as `.txt`, but does not generate the memtier ETL sidecars before report generation.
- Result: uploaded reports can be missing benchmark throughput/latency sections even when raw memtier logs exist.

## Implementation Direction

1. Identify the single shared ETL entrypoint for memtier artifacts.
   - Use the same functions for local and ECS paths.
   - Do not duplicate parsing logic.

2. In the exporter path, after raw loadgen streams are exported, run memtier ETL on those exported stream files.
   - Generate per-stream `.minute.csv`.
   - Generate per-stream `.totals.json`.
   - Generate combined `_memtier.minute.csv`.

3. Upload generated artifacts into the same directory as the raw memtier stream files.
   - Same prefix as raw stream file where applicable.
   - Different suffix.

4. Ensure `run_uploaded_report()` reads the artifacts it just produced.
   - If artifacts are required and missing, fail with a clear error before producing an empty benchmark report.

5. Align local generation with the same contract.
   - Local generation should either generate missing memtier artifacts from raw logs or fail clearly.
   - It should not quietly produce a full-looking report from stale local artifacts.

6. Load `cluster_details.json` in uploaded/ECS report generation when it exists.
   - Local comparison can already use it.
   - Uploaded single-run reports should have the same metadata enrichment.

## Validation

1. Use a run with raw memtier logs and no pre-existing ETL sidecars.
2. Run local generation and uploaded-path generation in equivalent conditions.
3. Verify both produce:
   - populated benchmark summary
   - populated throughput/latency plots
   - same report window
   - same top-card values
4. Verify generated artifacts are present beside raw memtier files.
5. Verify missing raw memtier logs cause a clear failure, not a misleading empty benchmark section.

## Out Of Scope

- Changing memtier benchmark settings.
- Reconstructing timestamps from elapsed seconds.
- Making legacy incomplete runs look complete.
