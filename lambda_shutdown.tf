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

data "archive_file" "shutdown_verify_lambda" {
  type        = "zip"
  source_file = "${path.module}/lambda/verify_shutdown.py"
  output_path = "${path.module}/lambda/verify_shutdown.zip"
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
  runtime          = "python3.13"
  timeout          = 300
  memory_size      = 256

  depends_on = [aws_cloudwatch_log_group.lambda_shutdown]

  environment {
    variables = {
      CLUSTER_ID     = local.cluster_id
      ECS_CLUSTER    = local.loadgen_cluster_name
      ECS_SERVICE    = local.loadgen_service_name
      ELASTICACHE_ID = aws_elasticache_replication_group.main.id
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
  runtime          = "python3.13"
  timeout          = 60
  memory_size      = 128

  depends_on = [aws_cloudwatch_log_group.lambda_shutdown_scheduler]

  environment {
    variables = {
      CLUSTER_ID                = local.cluster_id
      ECS_CLUSTER               = local.loadgen_cluster_name
      ECS_SERVICE               = local.loadgen_service_name
      SHUTDOWN_RULE_NAME        = aws_cloudwatch_event_rule.shutdown.name
      VERIFY_RULE_NAME          = aws_cloudwatch_event_rule.shutdown_verify.name
      TEST_DURATION_MINUTES     = var.test_duration_minutes
      VERIFY_DELAY_MINUTES      = tostring(var.test_duration_minutes + 15)
      SHUTDOWN_RULE_PLACEHOLDER = "cron(0 0 1 1 ? 2099)"
      NOTIFICATION_EMAIL        = var.notification_email
      SES_IDENTITY_ARN          = var.notification_ses_identity_arn
      ENGINE_TYPE               = var.engine_type
      ENGINE_VERSION            = var.engine_version
      NODE_TYPE                 = var.node_type
      NODE_COUNT                = tostring(var.cluster_mode_enabled ? var.num_node_groups : var.num_cache_nodes)
      CLUSTER_MODE              = tostring(var.cluster_mode_enabled)
      LOADGEN_TASK_COUNT        = tostring(var.loadgen_task_count)
      AWS_REGION_NAME           = var.aws_region
    }
  }

  tags = {
    Name = "${local.cluster_id}-shutdown-scheduler"
  }
}

resource "aws_lambda_function" "shutdown_verify" {
  filename         = data.archive_file.shutdown_verify_lambda.output_path
  function_name    = "${local.cluster_id}-shutdown-verify"
  role             = aws_iam_role.lambda_shutdown_verify_role.arn
  handler          = "verify_shutdown.handler"
  source_code_hash = data.archive_file.shutdown_verify_lambda.output_base64sha256
  runtime          = "python3.13"
  timeout          = 60
  memory_size      = 128

  depends_on = [aws_cloudwatch_log_group.lambda_shutdown_verify]

  environment {
    variables = {
      CLUSTER_ID                   = local.cluster_id
      ECS_CLUSTER                  = local.loadgen_cluster_name
      ECS_SERVICE                  = local.loadgen_service_name
      ELASTICACHE_ID               = aws_elasticache_replication_group.main.id
      S3_BUCKET                    = var.metrics_export_s3_bucket
      S3_PREFIX                    = var.metrics_export_s3_prefix
      RUN_FOLDER                   = local.run_folder
      REPORT_TIMESTAMP             = local.run_folder
      LOG_GROUP                    = aws_cloudwatch_log_group.loadgen.name
      LOADGEN_LOG_GROUP            = aws_cloudwatch_log_group.loadgen.name
      CONTAINER_INSIGHTS_LOG_GROUP = aws_cloudwatch_log_group.container_insights.name
      ELASTICACHE_LOG_GROUP        = aws_cloudwatch_log_group.elasticache.name
      LAMBDA_SCHEDULER_LOG_GROUP   = aws_cloudwatch_log_group.lambda_shutdown_scheduler.name
      TEST_DURATION_MINUTES        = var.test_duration_minutes
      CLUSTER_MODE                 = tostring(var.cluster_mode_enabled)
      NUM_CACHE_NODES              = tostring(var.num_cache_nodes)
      NUM_NODE_GROUPS              = tostring(var.num_node_groups)
      REPLICAS_PER_NODE_GROUP      = tostring(var.replicas_per_node_group)
      NOTIFICATION_EMAIL           = var.notification_email
      SES_IDENTITY_ARN             = var.notification_ses_identity_arn
      ENGINE_TYPE                  = var.engine_type
      ENGINE_VERSION               = var.engine_version
      NODE_TYPE                    = var.node_type
      NODE_MEMORY_BYTES            = tostring(local._advertised_bytes)
      NODE_HOURLY_USD              = local._node_hourly_usd != null ? tostring(local._node_hourly_usd) : ""
      NODE_HOURLY_USD_SOURCE       = local._node_hourly_usd_source
      NODE_HOURLY_USD_REASON       = local._node_hourly_usd_reason
      NODE_COUNT                   = tostring(var.cluster_mode_enabled ? var.num_node_groups : var.num_cache_nodes)
      AWS_REGION_NAME              = var.aws_region
      REPORTER_TASK_DEFINITION     = aws_ecs_task_definition.reporter.arn
    }
  }

  tags = {
    Name = "${local.cluster_id}-shutdown-verify"
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

resource "aws_cloudwatch_log_group" "lambda_shutdown_verify" {
  name              = "/aws/lambda/${local.cluster_id}-shutdown-verify"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-lambda-shutdown-verify-logs"
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

resource "aws_iam_role" "lambda_shutdown_verify_role" {
  name = "${local.cluster_id}-shutdown-verify"

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
    Name = "${local.cluster_id}-shutdown-verify"
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
          "ecs:UpdateService"
        ]
        Resource = aws_ecs_service.loadgen.id
      },
      {
        Effect = "Allow"
        Action = [
          "elasticache:DeleteReplicationGroup"
        ]
        Resource = aws_elasticache_replication_group.main.arn
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
        Resource = [
          "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${aws_cloudwatch_event_rule.shutdown.name}",
          "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${aws_cloudwatch_event_rule.shutdown_verify.name}"
        ]
      },
      {
        Effect   = var.notification_ses_identity_arn != "" ? "Allow" : "Deny"
        Action   = ["ses:SendEmail"]
        Resource = var.notification_ses_identity_arn != "" ? var.notification_ses_identity_arn : "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda_shutdown_verify_policy" {
  name = "${local.cluster_id}-shutdown-verify-policy"
  role = aws_iam_role.lambda_shutdown_verify_role.id

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
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.cluster_id}-shutdown-verify*:log-stream:*"
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
          "ecs:RunTask"
        ]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.reporter.family}:*"
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = [
          aws_iam_role.ecs_task_execution_role.arn,
          aws_iam_role.ecs_task_role.arn
        ]
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
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

# EventBridge rule to invoke scheduler when ECS deployment completes (service-level)
resource "aws_cloudwatch_event_rule" "shutdown_scheduler" {
  name        = "${local.cluster_id}-shutdown-scheduler"
  description = "Schedule shutdown when ECS deployment reaches COMPLETED state"

  event_pattern = jsonencode({
    source        = ["aws.ecs"],
    "detail-type" = ["ECS Deployment State Change"],
    resources     = [local.loadgen_service_arn],
    detail = {
      eventName = ["SERVICE_DEPLOYMENT_COMPLETED"]
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

resource "aws_cloudwatch_event_rule" "shutdown_verify" {
  name                = "${local.cluster_id}-shutdown-verify"
  description         = "Verify shutdown status after the test window"
  schedule_expression = "cron(0 0 1 1 ? 2099)"

  tags = {
    Name = "${local.cluster_id}-shutdown-verify"
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

resource "aws_cloudwatch_event_target" "shutdown_verify" {
  rule      = aws_cloudwatch_event_rule.shutdown_verify.name
  target_id = "shutdown-verify-lambda"
  arn       = aws_lambda_function.shutdown_verify.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.shutdown.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.shutdown.arn
}

resource "aws_lambda_permission" "eventbridge_shutdown_verify" {
  statement_id  = "AllowEventBridgeInvokeShutdownVerify"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.shutdown_verify.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.shutdown_verify.arn
}
