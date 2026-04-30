locals {
  reporter_modules = toset([
    "report_generator.py",
    "report_common.py",
    "report_compare.py",
    "report_ecs.py",
    "helpers.py",
    "parsers.py",
    "charts.py",
    "cards.py",
    "template.py",
    "summary.py",
  ])
}

resource "aws_s3_object" "reporter_scripts" {
  for_each = local.reporter_modules

  bucket = var.metrics_export_s3_bucket
  key    = "scripts/${each.value}"
  source = "${path.module}/reporter/${each.value}"
  etag   = filemd5("${path.module}/reporter/${each.value}")
}

resource "aws_ecs_task_definition" "reporter" {
  family                   = "${local.cluster_id}-reporter"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_task_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "reporter"
      image     = "python:3.11-slim"
      essential = true

      # NOTE: This approach installs dependencies at runtime with minimum versions.
      # For production use, consider building a custom Docker image with pre-installed
      # dependencies (see reporter/Dockerfile and reporter/requirements.txt) and pushing
      # it to ECR. This eliminates supply chain risks and reduces task startup time.
      command = ["sh", "-c",
        <<-EOT
          set -e

          # Install required Python dependencies (minimum versions; latest compatible release is used)
          pip install --no-cache-dir "boto3>=1.35.81" "pandas>=2.2.3" "plotly>=5.24.1"

          # Download all reporter modules from S3
          python - << 'PY'
import boto3
import sys

bucket = "${var.metrics_export_s3_bucket}"
modules = [
    "report_generator.py",
    "report_common.py",
    "report_compare.py",
    "report_ecs.py",
    "helpers.py",
    "parsers.py",
    "charts.py",
    "cards.py",
    "template.py",
    "summary.py",
]

s3 = boto3.client("s3")
for mod in modules:
    try:
        s3.download_file(bucket, f"scripts/{mod}", mod)
        print(f"Downloaded {mod}")
    except Exception as e:
        print(f"Failed to download {mod}: {e}", file=sys.stderr)
        sys.exit(1)
PY

          # Run the report generator
          python report_generator.py
        EOT
      ]

      environment = [
        {
          name  = "S3_BUCKET"
          value = var.metrics_export_s3_bucket
        },
        {
          name  = "S3_PREFIX"
          value = var.metrics_export_s3_prefix
        },
        {
          name  = "CLUSTER_ID"
          value = local.cluster_id
        },
        {
          name  = "REPORT_TIMESTAMP"
          value = local.run_folder
        },
        {
          name  = "AWS_DEFAULT_REGION"
          value = var.aws_region
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.loadgen.name # Reuse loadgen log group
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "reporter"
        }
      }
    }
  ])

  tags = {
    Name = "${local.cluster_id}-reporter"
  }
}
