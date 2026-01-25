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

      # Injection approach: Install deps, download script from S3, run script
      command = ["sh", "-c",
        "pip install boto3 pandas plotly && python -c \"import boto3, os; s3=boto3.client('s3'); s3.download_file('${var.metrics_export_s3_bucket}', 'scripts/report_generator.py', 'report_generator.py')\" && python report_generator.py"
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
