---
description: "Compare two ElastiCache test runs and generate an HTML report. Use when: comparing baseline vs candidate results, generating comparison reports, reporter-compare."
agent: "agent"
argument-hint: "Optional: <baseline-timestamp> <candidate-timestamp>"
---
Generate a comparison report between two ElastiCache test runs.

## Steps

1. List the available run directories under `results/` (exclude the `comparisons/` folder). Show them to the user so they can identify baseline and candidate timestamps.

2. If the user provided timestamps as arguments (e.g. `20260227-140039 20260307-093716`), use those. Otherwise ask the user to pick a baseline and a candidate from the list.

3. Run the comparison using the reporter virtual environment:

   **Windows (PowerShell)**:
   ```powershell
   reporter/.venv/Scripts/python.exe reporter/report_generator.py compare results/<baseline> results/<candidate>
   ```

   **Linux/macOS**:
   ```bash
   reporter/.venv/bin/python reporter/report_generator.py compare results/<baseline> results/<candidate>
   ```

4. Report the output path to the user:
   `results/comparisons/<baseline>_vs_<candidate>.html`

## Notes
- Each run directory must contain a `results_local.json` file.
- If `cluster_details.json` is present alongside `results_local.json`, the report will include configuration metadata.
- The output HTML is self-contained — open it directly in a browser.
