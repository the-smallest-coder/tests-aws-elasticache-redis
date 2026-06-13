# ---------------------------------------------------------------------------
# cluster_details.json — uploaded to S3 immediately after terraform apply.
#
# This file is the authoritative record of what was tested: every parameter
# that could explain a performance difference between two runs.  Because the
# cluster is destroyed after each test, the JSON must be written before the
# test begins (i.e. at apply time, not at destroy time).
#
# S3 path: {metrics_export_s3_prefix}{run_folder}/cluster_details.json
# The same run_folder is used by the shutdown Lambda for metrics/logs, so
# all artefacts for one test run live in a single deterministic folder.
# ---------------------------------------------------------------------------

resource "aws_s3_object" "cluster_details" {
  bucket = var.metrics_export_s3_bucket
  key    = "${var.metrics_export_s3_prefix}${local.run_folder}/cluster_details.json"

  content = jsonencode({
    # ----- Run metadata -----
    run = {
      run_id               = local.run_id_full
      run_folder           = local.run_folder
      cluster_id           = local.cluster_id
      aws_region           = var.aws_region
      run_id_discriminator = var.run_id_discriminator
    }

    # ----- ElastiCache configuration (as applied) -----
    elasticache = {
      engine                     = var.engine_type
      engine_version_configured  = var.engine_version
      node_type                  = var.node_type
      node_memory_bytes          = lookup(local._node_memory_bytes, var.node_type, 0)
      cluster_mode_enabled       = var.cluster_mode_enabled
      num_cache_nodes            = var.cluster_mode_enabled ? null : var.num_cache_nodes
      num_node_groups            = var.cluster_mode_enabled ? var.num_node_groups : null
      replicas_per_node_group    = var.cluster_mode_enabled ? var.replicas_per_node_group : null
      automatic_failover_enabled = var.automatic_failover_enabled
      multi_az_enabled           = var.automatic_failover_enabled
      at_rest_encryption_enabled = var.at_rest_encryption_enabled
      transit_encryption_enabled = var.transit_encryption_enabled
      port                       = var.port
      parameter_group_name       = length(var.parameter_group_settings) > 0 ? aws_elasticache_parameter_group.main[0].name : "default.${local.parameter_group_family}"
      parameter_group_settings   = var.parameter_group_settings
      snapshot_retention_limit   = var.snapshot_retention_limit
      maintenance_window         = var.maintenance_window
      cloudwatch_log_group       = aws_cloudwatch_log_group.elasticache.name

      # Live endpoints (resolved after cluster is created)
      primary_endpoint           = var.cluster_mode_enabled ? null : aws_elasticache_replication_group.main.primary_endpoint_address
      reader_endpoint            = var.cluster_mode_enabled ? null : aws_elasticache_replication_group.main.reader_endpoint_address
      configuration_endpoint     = var.cluster_mode_enabled ? aws_elasticache_replication_group.main.configuration_endpoint_address : null
      replication_group_arn      = aws_elasticache_replication_group.main.arn
    }

    # ----- memtier_benchmark configuration -----
    memtier = {
      task_count        = var.loadgen_task_count
      threads           = var.loadgen_memtier_threads
      clients           = var.loadgen_memtier_clients
      pipeline          = var.loadgen_memtier_pipeline
      data_size_bytes   = var.loadgen_memtier_data_size
      ratio             = var.loadgen_memtier_ratio
      key_pattern       = var.loadgen_memtier_key_pattern
      key_maximum       = local.memtier_key_maximum
      key_maximum_total = local.memtier_key_maximum * var.loadgen_task_count
      key_maximum_auto  = var.loadgen_memtier_key_maximum == 0
      auto_sizing       = {
        writable_shard_count = local._writable_shard_count
        target_fill_bytes    = local._target_fill_bytes
        target_total_keys    = local._target_total_keys
      }
      test_time_seconds = local.memtier_test_time_seconds
      duration_label    = local.memtier_duration_label
      tls               = var.transit_encryption_enabled
      cluster_mode      = var.cluster_mode_enabled
    }

    # ----- ECS load generator resources -----
    ecs = {
      cluster_name    = local.loadgen_cluster_name
      service_name    = local.loadgen_service_name
      fargate_cpu     = var.loadgen_cpu
      fargate_memory  = var.loadgen_memory
      container_insights_mode = var.ecs_container_insights_mode
    }

    # ----- Node type memory reference table (all known types) -----
    node_memory_table = local._node_memory_bytes
  })

  content_type = "application/json"

  # Overwrite if a re-apply happens for the same run (edge case: apply failed
  # and was re-run within the same time_static tick).
  etag = md5(jsonencode({
    cluster_id = local.cluster_id
    node_type  = var.node_type
  }))

  tags = {
    Name      = "${local.cluster_id}-cluster-details"
    RunFolder = local.run_folder
  }

  depends_on = [aws_elasticache_replication_group.main]
}
