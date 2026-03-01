---
applyTo: "**/*.tf"
---

# Terraform-Specific Instructions

## Scope

These rules apply to every `.tf` file in the repository root (the single
Terraform root module for this project).

---

## Formatting

- All HCL must pass `terraform fmt -check` with zero diff.
- Run `terraform fmt` (no flags) before every commit that touches `.tf` files.
- Use two-space indentation; do not use tabs.

## Validation (offline)

```bash
terraform init -backend=false   # resolves providers without touching remote state
terraform validate               # checks syntax and type correctness
```

Both commands must exit `0`. They are run automatically by
`.github/workflows/harness-offline.yml` on every PR.

## Provider Constraints

- AWS provider is pinned to `~> 5.0` (see `versions.tf`).
- Time provider is pinned to `~> 0.10`.
- Do **not** change these constraints unless specifically asked.
- If you bump a provider version, run `terraform init -upgrade` locally and
  commit the updated `.terraform.lock.hcl`.

## Variables

- Every variable must have a `description`.
- Variables carrying secrets must have `sensitive = true`.
- Use `validation` blocks for any variable that has a constrained set of valid
  values (see existing examples in `variables.tf`).

## Resource Naming

- Use `local.cluster_id` as the base name for all resources.
- All resources must include `tags = local.common_tags` or an explicit `tags`
  block derived from it.

## Lifecycle Rules

- Resources that are frequently replaced (parameter groups, log groups) should
  use `lifecycle { create_before_destroy = true }`.
- Never add `prevent_destroy = true` — the whole infrastructure is ephemeral.

## No AWS Access in CI

- Do **not** add workflow steps that call `terraform plan`, `terraform apply`,
  or `terraform destroy`.
- Do **not** add steps that call the AWS CLI or any AWS SDK.
- `terraform init -backend=false` is the only init variant allowed in CI.

## Lambda (Python in `lambda/`)

- Lambda code lives in `lambda/*.py`.
- It is packaged by Terraform's `archive_file` data source at apply time.
- Do **not** add a build step to CI that packages Lambda — it requires AWS.
- Python style: follow PEP 8; keep functions small and focused.
