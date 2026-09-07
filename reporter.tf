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
    "loadgen_analysis.py",
    "exporter.py",
    "memtier_etl.py",
    "formatting.py",
  ])
  reporter_scripts_prefix = "scripts/${local.cluster_id}/"
}

# Moved blocks: handle state migration from previous resource addresses.
# Previously the S3 objects lived in main.tf as aws_s3_object.report_scripts (for_each)
# and are now aws_s3_object.reporter_scripts.
moved {
  from = aws_s3_object.report_scripts["report_generator.py"]
  to   = aws_s3_object.reporter_scripts["report_generator.py"]
}

moved {
  from = aws_s3_object.report_scripts["report_common.py"]
  to   = aws_s3_object.reporter_scripts["report_common.py"]
}

moved {
  from = aws_s3_object.report_scripts["report_compare.py"]
  to   = aws_s3_object.reporter_scripts["report_compare.py"]
}

moved {
  from = aws_s3_object.report_scripts["report_ecs.py"]
  to   = aws_s3_object.reporter_scripts["report_ecs.py"]
}

moved {
  from = aws_s3_object.report_scripts["helpers.py"]
  to   = aws_s3_object.reporter_scripts["helpers.py"]
}

moved {
  from = aws_s3_object.report_scripts["parsers.py"]
  to   = aws_s3_object.reporter_scripts["parsers.py"]
}

moved {
  from = aws_s3_object.report_scripts["charts.py"]
  to   = aws_s3_object.reporter_scripts["charts.py"]
}

moved {
  from = aws_s3_object.report_scripts["cards.py"]
  to   = aws_s3_object.reporter_scripts["cards.py"]
}

moved {
  from = aws_s3_object.report_scripts["template.py"]
  to   = aws_s3_object.reporter_scripts["template.py"]
}

moved {
  from = aws_s3_object.report_scripts["summary.py"]
  to   = aws_s3_object.reporter_scripts["summary.py"]
}

resource "aws_s3_object" "reporter_scripts" {
  for_each = local.reporter_modules

  bucket = var.metrics_export_s3_bucket
  key    = "${local.reporter_scripts_prefix}${each.value}"
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

          # boto3/pandas: minimum versions, latest compatible release is used.
          # plotly: exact pin -- charts.py reads its private subplot
          # internals (fig._grid_ref, trace_kwargs, layout_keys), which can
          # change shape on any release with no warning. Keep this in sync
          # with reporter/requirements.txt; bump deliberately and re-verify
          # build_infra_panels, not via a floating >= constraint.
          pip install --no-cache-dir "boto3>=1.35.81" "pandas>=2.2.3" "plotly==6.9.0"

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
    "loadgen_analysis.py",
    "exporter.py",
    "memtier_etl.py",
    "formatting.py",
]

s3 = boto3.client("s3")
for mod in modules:
    try:
        s3.download_file(bucket, f"${local.reporter_scripts_prefix}{mod}", mod)
        print(f"Downloaded {mod}")
    except Exception as e:
        print(f"Failed to download {mod}: {e}", file=sys.stderr)
        sys.exit(1)
PY

          # Export CloudWatch data, generate the report, and send the final report-ready email.
          python exporter.py
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
          name  = "RUN_FOLDER"
          value = local.run_folder
        },
        {
          name  = "ELASTICACHE_ID"
          value = local.cluster_id
        },
        {
          name  = "ECS_CLUSTER"
          value = local.loadgen_cluster_name
        },
        {
          name  = "ECS_SERVICE"
          value = local.loadgen_service_name
        },
        {
          name  = "LOG_GROUP"
          value = aws_cloudwatch_log_group.loadgen.name
        },
        {
          name  = "LOADGEN_LOG_GROUP"
          value = aws_cloudwatch_log_group.loadgen.name
        },
        {
          name  = "CONTAINER_INSIGHTS_LOG_GROUP"
          value = aws_cloudwatch_log_group.container_insights.name
        },
        {
          name  = "ELASTICACHE_LOG_GROUP"
          value = aws_cloudwatch_log_group.elasticache.name
        },
        {
          name  = "LAMBDA_SCHEDULER_LOG_GROUP"
          value = aws_cloudwatch_log_group.lambda_shutdown_scheduler.name
        },
        {
          name  = "TEST_DURATION_MINUTES"
          value = tostring(var.test_duration_minutes)
        },
        {
          name  = "CLUSTER_MODE"
          value = tostring(var.cluster_mode_enabled)
        },
        {
          name  = "NUM_CACHE_NODES"
          value = tostring(var.num_cache_nodes)
        },
        {
          name  = "NUM_NODE_GROUPS"
          value = tostring(var.num_node_groups)
        },
        {
          name  = "REPLICAS_PER_NODE_GROUP"
          value = tostring(var.replicas_per_node_group)
        },
        {
          name  = "ENGINE_TYPE"
          value = var.engine_type
        },
        {
          name  = "ENGINE_VERSION"
          value = var.engine_version
        },
        {
          name  = "NODE_TYPE"
          value = var.node_type
        },
        {
          name  = "NODE_MEMORY_BYTES"
          value = tostring(local._advertised_bytes)
        },
        {
          name  = "NODE_HOURLY_USD"
          value = local._node_hourly_usd != null ? tostring(local._node_hourly_usd) : ""
        },
        {
          name  = "NODE_HOURLY_USD_SOURCE"
          value = local._node_hourly_usd_source
        },
        {
          name  = "NODE_HOURLY_USD_REASON"
          value = local._node_hourly_usd_reason
        },
        {
          name  = "NODE_COUNT"
          value = tostring(var.cluster_mode_enabled ? var.num_node_groups : var.num_cache_nodes)
        },
        {
          name  = "NOTIFICATION_EMAIL"
          value = var.notification_email
        },
        {
          name  = "SES_IDENTITY_ARN"
          value = var.notification_ses_identity_arn
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
