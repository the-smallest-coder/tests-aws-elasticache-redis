# Offline Validation Harness

## Findings (repo audit)

| Property | Value |
|----------|-------|
| IaC type | Terraform (single root module in repository root) |
| Providers | `hashicorp/aws ~> 5.0`, `hashicorp/time ~> 0.10`, `hashicorp/archive` (implicit) |
| Lock file | `.terraform.lock.hcl` committed – provider hashes are pinned |
| Lambda | Python 3 source in `lambda/`; packaged at apply time via `archive_file` |
| Existing CI | **None** before this PR |
| Lint / format tools | None pre-existing; Terraform's own `fmt` is the canonical formatter |
| CDK / CloudFormation / SAM | Not present |

---

## What Checks Exist

The workflow `.github/workflows/harness-offline.yml` runs three steps every
time a pull request is opened or updated, and on `workflow_dispatch`:

### 1. `terraform fmt -check`

**What it guarantees:**  
Every `.tf` file uses the canonical HCL formatting that `terraform fmt` produces
(two-space indentation, aligned `=` signs, etc.).

**What it does NOT guarantee:**  
That the Terraform configuration is semantically correct, or that it will apply
successfully against AWS.

**Failure means:**  
Run `terraform fmt` locally and commit the result.

---

### 2. `terraform init -backend=false`

**What it guarantees:**  
- Provider version constraints in `versions.tf` are satisfiable against the
  public Terraform registry.
- The lock file (`.terraform.lock.hcl`) is consistent with the version
  constraints.
- No remote backend (S3, Terraform Cloud, etc.) is contacted.

**What it does NOT guarantee:**  
- That the providers will work correctly against a real AWS account.
- That the provider version in the lock file is the best choice.

**Failure means:**  
Provider version constraints are unsatisfiable, or the lock file is out of date.
Run `terraform init` (or `terraform init -upgrade`) locally and commit the
updated lock file.

---

### 3. `terraform validate`

**What it guarantees:**  
- HCL syntax is valid.
- All variable references resolve to declared variables.
- Resource argument types match the provider schema for the pinned provider
  version.
- Required arguments (those without defaults) are present.

**What it does NOT guarantee:**  
- That resources will be created successfully (AWS-side validation happens only
  at apply time).
- That IAM permissions are sufficient.
- That referenced VPCs, subnets, S3 buckets, etc. exist.

**Failure means:**  
There is a type error, missing required argument, or undeclared reference. Read
the error output and fix the `.tf` file.

---

## What the Harness Deliberately Does NOT Check

Because this repository's infrastructure requires AWS:

- **No `terraform plan`** – requires AWS credentials and a live state backend.
- **No `terraform apply` / `terraform destroy`** – requires AWS credentials,
  live VPC, subnets, and S3 bucket.
- **No Lambda unit tests** – Lambda functions call the AWS SDK (`boto3`); they
  cannot be tested offline without mocking.
- **No integration / end-to-end tests** – the whole point of the repo is to run
  a live ElastiCache cluster; that requires AWS.
- **No Python linting** – no linter config exists in the repo; adding one is a
  separate concern.

---

## How to Add New Offline Checks

1. **Terraform only** – If you add a new `.tf` file or change an existing one,
   the three existing checks automatically cover it. No workflow change needed.

2. **New tool** (e.g., `tflint`, `checkov`, `trivy`) – Add a new step to the
   `terraform` job in `.github/workflows/harness-offline.yml`. Rules:
   - The step must not require AWS credentials.
   - The step must not require secrets of any kind.
   - Pin the tool version in the step.
   - Add a row to the step-summary table.
   - Document the check in this file.

3. **Python linting** – If the team decides to add a Python linter (e.g., `ruff`
   or `flake8`), add a `.ruff.toml` or `setup.cfg` to the repository root **and**
   add a corresponding job to the workflow. Do not add a linter without also
   adding its config file so the check is reproducible.

---

## Keeping CDK Synthesis Reproducible Without AWS Calls

> This repository does not use CDK. This section is included for completeness in
> case CDK is introduced in the future.

If CDK is ever added:

- Run `cdk synth --context key=value` with all required context values supplied
  on the command line, or commit a `cdk.context.json` that includes all required
  lookups.
- Use `--lookups false` (`--no-lookups` in CDK v2) to fail fast if any lookup
  would require an AWS API call.
- If `cdk.context.json` is present and up to date, synthesis is fully
  deterministic and offline.
- Failure message (if context is missing):
  > `Resolution error: Context lookups are disabled …`. Add the required context
  > values to `cdk.context.json` and commit them, or remove the lookup.

---

## Follow-Ups That Require Humans with AWS Access

The following improvements **cannot** be implemented in this harness (they
require live AWS access) and are tracked here for future action:

1. **`terraform plan` in CI** – Requires an AWS role, a live S3 backend, and
   OIDC trust configuration. Once set up, add as a separate
   `harness-with-aws.yml` workflow that only runs on pushes to `main`.

2. **Lambda integration tests** – Requires a real or localstack-based AWS
   environment. Consider adding `pytest` + `moto` for offline unit tests, or a
   dedicated AWS sandbox environment for integration tests.

3. **ElastiCache connectivity tests** – Require a live Redis/Valkey endpoint.

4. **S3 export validation** – Requires a real S3 bucket.

5. **Security scanning with real context** – Tools like `checkov` can flag AWS
   security misconfigurations, but some rules require knowing the live
   environment (e.g., whether the VPC has a NAT gateway).
