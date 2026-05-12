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
      stopTimeout = 120

      entryPoint = ["sh", "-c"]
      command = [
        join(" ", concat(
          [
            "UUID=$(cat /proc/sys/kernel/random/uuid)",
            "&&",
            "exec",
            "memtier_benchmark",
            "--server=${local.elasticache_endpoint}",
            "--port=${var.port}",
            "--threads=${var.loadgen_memtier_threads}",
            "--clients=${var.loadgen_memtier_clients}",
            "--pipeline=${var.loadgen_memtier_pipeline}",
            "--data-size=${var.loadgen_memtier_data_size}",
            "--ratio=${var.loadgen_memtier_ratio}",
            "--test-time=${local.memtier_test_time_seconds}",
            "--key-pattern=${var.loadgen_memtier_key_pattern}",
            "--key-prefix=$UUID-",
            "--key-maximum=${local.memtier_key_maximum}",
            "--hide-histogram"
          ],
          var.cluster_mode_enabled ? ["--cluster-mode"] : [],
          var.transit_encryption_enabled ? ["--tls", "--tls-skip-verify"] : []
        ))
      ]
      
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

  # ---------------------------------------------------------------------------
  # Advertised memory (bytes) per ElastiCache node type. The 85% target is
  # applied separately by _fill_factor below so the map stays easy to audit
  # against AWS instance specs.
  # Source: https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/CacheNodes.SupportedTypes.html
  # ---------------------------------------------------------------------------
  _node_memory_bytes = {
    # T4g family
    "cache.t4g.micro"  = 536870912    # 512 MB advertised
    "cache.t4g.small"  = 1610612736   # 1.5 GB advertised
    "cache.t4g.medium" = 3435973836   # 3.2 GB advertised
    # T3 family
    "cache.t3.micro"   = 536870912
    "cache.t3.small"   = 1610612736
    "cache.t3.medium"  = 3435973836
    # M7g family
    "cache.m7g.large"   = 7516192768   # 7 GB
    "cache.m7g.xlarge"  = 15032385536  # 14 GB
    "cache.m7g.2xlarge" = 30064771072  # 28 GB
    "cache.m7g.4xlarge" = 60129542144  # 56 GB
    "cache.m7g.8xlarge" = 120259084288 # 112 GB
    # M6g family
    "cache.m6g.large"   = 7516192768
    "cache.m6g.xlarge"  = 15032385536
    "cache.m6g.2xlarge" = 30064771072
    "cache.m6g.4xlarge" = 60129542144
    "cache.m6g.8xlarge" = 120259084288
    # R7g family
    "cache.r7g.large"   = 16106127360  # 15 GB
    "cache.r7g.xlarge"  = 32212254720  # 30 GB
    "cache.r7g.2xlarge" = 64424509440  # 60 GB
    "cache.r7g.4xlarge" = 128849018880 # 120 GB
    "cache.r7g.8xlarge" = 257698037760 # 240 GB
    # R6g family
    "cache.r6g.large"   = 16106127360
    "cache.r6g.xlarge"  = 32212254720
    "cache.r6g.2xlarge" = 64424509440
    "cache.r6g.4xlarge" = 128849018880
    "cache.r6g.8xlarge" = 257698037760
  }

  # 85% fill factor — cache stays warm without hitting eviction pressure
  _fill_factor = 0.85

  # Advertised bytes for this run; fall back to t4g.micro if type is unknown.
  _advertised_bytes = lookup(local._node_memory_bytes, var.node_type,
    local._node_memory_bytes["cache.t4g.micro"])

  # key-maximum: how many data_size-byte values fit at the target fill factor.
  # Each Redis key has ~70 bytes of overhead on top of the value.
  _key_overhead_bytes = 70
  _bytes_per_key      = var.loadgen_memtier_data_size + local._key_overhead_bytes

  # Capacity scales with primary shards in cluster mode. Non-cluster replicas
  # mirror the primary, so they do not increase writable keyspace.
  _writable_shard_count = var.cluster_mode_enabled ? var.num_node_groups : 1
  _target_fill_bytes    = local._advertised_bytes * local._writable_shard_count * local._fill_factor
  _target_total_keys    = floor(local._target_fill_bytes / local._bytes_per_key)
  _target_keys_per_task = max(1, floor(local._target_total_keys / var.loadgen_task_count))

  # If the user explicitly set key-maximum, honour it; otherwise auto-compute.
  memtier_key_maximum = var.loadgen_memtier_key_maximum > 0 ? var.loadgen_memtier_key_maximum : (
    local._target_keys_per_task
  )
}
