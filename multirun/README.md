# Multirun helper

`multirun.sh` runs disposable ElastiCache performance tests from the existing
root Terraform configuration by using Terraform workspaces.

The normal root workflow is unchanged and remains first-class:

```bash
terraform init
terraform apply -var-file=terraform.tfvars
terraform destroy
```

Multirun is explicit:

```text
configure -> apply once -> optional download -> destroy
```

The helper does not automatically download results or destroy infrastructure.

## Layout

```text
multirun/
  multirun.sh
  runs/
    <name>.tfvars
  batches/
    <batch>.list
  logs/
    <name>/
      apply.log
      download.log
      destroy.log
```

Per-run `.tfvars` files and batch manifests are intentionally gitignored. They
are removed by successful destroy commands.

## Configure

Create one run configuration without touching AWS:

```bash
./multirun/multirun.sh configure t3 \
  --var engine_type=valkey \
  --var node_type=cache.t3.micro
```

Create a batch:

```bash
./multirun/multirun.sh configure-batch smoke \
  --var engine_type=valkey \
  --run a,node_type=cache.t3.micro \
  --run b,node_type=cache.t4g.micro,loadgen_task_count=3
```

Batch-level `--var` values apply to every run. Per-run values in `--run` win.
Every run receives a unique `run_id_discriminator` unless supplied explicitly.

## Apply

```bash
./multirun/multirun.sh apply a
./multirun/multirun.sh apply-all smoke
```

`apply` creates or selects workspace `a`, refuses to run if that workspace has
non-empty state, then runs Terraform with:

```bash
terraform apply -input=false -auto-approve -var-file=multirun/runs/a.tfvars
```

`apply-all` is serial and continues after per-run failures.

## Download

```bash
./multirun/multirun.sh download a
./multirun/multirun.sh download-all smoke
```

Download is a direct S3 copy for the selected run only. It reads
`metrics_export_location` and `run_folder` from that run's Terraform workspace
and copies:

```text
<metrics_export_location><run_folder>/ -> results/<run_folder>/
```

No status check is performed. Empty S3 listings print `no results yet` and exit
successfully. Prefixes that contain only bootstrap objects, such as
`cluster_details.json`, are treated as not ready and are not copied. A download
copies only after the listing contains result artifacts such as `metrics/`,
`logs/`, `report_status.json`, or `results_<run_folder>.html/json`; after copy,
the helper verifies that the local file count is at least the listed object
count. AWS region selection is left to the user's AWS CLI environment/profile;
the helper does not pass `--region`.

## Destroy

```bash
./multirun/multirun.sh destroy a
./multirun/multirun.sh destroy-all smoke
```

Destroy selects the run workspace and runs:

```bash
terraform destroy -input=false -auto-approve -var-file=multirun/runs/a.tfvars
```

On success, the helper selects `default`, deletes the run workspace, and removes
`multirun/runs/<name>.tfvars`. On failure, the workspace and run config remain
for diagnosis and retry.

When `destroy-all <batch>` succeeds for every listed run, it also removes the
batch manifest. Destroy logs remain under `multirun/logs/`.

## Summary

```bash
./multirun/multirun.sh summary
./multirun/multirun.sh summary smoke
```

Summary makes no AWS calls. If Terraform has not been initialized, configured
runs are reported as `not-initialized`. Otherwise it reports whether the
workspace exists and whether its state is empty or non-empty.

## Workspace rules

- Run name equals Terraform workspace name.
- `default` is reserved for normal single-run use.
- Re-applying over non-empty state is refused; destroy first.
- The helper restores the `default` workspace on exit where possible.
- Do not run two multirun helper commands concurrently in the same checkout.
