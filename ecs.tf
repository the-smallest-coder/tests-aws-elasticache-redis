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
    value = "enabled"
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
  name              = "/aws/ecs/${local.cluster_id}/loadgen-${formatdate("YYYYMMDD", timestamp())}"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-loadgen-logs"
  }

  lifecycle {
    ignore_changes = [name]
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

      command = concat(
        [
          "--server=${local.elasticache_endpoint}",
          "--port=${var.port}",
          "--threads=${var.loadgen_memtier_threads}",
          "--clients=${var.loadgen_memtier_clients}",
          "--pipeline=${var.loadgen_memtier_pipeline}",
          "--data-size=${var.loadgen_memtier_data_size}",
          "--ratio=${var.loadgen_memtier_ratio}",
          "--test-time=${var.loadgen_memtier_test_time}",
          "--key-pattern=${var.loadgen_memtier_key_pattern}",
          "--hide-histogram"
        ],
        var.cluster_mode_enabled ? ["--cluster-mode"] : [],
        var.transit_encryption_enabled ? ["--tls", "--tls-skip-verify"] : []
      )

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

  depends_on = [aws_elasticache_replication_group.main]
}


locals {
  # Determine the correct endpoint based on cluster mode
  elasticache_endpoint = var.cluster_mode_enabled ? (
    aws_elasticache_replication_group.main.configuration_endpoint_address
    ) : (
    aws_elasticache_replication_group.main.primary_endpoint_address
  )
}
