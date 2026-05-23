# AWS ElastiCache Performance Testing — Agent Instructions

Terraform infrastructure for automated ElastiCache (Redis/Valkey) load testing.
Single `terraform apply` → spins up ElastiCache + ECS memtier_benchmark → runs test → auto-exports metrics/logs to S3 → stops ECS and deletes the ElastiCache replication group.

See [README.md](README.md) for full architecture diagrams and quick-start guide.

## Architecture

| Component | File(s) | Purpose |
|-----------|---------|---------|
| ElastiCache cluster | `main.tf` | Redis/Valkey replication group under test |
| ECS load generator | `ecs.tf` | Fargate tasks running `memtier_benchmark` |
| Security groups | `security_groups.tf` | ElastiCache SG allows TCP 6379 from load-gen SG |
| Lambda orchestration | `lambda_shutdown.tf`, `lambda/shutdown.py` | Export metrics/logs → delete cluster after test |
| Scheduling | `lambda/schedule_shutdown.py` | Sets EventBridge cron at `terraform apply` time |
| Post-test verify | `lambda/verify_shutdown.py` | SES alert if resources still running after shutdown |
| Reporter | `reporter/report_generator.py` | Generates HTML reports from metrics CSV + CloudWatch logs |

## Build & Run

### Terraform
```bash
terraform init
terraform apply -var-file=terraform.tfvars   # creates resources, starts test
terraform destroy                             # cleanup after test
```
`metrics_export_s3_bucket` is **required** — Terraform errors without it.

### Reporter (local comparison)
```bash
# Activate the virtual environment first
reporter/.venv/Scripts/Activate.ps1          # Windows PowerShell
# or
source reporter/.venv/bin/activate           # Linux/macOS

# Generate comparison report
python reporter/report_generator.py compare results/20260227-140039 results/20260307-093716
# Output: results/comparisons/<baseline>_vs_<candidate>.html

# Omitting `compare` keyword also works — two positional paths are treated as comparison
python reporter/report_generator.py results/20260227-140039 results/20260307-093716
```
Lambda deps are `boto3` only (built-in to Lambda runtime). Reporter deps are in `reporter/.venv/`.

## Key Conventions

### Naming
- **Cluster ID**: `{project_name}-{engine_type}-{last-8-digits-of-timestamp}`. Keep `project_name` short enough that the full ID stays within ElastiCache's 40-character limit; only the timestamp is shortened.
- **Run folders**: `results/YYYYmmdd-HHMMSS/`
- **S3 paths**: `{s3_prefix}/{timestamp}/metrics/{cluster_id}.csv` and `{cluster_id}-ecs.csv`, `logs/loadgen/{stream}.txt`
- **CloudWatch log groups**: `/aws/elasticache/{cluster_id}`, `/aws/ecs/{cluster_id}/loadgen`

### Metrics CSV format
```
Timestamp,Namespace,MetricName,Stat,Value,Unit,Dimensions
```
Produced by `lambda/shutdown.py`; consumed by `reporter/report_ecs.py`.

### Report time requirements
- **All report times are absolute timestamps only.**
- **Reporting window start** is the absolute timestamp of the very first memtier log message across all memtier log streams.
- **Reporting window end** is the absolute timestamp of the very latest memtier log message across all memtier log streams.
- Every plotted and reported datapoint must use the absolute timestamp attached to its log or metric record.
- No relative offsets, reconstructed timestamps, duration-derived windows, "N seconds/minutes after start", prefill windows, or active-window interpretations are allowed anywhere in reports.

### results_local.json structure
Nested dict with top-level keys: `meta`, `benchmark`, `cache_efficiency`, `engine_cpu`, `memory`, `network`, `latency_server_us`, `connections`, `ecs`. Metric specs in `report_compare.py` use tuple paths e.g. `("benchmark", "avg_ops")` resolved via `get_nested()` in `report_common.py`.

### cluster_details.json
Optional file saved alongside `results_local.json`. Contains `ecs`, `elasticache`, `memtier`, `run`, `node_memory_table` sections. `load_run()` in `report_common.py` enriches `RunData` with this if present.

### Delta tones
Comparison rows use CSS classes `tone-better`, `tone-worse`, `tone-neutral`, `tone-mixed`, `tone-warning`. Controlled by `direction` field on `MetricSpec` (`higher`/`lower`/`neutral`).

## Important Gotchas
- **EventBridge placeholder cron**: The shutdown rule is initially set to `cron(0 0 1 1 ? 2099)` (never fires). `schedule_shutdown` Lambda updates it at apply time.
- **Cluster mode vs non-cluster**: Endpoint logic branches — cluster mode uses `configuration_endpoint_address`; non-cluster uses `primary_endpoint_address`. Lambda and ECS task env vars reflect this.
- **Parameter group family**: Auto-detected from `engine_type` + major version (`redis7`, `valkey8`, etc.). Don't hardcode family names.
- **Per-task key prefixes**: Each ECS task gets a distinct key prefix to avoid write collisions when `loadgen_task_count > 1`.
- **AWS CLI commands are run by the user only** — never attempt to run `aws` CLI yourself.
