# Multirun Agent Notes

- Root Terraform files remain the source of truth.
- Do not change the normal root workflow:
  `terraform init`, `terraform apply -var-file=terraform.tfvars`,
  `terraform destroy`.
- Use Terraform workspaces for multirun isolation. The run name is the workspace
  name.
- Keep generated run configs under `multirun/runs/` and batch manifests under
  `multirun/batches/`.
- Never run real AWS CLI commands or real Terraform apply/destroy/init on the
  user's behalf unless explicitly instructed by the user.
- `apply` and `destroy` must always pass the selected run's
  `-var-file=multirun/runs/<name>.tfvars`.
- `download` must copy only
  `<metrics_export_location><run_folder>/` to `results/<run_folder>/`.
- Keep lifecycle orchestration limited to the commands documented in
  `multirun/README.md`.
- Do not pass AWS `--region` from Terraform state; rely on AWS CLI
  environment/profile configuration.
