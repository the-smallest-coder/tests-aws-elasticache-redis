resource "time_static" "run_id" {}

locals {
  # Auto-determine parameter group family if not specified
  parameter_group_family = var.parameter_group_family != "" ? var.parameter_group_family : (
    var.engine_type == "redis" ? "redis${split(".", var.engine_version)[0]}" : "valkey${split(".", var.engine_version)[0]}"
  )

  # Compact run timestamp and suffix keep IDs within ElastiCache length limits.
  run_id_full   = formatdate("YYYYMMDDHHmmss", time_static.run_id.rfc3339)
  run_id_suffix = substr(local.run_id_full, length(local.run_id_full) - 8, 8)

  # Optional per-run uniqueness token. Empty preserves legacy timestamp-only IDs.
  run_disc_suffix = var.run_id_discriminator != "" ? "-${var.run_id_discriminator}" : ""

  # Human-readable folder name shared by all artefacts for this run:
  # Terraform writes cluster_details.json here at apply time; the Lambda
  # uses the same name for metrics/logs so everything lands in one folder.
  run_folder = "${formatdate("YYYYMMDD-HHmmss", time_static.run_id.rfc3339)}${local.run_disc_suffix}"

  # Cluster identifier (run-scoped)
  cluster_id = "${var.project_name}-${var.engine_type}-${local.run_id_suffix}${local.run_disc_suffix}"

  # Common tags
  common_tags = {
    Name        = local.cluster_id
    Engine      = var.engine_type
    ClusterMode = var.cluster_mode_enabled ? "enabled" : "disabled"
  }
}

# Subnet group for ElastiCache
resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.cluster_id}-subnet-group"
  subnet_ids = var.subnet_ids

  tags = {
    Name = "${local.cluster_id}-subnet-group"
  }
}

# Parameter group for custom settings
resource "aws_elasticache_parameter_group" "main" {
  count = length(var.parameter_group_settings) > 0 ? 1 : 0

  name   = "${local.cluster_id}-params"
  family = local.parameter_group_family

  dynamic "parameter" {
    for_each = var.parameter_group_settings
    content {
      name  = parameter.key
      value = parameter.value
    }
  }

  tags = {
    Name = "${local.cluster_id}-params"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "elasticache" {
  name              = "/aws/elasticache/${local.cluster_id}"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${local.cluster_id}-logs"
  }
}

# ElastiCache Replication Group (supports both cluster and non-cluster modes)
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = local.cluster_id
  description          = "ElastiCache ${var.engine_type} cluster for performance testing"

  # Engine configuration
  engine         = var.engine_type
  engine_version = var.engine_version
  port           = var.port
  node_type      = var.node_type

  # Cluster mode configuration
  num_cache_clusters = var.cluster_mode_enabled ? null : var.num_cache_nodes

  # Cluster mode specific settings
  num_node_groups         = var.cluster_mode_enabled ? var.num_node_groups : null
  replicas_per_node_group = var.cluster_mode_enabled ? var.replicas_per_node_group : null

  # Parameter group
  parameter_group_name = length(var.parameter_group_settings) > 0 ? aws_elasticache_parameter_group.main[0].name : "default.${local.parameter_group_family}"

  # Network configuration
  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.elasticache.id]

  # Availability and failover
  #
  # Pin the cache cluster's AZ when requested so which ECS AZ ends up
  # co-located with the cache node is reproducible across repeated runs of
  # the same config, rather than left to AWS's per-apply placement (which
  # can flip between runs and inject AZ-placement noise unrelated to the
  # node type under test). One entry per cache cluster, so this only
  # applies to the single-node, non-cluster-mode case; cluster mode and
  # multi-node runs fall back to AWS's own placement.
  preferred_cache_cluster_azs = (
    var.cluster_mode_enabled || var.elasticache_availability_zone == "" || var.num_cache_nodes != 1
    ? null
    : [var.elasticache_availability_zone]
  )
  automatic_failover_enabled = var.automatic_failover_enabled
  multi_az_enabled           = var.automatic_failover_enabled

  # Security
  at_rest_encryption_enabled = var.at_rest_encryption_enabled
  transit_encryption_enabled = var.transit_encryption_enabled
  auth_token                 = var.transit_encryption_enabled ? var.auth_token : null

  # Backup and maintenance
  snapshot_retention_limit = var.snapshot_retention_limit
  snapshot_window          = var.snapshot_retention_limit > 0 ? var.snapshot_window : null
  maintenance_window       = var.maintenance_window

  # CloudWatch Logs
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.elasticache.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.elasticache.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "engine-log"
  }

  # Auto minor version upgrade
  auto_minor_version_upgrade = true

  tags = local.common_tags

  lifecycle {
    ignore_changes = [engine_version]

    precondition {
      condition     = length(local.cluster_id) <= 40
      error_message = "cluster_id '${local.cluster_id}' exceeds ElastiCache's 40-character replication-group-id limit. Shorten project_name or run_id_discriminator."
    }
  }
}
