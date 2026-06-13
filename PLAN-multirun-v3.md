# Multirun v3 - Implementation Plan

Goal: add a small multirun helper for disposable ElastiCache performance runs
without changing the existing root Terraform workflow.

The root workflow remains valid and first-class:

```bash
terraform init
terraform apply -var-file=terraform.tfvars
terraform destroy
```

Multirun uses Terraform workspaces for isolated state. It does not extract a
module, generate wrapper Terraform, move the provider block, copy root
configuration files, or change how a normal single run works.

The lifecycle is explicit:

```text
configure -> apply once -> optional human download -> destroy
```

The helper never performs automatic download or automatic destroy.

The agent must not run AWS CLI or real `terraform init/plan/apply/destroy`.
Those commands are user-triggered only.

## 1. Locked Scope

- Root Terraform files stay the source of truth.
- The root `provider "aws"` block stays exactly in the root workflow.
- The `default` Terraform workspace is reserved for normal single-run use.
- Each multirun run uses one non-default Terraform workspace and one per-run
  tfvars file.
- Run names are global within the checkout because Terraform workspace names
  are global within the root configuration.
- Multirun files live under `multirun/`; there is no root-level generated
  `runs/` directory.
- `terraform.tfvars` is HCL. Do not parse it with `jq`.
- Region comes from the user's AWS CLI environment/profile. Do not add an
  `aws_region` output and do not pass a synthetic `--region` from state.
- Status is not a new helper feature. The rig already has email/progress
  behavior, and download can simply copy results when they exist.
- Re-apply is refused when the selected workspace already has Terraform state.
  Destroy first to redo a run.

## 2. Repository Facts

- The repo root is currently a complete single-run Terraform configuration.
- `run_id_discriminator` already exists and is used in `cluster_id` and
  `run_folder`.
- `run_folder` already exists as an output and is the key value needed to copy
  one run's S3 results.
- `metrics_export_location` already exists as an output and identifies the S3
  bucket/prefix root.
- `terraform.tfvars` is HCL, not JSON.
- `scripts/check_status.sh` and `scripts/download_results.sh` currently resolve
  root outputs by running `terraform output -json`.
- There is no `terragrunt/` directory in this tree.

## 3. Target Layout

```text
<repo root>
  *.tf                         # unchanged root single-run config
  terraform.tfvars             # unchanged root defaults
  multirun/
    multirun.sh
    README.md
    AGENTS.md
    runs/
      .gitkeep
      <name>.tfvars            # per-run overrides, including discriminator
    batches/
      .gitkeep
      <batch>.list             # ordered run names, one per line
    logs/
      <name>/
        apply.log
        download.log
        destroy.log
```

Existing `.gitignore` already covers `.terraform/`, `*.tfstate`,
`*.tfstate.*`, `*.tfvars`, `*.tfvars.json`, and `results/*`.

New gitignore entries needed for multirun:

```text
multirun/batches/*.list
multirun/logs/
.tf-plugin-cache/
```

Track `multirun/runs/.gitkeep` and `multirun/batches/.gitkeep`.

Terraform workspace state remains Terraform-managed under the current backend
layout. For the default local backend, non-default workspace state is under
`terraform.tfstate.d/<workspace>/terraform.tfstate` in the working directory.
Terraform stores the selected workspace name under `.terraform/`.

## 4. Workspace Rules

- Multirun workspace name equals the run name.
- Reject `default` as a multirun run name.
- Reject `/`, `\`, whitespace, `..`, leading dot, and empty names.
- Before any workspace operation, select or create the target workspace and
  assert `terraform workspace show` equals the run name.
- After every helper command, restore the `default` workspace on a best-effort
  trap so bare root Terraform commands stay predictable.
- Do not run two multirun helper commands concurrently in the same checkout.
  Terraform stores the active workspace per working directory, so concurrent
  helper commands would race on `.terraform/environment`. Use separate
  checkouts if concurrent helper orchestration is ever needed.
- `apply` refuses to run if `terraform state list` in the run workspace returns
  any resource address.
- `destroy` always runs in the run workspace and then switches to `default`
  before deleting the run workspace.
- If a workspace exists but its state is empty, `apply` may run. If state is
  non-empty, destroy first.

This is the whole no-reapply model. There is no stage file and no resume state
machine.

## 5. Run Configuration

`configure <name> [--var key=value ...]`

Creates `multirun/runs/<name>.tfvars` without running Terraform.

Rules:

- Validate the run name.
- Validate every `--var` key against declared root variables.
- Write scalar overrides as HCL assignments with string values, letting
  Terraform convert primitive types according to variable definitions.
- Escape HCL quoted strings correctly. In particular, emit literal Terraform
  interpolation openers by replacing `${` with `$${` and `%{` with `%%{`.
- Add `run_id_discriminator = "<value>"` unless the user supplied an explicit
  discriminator.
- Refuse to overwrite an existing run tfvars file.
- Do not copy root `terraform.tfvars`; Terraform loads it from the root during
  apply/destroy.
- Do not build an effective variable map.
- For rare structured values, the user may edit the generated tfvars file
  manually.

Batch configuration:

```bash
./multirun/multirun.sh configure-batch smoke \
  --var engine_type=valkey \
  --run a,node_type=cache.t3.micro \
  --run b,node_type=cache.t4g.micro,loadgen_task_count=3
```

- Batch-level `--var` applies to every run.
- Per-run values win.
- `--run <name>,k=v[,k=v...]` is the only batch override grammar.
- Reject embedded JSON and comma-containing values with a clear error.
- Write one tfvars file per run and `multirun/batches/<batch>.list`.
- If validation or discriminator allocation fails for any run, create nothing.
- Refuse to mutate an existing batch manifest.
- Refuse to create any run whose tfvars file already exists, even from another
  batch.

## 6. Discriminator Generation

Cluster ID format remains:

```text
<project_name>-<engine_type>-<8-char timestamp suffix>-<discriminator>
```

When a discriminator is present, the max discriminator length is:

```text
40 - len(project_name) - len(engine_type) - 8 - 3
```

Rules:

- For multirun runs, require at least two discriminator characters of budget.
- Generate two-character lowercase alphanumeric suffixes in manifest/configure
  order with alphabet `abcdefghijklmnopqrstuvwxyz0123456789`: `aa`, `ab`, ...
  `az`, `a0`, ... `a9`, `ba`, ... `99`.
- Skip explicitly supplied discriminators while generating automatic ones.
- Check discriminator uniqueness against all existing configured runs plus the
  new configure/configure-batch set.
- Fail before writing any tfvars files if an explicit or generated
  discriminator would repeat.
- Fail before writing any tfvars files if the two-character sequence is
  exhausted.
- Do not warn merely because the budget is two characters.

Budget estimation may read simple scalar `project_name` and `engine_type`
assignments from `terraform.tfvars` with a strict HCL scalar extractor. It must
not use `jq`. If extraction is ambiguous, require explicit `--var project_name`
and/or `--var engine_type`.

## 7. Apply

`apply <name>`

User-triggered AWS action.

Steps:

1. Require `multirun/runs/<name>.tfvars`.
2. Create the plugin cache directory:
   `mkdir -p "$REPO_ROOT/.tf-plugin-cache"`.
3. Run `terraform init -input=false` with
   `TF_PLUGIN_CACHE_DIR=$REPO_ROOT/.tf-plugin-cache`.
4. Select or create workspace `<name>`.
5. Assert the active workspace is `<name>`.
6. Run `terraform state list`.
7. If state is non-empty, refuse with `destroy first`.
8. Run:

   ```bash
   terraform apply -input=false -auto-approve \
     -var-file=multirun/runs/<name>.tfvars
   ```

9. Tee output to `multirun/logs/<name>/apply.log` and preserve Terraform's exit
   code.
10. Restore the `default` workspace before exit.

Always pass the per-run `-var-file` on apply. Missing it would silently use the
root defaults and create the wrong run.

`apply-all [batch]`

- If `batch` is supplied, read `multirun/batches/<batch>.list`.
- If no batch is supplied, iterate `multirun/runs/*.tfvars` by filename order.
- Continue on per-run errors.
- Print one summary line per run and a final count.
- Applies are serial. Each successful apply starts that run immediately.

## 8. Download

`download <name>`

Explicit human-controlled action. This is copy-only.

Steps:

1. Require `multirun/runs/<name>.tfvars`.
2. Select workspace `<name>` and assert it is active.
3. Run `terraform output -json`.
4. Read `metrics_export_location.value` and `run_folder.value`.
5. Copy exactly:

   ```text
   <metrics_export_location><run_folder>/ -> results/<run_folder>/
   ```

6. Do not insert an extra slash between `metrics_export_location` and
   `run_folder`; Terraform uploads artifacts as
   `${metrics_export_s3_prefix}${run_folder}/...`.
7. Before copying, run `aws s3 ls <source> --recursive`.
8. If the listing command succeeds with no object rows, print `no results yet`
   and exit 0 without running copy.
9. If the listing command fails, fail the download.
10. If the listing has objects, run `aws s3 cp <source>
   results/<run_folder>/ --recursive`.
11. Do not call status.
12. Do not use `--latest`.
13. Do not pass `--region`; rely on AWS CLI environment/profile.
14. Tee output to `multirun/logs/<name>/download.log`.
15. Restore the `default` workspace before exit.

`download-all [batch]`

- Iterate the same run set as `apply-all`.
- Continue on missing/not-ready results.
- Empty S3 listings are success.
- Exit non-zero if one or more S3 listing or copy operations fail.

## 9. Destroy

`destroy <name>`

User-triggered AWS action.

Steps:

1. Require `multirun/runs/<name>.tfvars`.
2. Select workspace `<name>` and assert it is active.
3. Run:

   ```bash
   terraform destroy -input=false -auto-approve \
     -var-file=multirun/runs/<name>.tfvars
   ```

4. Tee output to `multirun/logs/<name>/destroy.log` and preserve Terraform's
   exit code.
5. On successful destroy, select `default` and delete workspace `<name>`.
6. On failed destroy, keep the workspace and log for manual diagnosis.
7. Restore `default` before exit.

Always pass the per-run `-var-file` on destroy.

Destroy does not check whether download happened. Download is optional and
human-controlled.

`destroy-all [batch]`

- Iterate the same run set as `apply-all`.
- Continue on per-run failures.
- Exit non-zero if one or more destroys fail.

## 10. Summary

`summary [batch]`

No AWS calls by default.

- List configured runs from `multirun/runs/*.tfvars` or the selected batch.
- For each run, print whether the workspace exists.
- If the workspace exists, print whether `terraform state list` is empty or
  non-empty.
- Do not infer completion/readiness.

## 11. Existing Scripts

- Do not make status a required helper path.
- Do not add a `verify-state` command.
- Do not add stage files.
- Do not add output caches.
- `scripts/check_status.sh` may remain unchanged.
- `scripts/download_results.sh` may remain unchanged if the helper implements
  direct S3 copy for `download`.

If a later cleanup chooses to share code with `download_results.sh`, it must
preserve root legacy behavior and must not add region-from-state behavior.

## 12. Work Items

### WI-1 - Workspace Feasibility

Files:
`tests/fixtures/`, optional scratch fixture directory for local Terraform smoke
testing.

Tasks:

1. Use no AWS and do not add user-facing helper commands yet.
2. Prepare fake `terraform` and fake `aws` fixtures for later helper tests.
3. When Terraform is available locally, run an offline local-backend workspace
   smoke test to confirm non-default workspace state is written under
   `terraform.tfstate.d/<workspace>/terraform.tfstate`.
4. Record the expected workspace command sequences for implementation in WI-3.

Tests:

- fake binaries can be invoked by tests without AWS access;
- optional local Terraform smoke test confirms the real workspace state path;
- no root Terraform files are mutated.

### WI-2 - Configure and Batch Configure

Files:
`multirun/multirun.sh`, `tests/test_multirun_configure.py`, `.gitignore`.

Tasks:

1. Implement run-name validation.
2. Implement variable-key validation from root `variables.tf`.
3. Implement scalar HCL tfvars writing.
4. Implement sequential discriminator allocation.
5. Implement `configure`.
6. Implement `configure-batch`.
7. Confirm existing `*.tfvars` ignore covers multirun run configs.
8. Add gitignore entries for batch manifests, logs, and `.tf-plugin-cache/`.

Tests:

- invalid names rejected;
- `default` rejected;
- unknown variable keys rejected;
- scalar values are quoted/escaped correctly in tfvars;
- scalar values containing `${` or `%{` are written literally, not as Terraform
  interpolation/template openers;
- discriminator sequence starts `aa`, `ab`, and skips explicit values;
- duplicate discriminators fail before writing files;
- existing run config or batch manifest refuses overwrite;
- batch-level vs per-run scalar precedence works.

### WI-3 - Apply, Download, Destroy

Files:
`multirun/multirun.sh`, `tests/test_multirun_lifecycle.py`.

Tasks:

1. Implement `apply` and `apply-all`.
2. Implement `download` and `download-all`.
3. Implement `destroy` and `destroy-all`.
4. Implement `summary`.
5. Preserve pipeline exit codes with `pipefail`.
6. Continue-on-error for all `*-all` commands.

Tests:

- apply creates/selects workspace and refuses non-empty state;
- missing tfvars refuses apply/download/destroy;
- apply creates `.tf-plugin-cache/` before `terraform init`;
- apply and destroy always include the per-run `-var-file`;
- apply is serial in `apply-all`;
- download reads `metrics_export_location` and `run_folder`;
- download concatenates `metrics_export_location` and `run_folder` without
  inserting a slash;
- download runs explicit `aws s3 ls <source> --recursive` before copy;
- download with empty S3 listing exits 0 and does not run copy;
- failed S3 listing exits non-zero;
- download copy failure exits non-zero;
- download does not pass AWS `--region`;
- destroy selects default before workspace delete;
- destroy failure keeps workspace;
- all commands print per-run summaries.

### WI-4 - Docs

Files:
`multirun/README.md`, `multirun/AGENTS.md`, root `README.md`.

Tasks:

1. Document that root single-run remains unchanged.
2. Document workspace-per-run behavior.
3. Document `configure`, `configure-batch`, `apply/apply-all`,
   `download/download-all`, `destroy/destroy-all`, and `summary`.
4. Document strict lifecycle and no helper re-apply over non-empty state.
5. Document AWS region behavior: environment/profile only.
6. Document direct result copy and optional download.

Tests:

- docs do not mention generated Terraform wrappers;
- docs do not mention module extraction;
- docs do not mention root-level generated `runs/`;
- docs do not mention `verify-state`, stage files, output caches, or
  region-from-state.

## 13. Command Dependencies

| Command | Needs | AWS access |
| --- | --- | --- |
| configure / configure-batch | bash | none |
| apply / apply-all | bash, terraform | yes through Terraform, user-triggered |
| download / download-all | bash, terraform, jq, aws | S3 read |
| destroy / destroy-all | bash, terraform | yes through Terraform, user-triggered |
| summary | bash, terraform | local/backend state only |

## 14. Acceptance

Root single-run still works:

```bash
terraform init
terraform apply -var-file=terraform.tfvars
terraform destroy
```

Multirun workflow:

```bash
VALKEY_VERSION=<supported-valkey-version>

./multirun/multirun.sh configure-batch smoke \
  --var engine_type=valkey \
  --var engine_version="$VALKEY_VERSION" \
  --run a,node_type=cache.t3.micro \
  --run b,node_type=cache.t4g.micro,loadgen_task_count=3

./multirun/multirun.sh apply-all smoke

# Human decides when to fetch results.
./multirun/multirun.sh download-all smoke

./multirun/multirun.sh destroy-all smoke
```

Expected:

- root Terraform behavior remains unchanged;
- no module extraction is required;
- no generated Terraform wrapper exists;
- each run has isolated Terraform state through its workspace;
- every apply passes that run's tfvars file;
- every destroy passes that run's tfvars file;
- download copies only the selected run's S3 prefix;
- no helper command downloads or destroys automatically after apply;
- helper returns the Terraform workspace to `default`.

## 15. Non-Goals

- No module refactor.
- No generated Terraform wrappers.
- No root-level generated `runs/` directory.
- No root provider change.
- No `aws_region` output.
- No status feature.
- No wait command.
- No `verify-state`.
- No stage machine.
- No output cache.
- No active-run cap.
- No concurrent Terraform applies.
