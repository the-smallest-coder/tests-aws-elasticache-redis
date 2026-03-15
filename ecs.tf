# CloudWatch Log Group for ECS Container Insights (managed for cleanup on destroy)
resource "aws_cloudwatch_log_group" "container_insights" {
  name              = "/aws/ecs/containerinsights/${local.cluster_id}-loadgen/performance"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-loadgen-container-insights"
  }
}

# ECS Cluster for load generators
resource "aws_ecs_cluster" "loadgen" {
  name = "${local.cluster_id}-loadgen"

  setting {
    name  = "containerInsights"
    value = var.ecs_container_insights_mode
  }

  configuration {
    execute_command_configuration {
      logging = "DEFAULT"
    }
  }

  # Ensure log group exists first and is deleted after the cluster.
  depends_on = [aws_cloudwatch_log_group.container_insights]

  tags = {
    Name = "${local.cluster_id}-loadgen"
  }
}

# CloudWatch Log Group for load generator (date-stamped)
resource "aws_cloudwatch_log_group" "loadgen" {
  name              = "/aws/ecs/${local.cluster_id}/loadgen"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-loadgen-logs"
  }
}

# ECS Task Definition for memtier_benchmark
resource "aws_ecs_task_definition" "loadgen" {
  family                   = "${local.cluster_id}-loadgen"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.loadgen_cpu
  memory                   = var.loadgen_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "memtier"
      image     = "redislabs/memtier_benchmark:latest"
      essential = true

      entryPoint = ["/bin/sh", "-c"]
      command = [join(" ", compact([
        "exec memtier_benchmark",
        "--server=${local.elasticache_endpoint}",
        "--port=${var.port}",
        "--threads=${var.loadgen_memtier_threads}",
        "--clients=${var.loadgen_memtier_clients}",
        "--pipeline=${var.loadgen_memtier_pipeline}",
        "--data-size=${var.loadgen_memtier_data_size}",
        "--ratio=${var.loadgen_memtier_ratio}",
        "--test-time=${local.memtier_test_time_seconds}",
        "--key-pattern=${var.loadgen_memtier_key_pattern}",
        "--key-prefix=$(cat /proc/sys/kernel/random/uuid)-",
        "--hide-histogram",
        var.loadgen_memtier_key_maximum > 0 ? "--key-maximum=${var.loadgen_memtier_key_maximum}" : "",
        var.cluster_mode_enabled ? "--cluster-mode" : "",
        var.transit_encryption_enabled ? "--tls --tls-skip-verify" : "",
      ]))]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.loadgen.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "memtier"
        }
      }
    }
  ])

  tags = {
    Name = "${local.cluster_id}-loadgen"
  }
}

# ECS Service to run load generator tasks
resource "aws_ecs_service" "loadgen" {
  name            = "${local.cluster_id}-loadgen"
  cluster         = aws_ecs_cluster.loadgen.id
  task_definition = aws_ecs_task_definition.loadgen.arn
  desired_count   = var.loadgen_task_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.loadgen.id]
    # Workaround only: enable when subnets have no NAT/egress for Docker Hub pulls.
    # Not recommended for normal use; prefer private subnets with NAT or ECR.
    assign_public_ip = var.loadgen_assign_public_ip
  }

  # Allow tasks to complete without being rescheduled
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 0

  tags = {
    Name = "${local.cluster_id}-loadgen"
  }

  depends_on = [
    aws_elasticache_replication_group.main,
    aws_cloudwatch_event_target.shutdown_scheduler,
    aws_lambda_permission.eventbridge_shutdown_scheduler
  ]
}


locals {
  # Determine the correct endpoint based on cluster mode
  elasticache_endpoint = var.cluster_mode_enabled ? (
    aws_elasticache_replication_group.main.configuration_endpoint_address
    ) : (
    aws_elasticache_replication_group.main.primary_endpoint_address
  )

  # Run effectively indefinitely when test time is 0.
  memtier_test_time_seconds = var.loadgen_memtier_test_time > 0 ? var.loadgen_memtier_test_time : 2147483647
  memtier_duration_label    = var.loadgen_memtier_test_time > 0 ? "${var.loadgen_memtier_test_time}s" : "until stopped"
}

# CloudWatch Log Group for report generator
resource "aws_cloudwatch_log_group" "report" {
  name              = "/aws/ecs/${local.cluster_id}/report"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-report-logs"
  }
}

# ECS Task Definition for report generator
resource "aws_ecs_task_definition" "report" {
  family                   = "${local.cluster_id}-report"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # Resource allocation: Starting with minimal resources (256 CPU, 512 MB memory).
  # These will be increased only if core dumps or task exits occur during execution.
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.report_task_execution_role.arn
  task_role_arn            = aws_iam_role.report_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "report-generator"
      image     = "python:3.11-slim"
      essential = true

      # Custom Execution Control:
      # This command explicitly overrides the container default to inject the report generation logic.
      # We install dependencies, fetch the script from S3, and execute it, maintaining full control
      # over the runtime environment without requiring a custom Docker image build.
      command = [
        "/bin/sh",
        "-c",
        "set -e; pip install boto3 pandas plotly && python - << 'PY'\nimport boto3\nimport sys\n\nbucket = '${var.metrics_export_s3_bucket}'\ns3 = boto3.client('s3')\ndownloaded = 0\n\nfor page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix='scripts/'):\n    for item in page.get('Contents', []):\n        key = item['Key']\n        if not key.endswith('.py'):\n            continue\n        name = key.rsplit('/', 1)[-1]\n        try:\n            s3.download_file(bucket, key, name)\n        except Exception as e:\n            print(f'Failed to download {name} from S3 bucket {bucket} with key {key}: {e}', file=sys.stderr)\n            sys.exit(1)\n        downloaded += 1\n\nif downloaded == 0:\n    print(f'No reporter Python files found in s3://{bucket}/scripts/', file=sys.stderr)\n    sys.exit(1)\nPY\npython report_generator.py"
      ]

      environment = [
        { name = "S3_BUCKET", value = var.metrics_export_s3_bucket },
        { name = "S3_PREFIX", value = var.metrics_export_s3_prefix },
        { name = "CLUSTER_ID", value = local.cluster_id }
        # REPORT_TIMESTAMP will be injected by Lambda overrides
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.report.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "report"
        }
      }
    }
  ])

  tags = {
    Name = "${local.cluster_id}-report"
  }
}
