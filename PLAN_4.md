# Plan 4: Legacy Results And Readiness Validation

## Goal

Prevent stale or incomplete local artifacts from being mistaken for valid current reports, and make old run folders explain their own limitations.

## Hard Requirements

- Do not treat `results_local.json/html` as authoritative when canonical uploaded artifacts and raw inputs disagree.
- Do not hide missing memtier logs or missing ETL sidecars behind a full-looking report.
- Downloader readiness remains based on the completed uploaded-report contract.
- No AWS CLI commands are run by agents.

## Current Evidence

- `results/20260227-140039` has:
  - rich `results_local.*`
  - old canonical `results_20260227-140039.html`
  - no canonical JSON
  - no `report_status.json`
  - no `cluster_details.json`
  - a legacy large loadgen log in an old path

- `results/20260501-083934` has:
  - `results_local.*`
  - `cluster_details.json`
  - metrics CSVs
  - no logs
  - no `report_status.json`
  - no canonical `results_*.html/json`

- Comparison loading prefers `results_local.json` when present.
- This can make stale local summaries look more complete than the data currently supports.

## Implementation Direction

1. Define local run readiness metadata.
   - Ready uploaded run:
     - `report_status.json`
     - `complete == true`
     - `report` and `summary`
     - referenced canonical files exist
   - Ready local regenerated run:
     - current generator version marker
     - strict memtier window present
     - required raw inputs or generated ETL artifacts present

2. Add report-generation metadata to new summaries.
   - generator version or schema version
   - source mode: local or uploaded
   - memtier window source
   - artifact source: generated, loaded, or missing

3. Add validation warnings for legacy folders.
   - Missing `report_status.json`.
   - Missing canonical JSON.
   - Missing memtier logs.
   - Missing memtier ETL artifacts.
   - Legacy relative fields detected.

4. Update comparison loading rules.
   - Prefer canonical current-schema JSON when available.
   - Use `results_local.json` only when it passes current schema/readiness validation.
   - Otherwise flag the run as legacy/incomplete instead of silently comparing stale values.

5. Add a local inspection command or mode if useful.
   - It should print exactly which files are present and which contract checks fail.
   - It should not regenerate or modify artifacts unless explicitly requested.

## Validation

1. Run inspection on `20260227-140039`.
   - It should flag stale local summary, missing status, missing canonical JSON, and legacy relative fields.

2. Run inspection on `20260501-083934`.
   - It should flag missing logs, missing ETL sidecars, and incomplete benchmark data.

3. Run inspection on a current complete run.
   - It should pass readiness checks.

4. Verify comparison reports do not silently use stale `results_local.json` when current contract fails.

## Out Of Scope

- Deleting old local artifacts.
- Regenerating old reports without explicit user instruction.
- Reclassifying old incomplete runs as complete.
