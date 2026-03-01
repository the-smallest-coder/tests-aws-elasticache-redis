# Copilot Agent Instructions

## Project Purpose

This repository provides **Terraform infrastructure** for automated, ephemeral AWS
ElastiCache (Redis/Valkey) performance testing.

The intended workflow is:

```
terraform apply  →  ECS Fargate runs memtier_benchmark  →  Lambda exports metrics to S3  →  auto-shutdown  →  terraform destroy
```

The infrastructure is not meant to be long-lived; it is created per test run,
automatically tears itself down after a configurable duration, and is then
destroyed with `terraform destroy`.

---

## Repository Map

```
.                           ← Terraform root (single root module)
├── main.tf                 ← ElastiCache replication group + subnet/parameter groups
├── versions.tf             ← Provider requirements (aws ~>5, time ~>0.10)
├── variables.tf            ← All input variables with validation blocks
├── outputs.tf              ← Outputs (endpoints, cluster IDs, etc.)
├── ecs.tf                  ← ECS cluster + Fargate task + service (memtier_benchmark)
├── ecs_iam.tf              ← IAM roles for ECS task execution and task itself
├── security_groups.tf      ← Security groups for load-generator ↔ ElastiCache
├── cloudwatch.tf           ← CloudWatch dashboards and alarms
├── lambda_shutdown.tf      ← Lambda + EventBridge scheduler for auto-shutdown
├── lambda/                 ← Python source for the shutdown Lambda functions
│   ├── shutdown.py
│   ├── schedule_shutdown.py
│   └── verify_shutdown.py
├── terraform.tfvars.example← Reference variable values (safe to commit)
├── .terraform.lock.hcl     ← Provider lock file (must be committed)
├── .github/
│   ├── copilot-instructions.md  ← THIS FILE
│   ├── instructions/
│   │   └── terraform.instructions.md
│   └── workflows/
│       └── harness-offline.yml  ← Offline CI checks
└── docs/
    └── harness-offline.md  ← Harness documentation
```

---

## Hard Rules — Read Before Making Any Change

### NO AWS ACCESS
- **Never** add AWS credentials, OIDC role assumptions, or any secret to any
  workflow or configuration file.
- **Never** call AWS APIs from CI — no `aws` CLI commands, no `terraform plan`
  against a real backend, no `terraform apply`, no `terraform destroy`.
- **Never** add steps that read from or write to S3, SSM, Secrets Manager, or any
  other AWS service.

### NO TEST EXECUTION
- The repository has no unit-test framework.
- The Lambda Python code cannot be tested offline (it calls AWS SDK).
- **Do not** add test runners or attempt to execute tests in CI.

### OFFLINE VALIDATION ONLY
What "validation" means for this repo in an AWS-free environment:

| Check | Tool | What it guarantees |
|-------|------|--------------------|
| Terraform formatting | `terraform fmt -check` | Code is consistently formatted |
| Terraform initialisation | `terraform init -backend=false` | Provider versions resolve; no remote state touched |
| Terraform static analysis | `terraform validate` | HCL syntax and type-system are correct |

These checks run in `.github/workflows/harness-offline.yml` on every PR and on
`workflow_dispatch`. They require **no secrets and no AWS**.

---

## What You May Modify

| Path | Allowed changes |
|------|-----------------|
| `*.tf` files | Add/change Terraform resources, variables, outputs |
| `lambda/*.py` | Modify Lambda business logic |
| `terraform.tfvars.example` | Update example values |
| `.github/copilot-instructions.md` | Keep docs current |
| `.github/instructions/*.md` | Keep docs current |
| `.github/workflows/harness-offline.yml` | Add offline-only checks |
| `docs/harness-offline.md` | Keep docs current |
| `README.md` | Keep docs current |

## What You Must NOT Touch

| Path | Reason |
|------|--------|
| `.terraform.lock.hcl` | Auto-managed by `terraform init`; do not edit by hand |
| `*.tfstate` / `*.tfstate.*` | Never commit state files |
| `*.tfvars` (non-example) | Contain real secrets/VPC IDs; gitignored |
| `.github/agents/` | Internal agent config; do not read or modify |

---

## Coding Guidelines

1. **Formatting**: All `.tf` files must pass `terraform fmt -check`. Run
   `terraform fmt` locally before committing.
2. **Validation**: All `.tf` files must pass `terraform validate` (with
   `terraform init -backend=false` run first).
3. **Provider versions**: Do not change provider version constraints without also
   running `terraform init -upgrade` and committing the updated lock file.
4. **No inline secrets**: Variables that carry secrets must be `sensitive = true`;
   never hard-code credentials.
5. **Minimal blast-radius**: Prefer `count` / `for_each` and `lifecycle` blocks
   when changing resources that exist in live environments.
