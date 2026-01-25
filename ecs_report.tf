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

      # Inject execution environment:
      # 1. Install dependencies (boto3)
      # 2. Download report script from S3
      # 3. Execute report script
      command   = ["/bin/sh", "-c", "pip install boto3 && python -c \"import boto3; boto3.client('s3').download_file('${var.metrics_export_s3_bucket}', '${var.metrics_export_s3_prefix}scripts/report.py', 'report.py')\" && python report.py"]

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
