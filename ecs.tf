# Live AWS Price List lookup for this run's ElastiCache node hourly cost.
# Region- and engine-qualified (Valkey is priced ~20% below Redis on the
# same node type) instead of a hardcoded single-region Redis-only table.
#
# Shells out to `aws pricing get-products` because the Price List Query API
# has no native Terraform data source; the script always exits 0 and
# reports source: "unavailable" on any failure (missing perms, no network,
# no matching SKU), so a pricing hiccup never blocks `terraform apply`.
# Requires `aws` and `jq` on the machine running Terraform, and
# `pricing:GetProducts` on its credentials.
data "external" "node_price" {
  program = ["bash", "${path.module}/scripts/fetch_elasticache_price.sh"]

  query = {
    node_type   = var.node_type
    engine_type = var.engine_type
    aws_region  = var.aws_region
  }
}

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
      name        = "memtier"
      image       = "redislabs/memtier_benchmark:latest"
      essential   = true
      stopTimeout = 120

      entryPoint = ["sh", "-c"]
      command = [
        join(" ", concat(
          [
            "set -u;",
            "FIFO=/tmp/memtier-output.$$;",
            "mkfifo \"$FIFO\";",
            "UUID=$(cat /proc/sys/kernel/random/uuid);",
            "CLUSTER_NAME=${local.cluster_id}-loadgen;",
            "SERVICE_NAME=${local.cluster_id}-loadgen;",
            "TASK_ID=$UUID;",
            "if [ -n \"$${ECS_CONTAINER_METADATA_URI_V4:-}\" ]; then TASK_ARN=$( (wget -qO- \"$ECS_CONTAINER_METADATA_URI_V4/task\" 2>/dev/null || curl -fsS \"$ECS_CONTAINER_METADATA_URI_V4/task\" 2>/dev/null) | sed -n 's/.*\"TaskARN\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p' ); if [ -n \"$TASK_ARN\" ]; then TASK_ID=$${TASK_ARN##*/}; fi; fi;",
            "MEMTIER_PID=;",
            "term() { if [ -n \"$MEMTIER_PID\" ]; then kill -INT \"$MEMTIER_PID\" 2>/dev/null || true; fi; };",
            "trap term TERM INT;",
            "awk -v cluster=\"$CLUSTER_NAME\" -v service=\"$SERVICE_NAME\" -v task=\"$TASK_ID\" 'BEGIN { RS = \"[\\r\\n]+\" } function esc(s) { gsub(/\\\\/, \"\\\\\\\\\", s); gsub(/\"/, \"\\\\\\\"\", s); return s } function emit(  i,json,ts) { if (n == 0) return; ts = sprintf(\"%.0f\", systime() * 1000); json = \"{\\\"_aws\\\":{\\\"Timestamp\\\":\" ts \",\\\"CloudWatchMetrics\\\":[{\\\"Namespace\\\":\\\"ElastiCache/LoadGenerator\\\",\\\"Dimensions\\\":[[\\\"ClusterName\\\",\\\"ServiceName\\\",\\\"TaskId\\\"]],\\\"Metrics\\\":[{\\\"Name\\\":\\\"ClientLatency\\\",\\\"Unit\\\":\\\"Milliseconds\\\",\\\"StorageResolution\\\":60}]}]},\\\"ClusterName\\\":\\\"\" esc(cluster) \"\\\",\\\"ServiceName\\\":\\\"\" esc(service) \"\\\",\\\"TaskId\\\":\\\"\" esc(task) \"\\\",\\\"ClientLatency\\\":[\"; for (i = 1; i <= n; i++) json = json (i > 1 ? \",\" : \"\") vals[i]; json = json \"]}\"; print json; fflush(); n = 0 } function capture(line,  tmp,lat) { if (index(line, \"msec latency\") == 0) return; tmp = line; if (index(tmp, \"(avg:\") > 0) { sub(/^.*\\(avg:[[:space:]]*/, \"\", tmp); sub(/\\)[[:space:]]*msec latency.*$/, \"\", tmp) } else { sub(/[[:space:]]*msec latency.*$/, \"\", tmp); sub(/^.*[^0-9.]/, \"\", tmp) } lat = tmp; gsub(/^[[:space:]]+|[[:space:]]+$/, \"\", lat); if (lat ~ /^[0-9]+(\\.[0-9]+)?$/) { vals[++n] = lat; if (n >= 100) emit() } } NF { print; capture($0); fflush() } END { emit() }' < \"$FIFO\" &",
            "FILTER_PID=$!;",
            "if command -v stdbuf >/dev/null 2>&1; then STDBUF=\"stdbuf -o0\"; else STDBUF=\"\"; fi;",
            "$STDBUF memtier_benchmark",
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
          var.transit_encryption_enabled ? ["--tls", "--tls-skip-verify"] : [],
          [
            "> \"$FIFO\" 2>&1 &",
            "MEMTIER_PID=$!;",
            "while true; do wait \"$MEMTIER_PID\"; STATUS=$?; if [ \"$STATUS\" -ge 128 ] && kill -0 \"$MEMTIER_PID\" 2>/dev/null; then continue; fi; break; done;",
            "wait \"$FILTER_PID\" 2>/dev/null || true;",
            "rm -f \"$FIFO\";",
            "exit \"$STATUS\""
          ]
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
    subnets         = var.subnet_ids
    security_groups = [aws_security_group.loadgen.id]
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
  # Advertised memory (bytes) per ElastiCache node type, sourced from AWS's
  # own "Supported node types" table:
  #   https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/CacheNodes.SupportedTypes.html
  # (every row below was checked directly against that page, most recently
  # when the 12xlarge/16xlarge/24xlarge/10xlarge rows were added so real
  # ElastiCache sizes above each family's original top entry don't hit the
  # raw "Invalid index" below -- see the precondition on
  # aws_elasticache_replication_group.main for the friendly version of that
  # failure). The 85% target is applied separately by _fill_factor below so
  # the map stays easy to audit against that table.
  #
  # Comparability note: before 2026-06-15 (commit 145feec) this map held
  # coarse rounded advertised-GB figures (e.g. m7g.large = 7 GB, t4g.small =
  # 1.5 GB, cited to this same AWS page but not transcribed precisely from
  # it); it now holds AWS's exact decimal-GiB figures (6.38 GiB, 1.37 GiB).
  # That's strictly more correct, but it also changed memtier_key_maximum
  # for every node type on any run that let key-maximum auto-size
  # (var.loadgen_memtier_key_maximum == 0) -- an auto-sized run from before
  # that commit is NOT keyspace-comparable to one after it, even for the
  # identical node_type. Each run's actual keyspace is recorded in
  # cluster_details.json (memtier.key_maximum / memtier.key_maximum_auto);
  # check that field, not node_type alone, before comparing two reports.
  # ---------------------------------------------------------------------------
  _node_memory_bytes = {
    # T2 family
    "cache.t2.micro"  = 595926712  # 0.555 GiB
    "cache.t2.small"  = 1664299827 # 1.55 GiB
    "cache.t2.medium" = 3457448673 # 3.22 GiB
    # T4g family
    "cache.t4g.micro"  = 536870912  # 0.5 GiB
    "cache.t4g.small"  = 1471026298 # 1.37 GiB
    "cache.t4g.medium" = 3317862236 # 3.09 GiB
    # T3 family
    "cache.t3.micro"  = 536870912  # 0.5 GiB
    "cache.t3.small"  = 1471026298 # 1.37 GiB
    "cache.t3.medium" = 3317862236 # 3.09 GiB
    # C7gn family
    "cache.c7gn.large"    = 3317862236   # 3.09 GiB
    "cache.c7gn.xlarge"   = 6850472837   # 6.38 GiB
    "cache.c7gn.2xlarge"  = 13894219202  # 12.94 GiB
    "cache.c7gn.4xlarge"  = 27970974515  # 26.05 GiB
    "cache.c7gn.8xlarge"  = 56113747722  # 52.26 GiB
    "cache.c7gn.12xlarge" = 84353157693  # 78.56 GiB
    "cache.c7gn.16xlarge" = 113612622397 # 105.81 GiB
    # M4 family
    "cache.m4.large"    = 6893422510   # 6.42 GiB
    "cache.m4.xlarge"   = 15333033246  # 14.28 GiB
    "cache.m4.2xlarge"  = 31890132172  # 29.7 GiB
    "cache.m4.4xlarge"  = 65262028062  # 60.78 GiB
    "cache.m4.10xlarge" = 166043435663 # 154.64 GiB
    # M5 family
    "cache.m5.large"    = 6850472837   # 6.38 GiB
    "cache.m5.xlarge"   = 13883481784  # 12.93 GiB
    "cache.m5.2xlarge"  = 27960237096  # 26.04 GiB
    "cache.m5.4xlarge"  = 56113747722  # 52.26 GiB
    "cache.m5.12xlarge" = 168706315387 # 157.12 GiB
    "cache.m5.24xlarge" = 337498530120 # 314.32 GiB
    # M7g family
    "cache.m7g.large"    = 6850472837   # 6.38 GiB
    "cache.m7g.xlarge"   = 13883481784  # 12.93 GiB
    "cache.m7g.2xlarge"  = 27960237096  # 26.04 GiB
    "cache.m7g.4xlarge"  = 56113747722  # 52.26 GiB
    "cache.m7g.8xlarge"  = 111325552312 # 103.68 GiB
    "cache.m7g.12xlarge" = 168706315387 # 157.12 GiB
    "cache.m7g.16xlarge" = 225002599219 # 209.55 GiB
    # M6g family
    "cache.m6g.large"    = 6850472837   # 6.38 GiB
    "cache.m6g.xlarge"   = 13883481784  # 12.93 GiB
    "cache.m6g.2xlarge"  = 27960237096  # 26.04 GiB
    "cache.m6g.4xlarge"  = 56113747722  # 52.26 GiB
    "cache.m6g.8xlarge"  = 111325552312 # 103.68 GiB
    "cache.m6g.12xlarge" = 168706315387 # 157.12 GiB
    "cache.m6g.16xlarge" = 225002599219 # 209.55 GiB
    # R4 family
    "cache.r4.large"    = 13207024435  # 12.3 GiB
    "cache.r4.xlarge"   = 26897232691  # 25.05 GiB
    "cache.r4.2xlarge"  = 54191749857  # 50.47 GiB
    "cache.r4.4xlarge"  = 108855946117 # 101.38 GiB
    "cache.r4.8xlarge"  = 218248763146 # 203.26 GiB
    "cache.r4.16xlarge" = 437012922368 # 407.0 GiB
    # R5 family
    "cache.r5.large"    = 14033805639  # 13.07 GiB
    "cache.r5.xlarge"   = 28260884807  # 26.32 GiB
    "cache.r5.2xlarge"  = 56715043143  # 52.82 GiB
    "cache.r5.4xlarge"  = 113612622397 # 105.81 GiB
    "cache.r5.12xlarge" = 341202939412 # 317.77 GiB
    "cache.r5.24xlarge" = 682481040753 # 635.61 GiB
    # R7g family
    "cache.r7g.large"    = 14033805639  # 13.07 GiB
    "cache.r7g.xlarge"   = 28260884807  # 26.32 GiB
    "cache.r7g.2xlarge"  = 56715043143  # 52.82 GiB
    "cache.r7g.4xlarge"  = 113612622397 # 105.81 GiB
    "cache.r7g.8xlarge"  = 225002599219 # 209.55 GiB
    "cache.r7g.12xlarge" = 341202939412 # 317.77 GiB
    "cache.r7g.16xlarge" = 449994461020 # 419.09 GiB
    # R6g family
    "cache.r6g.large"    = 14033805639  # 13.07 GiB
    "cache.r6g.xlarge"   = 28260884807  # 26.32 GiB
    "cache.r6g.2xlarge"  = 56715043143  # 52.82 GiB
    "cache.r6g.4xlarge"  = 113612622397 # 105.81 GiB
    "cache.r6g.8xlarge"  = 225002599219 # 209.55 GiB
    "cache.r6g.12xlarge" = 341202939412 # 317.77 GiB
    "cache.r6g.16xlarge" = 449994461020 # 419.09 GiB
    # R6gd family
    "cache.r6gd.xlarge"   = 28260884807  # 26.32 GiB
    "cache.r6gd.2xlarge"  = 56715043143  # 52.82 GiB
    "cache.r6gd.4xlarge"  = 113612622397 # 105.81 GiB
    "cache.r6gd.8xlarge"  = 225002599219 # 209.55 GiB
    "cache.r6gd.12xlarge" = 341202939412 # 317.77 GiB
    "cache.r6gd.16xlarge" = 449994461020 # 419.09 GiB
  }

  # Live on-demand hourly price for this run's exact node_type / engine_type
  # / aws_region, from the AWS Price List API (see data.external.node_price
  # below). Replaces a hardcoded single-region Redis-only price table, which
  # silently mislabeled every non-us-east-1 run and every Valkey run (AWS
  # prices Valkey ~20% below Redis on the same node type).
  #
  # "" (not a number) means the live lookup failed -- see
  # data.external.node_price.result.reason. Never falls back to a stale
  # guess; downstream consumers must treat "" as unknown, not zero.
  _node_hourly_usd        = data.external.node_price.result.hourly_usd != "" ? tonumber(data.external.node_price.result.hourly_usd) : null
  _node_hourly_usd_source = data.external.node_price.result.source

  # 85% fill factor — cache stays warm without hitting eviction pressure
  _fill_factor = 0.85

  # Advertised bytes for this run. Unknown node types must fail at plan time;
  # silent fallback would corrupt the benchmark keyspace.
  _advertised_bytes = local._node_memory_bytes[var.node_type]

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
