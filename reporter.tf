resource "aws_s3_object" "reporter_script" {
  bucket = var.metrics_export_s3_bucket
  key    = "scripts/report_generator.py"
  source = "${path.module}/reporter/report_generator.py"
  etag   = filemd5("${path.module}/reporter/report_generator.py")
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

      # NOTE: This approach installs dependencies at runtime with pinned versions.
      # For production use, consider building a custom Docker image with pre-installed
      # dependencies (see reporter/Dockerfile and reporter/requirements.txt) and pushing
      # it to ECR. This eliminates supply chain risks and reduces task startup time.
      command = ["sh", "-c",
        <<-EOT
          set -e

          # Install required Python dependencies with pinned versions
          pip install --no-cache-dir boto3==1.35.81 pandas==2.2.3 plotly==5.24.1

          # Download the report generator script from S3
          python - << 'PY'
import boto3
import os
import sys

bucket = "${var.metrics_export_s3_bucket}"
key = "scripts/report_generator.py"
local_path = "report_generator.py"

s3 = boto3.client("s3")

try:
    s3.download_file(bucket, key, local_path)
except Exception as e:
    print(f"Failed to download {key} from bucket {bucket}: {e}", file=sys.stderr)
    sys.exit(1)
PY

          # Verify that the script was downloaded successfully
          if [ ! -f report_generator.py ]; then
            echo "report_generator.py not found after download" >&2
            exit 1
          fi

          # Run the report generator
          python report_generator.py
        EOT
      ]

      environment = [
        {
          name  = "OUTPUT_BUCKET"
          value = var.metrics_export_s3_bucket
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
