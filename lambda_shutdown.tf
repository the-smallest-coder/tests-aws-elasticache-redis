# Lambda function for shutdown orchestration
data "archive_file" "shutdown_lambda" {
  type        = "zip"
  source_file = "${path.module}/lambda/shutdown.py"
  output_path = "${path.module}/lambda/shutdown.zip"
}

data "archive_file" "shutdown_scheduler_lambda" {
  type        = "zip"
  source_file = "${path.module}/lambda/schedule_shutdown.py"
  output_path = "${path.module}/lambda/schedule_shutdown.zip"
}

data "aws_caller_identity" "current" {}

locals {
  loadgen_cluster_name = "${local.cluster_id}-loadgen"
  loadgen_service_name = "${local.cluster_id}-loadgen"
  loadgen_service_arn  = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${local.loadgen_cluster_name}/${local.loadgen_service_name}"
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
      ECS_CLUSTER    = local.loadgen_cluster_name
      ECS_SERVICE    = local.loadgen_service_name
      ELASTICACHE_ID = aws_elasticache_replication_group.main.id
      S3_BUCKET      = var.metrics_export_s3_bucket
      S3_PREFIX      = var.metrics_export_s3_prefix
      LOG_GROUP      = aws_cloudwatch_log_group.loadgen.name
      CONTAINER_INSIGHTS_LOG_GROUP = aws_cloudwatch_log_group.container_insights.name
      ELASTICACHE_LOG_GROUP        = aws_cloudwatch_log_group.elasticache.name
      LAMBDA_SHUTDOWN_LOG_GROUP    = aws_cloudwatch_log_group.lambda_shutdown.name
      LAMBDA_SCHEDULER_LOG_GROUP   = aws_cloudwatch_log_group.lambda_shutdown_scheduler.name
      TEST_DURATION_MINUTES        = var.test_duration_minutes
      NOTIFICATION_EMAIL           = var.notification_email
      SES_IDENTITY_ARN             = var.notification_ses_identity_arn
    }
  }

  tags = {
    Name = "${local.cluster_id}-shutdown"
  }
}

resource "aws_lambda_function" "shutdown_scheduler" {
  filename         = data.archive_file.shutdown_scheduler_lambda.output_path
  function_name    = "${local.cluster_id}-shutdown-scheduler"
  role             = aws_iam_role.lambda_shutdown_scheduler_role.arn
  handler          = "schedule_shutdown.handler"
  source_code_hash = data.archive_file.shutdown_scheduler_lambda.output_base64sha256
  runtime          = "python3.11"
  timeout          = 60
  memory_size      = 128

  environment {
    variables = {
      ECS_CLUSTER          = local.loadgen_cluster_name
      ECS_SERVICE          = local.loadgen_service_name
      SHUTDOWN_RULE_NAME   = aws_cloudwatch_event_rule.shutdown.name
      TEST_DURATION_MINUTES = var.test_duration_minutes
      SHUTDOWN_RULE_PLACEHOLDER = "cron(0 0 1 1 ? 2099)"
    }
  }

  tags = {
    Name = "${local.cluster_id}-shutdown-scheduler"
  }
}

# Unique Lambda log group with date
resource "aws_cloudwatch_log_group" "lambda_shutdown" {
  name              = "/aws/lambda/${local.cluster_id}-shutdown"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-lambda-shutdown-logs"
  }
}

resource "aws_cloudwatch_log_group" "lambda_shutdown_scheduler" {
  name              = "/aws/lambda/${local.cluster_id}-shutdown-scheduler"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-lambda-shutdown-scheduler-logs"
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

resource "aws_iam_role" "lambda_shutdown_scheduler_role" {
  name = "${local.cluster_id}-shutdown-scheduler"

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
    Name = "${local.cluster_id}-shutdown-scheduler"
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
          "logs:GetLogEvents",
          "logs:FilterLogEvents"
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
          "ecs:ListTasks",
          "ecs:DescribeTasks"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "elasticache:DeleteReplicationGroup"
        ]
        Resource = aws_elasticache_replication_group.main.arn
      },
      {
        Effect = "Allow"
        Action = [
          "elasticache:DescribeReplicationGroups"
        ]
        Resource = "*"
      },
      {
        Effect   = var.notification_ses_identity_arn != "" ? "Allow" : "Deny"
        Action   = ["ses:SendEmail"]
        Resource = var.notification_ses_identity_arn != "" ? var.notification_ses_identity_arn : "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda_shutdown_scheduler_policy" {
  name = "${local.cluster_id}-shutdown-scheduler-policy"
  role = aws_iam_role.lambda_shutdown_scheduler_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.cluster_id}-shutdown-scheduler*:log-stream:*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "events:DescribeRule",
          "events:PutRule"
        ]
        Resource = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${aws_cloudwatch_event_rule.shutdown.name}"
      }
    ]
  })
}

# EventBridge rule to invoke scheduler when ECS tasks are running
resource "aws_cloudwatch_event_rule" "shutdown_scheduler" {
  name        = "${local.cluster_id}-shutdown-scheduler"
  description = "Schedule shutdown when ECS tasks enter RUNNING state"

  event_pattern = jsonencode({
    source = ["aws.ecs"],
    "detail-type" = ["ECS Task State Change"],
    detail = {
      clusterArn    = [aws_ecs_cluster.loadgen.arn],
      group         = ["service:${local.loadgen_service_name}"],
      lastStatus    = ["RUNNING"],
      desiredStatus = ["RUNNING"]
    }
  })
}

resource "aws_cloudwatch_event_target" "shutdown_scheduler" {
  rule      = aws_cloudwatch_event_rule.shutdown_scheduler.name
  target_id = "shutdown-scheduler-lambda"
  arn       = aws_lambda_function.shutdown_scheduler.arn
}

resource "aws_lambda_permission" "eventbridge_shutdown_scheduler" {
  statement_id  = "AllowEventBridgeInvokeShutdownScheduler"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.shutdown_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.shutdown_scheduler.arn
}

# EventBridge rule to trigger Lambda after test duration
resource "aws_cloudwatch_event_rule" "shutdown" {
  name                = "${local.cluster_id}-shutdown"
  description         = "Trigger shutdown Lambda at the scheduled time"
  schedule_expression = "cron(0 0 1 1 ? 2099)"

  tags = {
    Name = "${local.cluster_id}-shutdown"
  }

  lifecycle {
    ignore_changes = [schedule_expression]
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
