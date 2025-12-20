# Lambda function for shutdown orchestration
data "archive_file" "shutdown_lambda" {
  type        = "zip"
  source_file = "${path.module}/lambda/shutdown.py"
  output_path = "${path.module}/lambda/shutdown.zip"
}

resource "aws_lambda_function" "shutdown" {
  filename         = data.archive_file.shutdown_lambda.output_path
  function_name    = "${local.cluster_id}-shutdown"
  role             = aws_iam_role.lambda_shutdown_role.arn
  handler          = "shutdown.handler"
  source_code_hash = data.archive_file.shutdown_lambda.output_base64sha256
  runtime          = "python3.11"
  timeout          = 300
  memory_size      = 256

  environment {
    variables = {
      CLUSTER_ID     = local.cluster_id
      ECS_CLUSTER    = aws_ecs_cluster.loadgen.name
      ECS_SERVICE    = aws_ecs_service.loadgen.name
      ELASTICACHE_ID = aws_elasticache_replication_group.main.id
      S3_BUCKET      = var.metrics_export_s3_bucket
      S3_PREFIX      = var.metrics_export_s3_prefix
      LOG_GROUP      = aws_cloudwatch_log_group.loadgen.name
    }
  }

  tags = {
    Name = "${local.cluster_id}-shutdown"
  }
}

# Unique Lambda log group with date
resource "aws_cloudwatch_log_group" "lambda_shutdown" {
  name              = "/aws/lambda/${local.cluster_id}-shutdown-${formatdate("YYYYMMDD", timestamp())}"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-lambda-shutdown-logs"
  }

  lifecycle {
    ignore_changes = [name]
  }
}

# IAM Role for Lambda
resource "aws_iam_role" "lambda_shutdown_role" {
  name = "${local.cluster_id}-lambda-shutdown"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.cluster_id}-lambda-shutdown"
  }
}

# Lambda permissions policy
resource "aws_iam_role_policy" "lambda_shutdown_policy" {
  name = "${local.cluster_id}-lambda-shutdown-policy"
  role = aws_iam_role.lambda_shutdown_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
          "logs:GetLogEvents"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = "arn:aws:s3:::${var.metrics_export_s3_bucket}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:UpdateService"
        ]
        Resource = aws_ecs_service.loadgen.id
      },
      {
        Effect = "Allow"
        Action = [
          "elasticache:ModifyReplicationGroup"
        ]
        Resource = aws_elasticache_replication_group.main.arn
      }
    ]
  })
}

# EventBridge rule to trigger Lambda after test duration
resource "aws_cloudwatch_event_rule" "shutdown" {
  name                = "${local.cluster_id}-shutdown"
  description         = "Trigger shutdown Lambda after test duration"
  schedule_expression = "rate(${var.test_duration_minutes} minutes)"

  tags = {
    Name = "${local.cluster_id}-shutdown"
  }
}

resource "aws_cloudwatch_event_target" "shutdown" {
  rule      = aws_cloudwatch_event_rule.shutdown.name
  target_id = "shutdown-lambda"
  arn       = aws_lambda_function.shutdown.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.shutdown.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.shutdown.arn
}
