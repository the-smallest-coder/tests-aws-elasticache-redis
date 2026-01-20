# Report Generator Infrastructure

# CloudWatch Log Group for report generator
resource "aws_cloudwatch_log_group" "report_gen" {
  name              = "/aws/ecs/${local.cluster_id}/report-gen"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-report-gen-logs"
  }
}

# Upload report generator script to S3
resource "aws_s3_object" "report_script" {
  bucket = var.metrics_export_s3_bucket
  key    = "${var.metrics_export_s3_prefix}scripts/report.py"
  source = "${path.module}/report/report.py"
  etag   = filemd5("${path.module}/report/report.py")
}

# Upload requirements.txt to S3
resource "aws_s3_object" "report_requirements" {
  bucket = var.metrics_export_s3_bucket
  key    = "${var.metrics_export_s3_prefix}scripts/requirements.txt"
  source = "${path.module}/report/requirements.txt"
  etag   = filemd5("${path.module}/report/requirements.txt")
}

# ECS Task Definition for Report Generator
resource "aws_ecs_task_definition" "report_gen" {
  family                   = "${local.cluster_id}-report-gen"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.ecs_task_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "report-gen"
      image     = var.report_container_image
      essential = true

      # If using the default python:3.9-slim image, we need to install dependencies.
      # If the user provides a custom image (via variable), this command can be overridden or simply work
      # if the image has the script and dependencies.
      # Here we assume a "hybrid" approach: we check if requirements.txt exists locally (baked image)
      # or download it.
      # However, consistent with the sandbox limitations, we implement the dynamic download as the primary path
      # for the default image case, but we structure it cleanly.

      command   = ["/bin/sh", "-c", "if [ -f requirements.txt ]; then pip install -r requirements.txt; else pip install boto3 && python -c \"import boto3; s3 = boto3.client('s3'); s3.download_file('${var.metrics_export_s3_bucket}', '${var.metrics_export_s3_prefix}scripts/requirements.txt', 'requirements.txt'); s3.download_file('${var.metrics_export_s3_bucket}', '${var.metrics_export_s3_prefix}scripts/report.py', 'report.py')\" && pip install -r requirements.txt; fi && python report.py"]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.report_gen.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "report-gen"
        }
      }
    }
  ])

  tags = {
    Name = "${local.cluster_id}-report-gen"
  }
}
