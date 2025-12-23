output "elasticache_cluster_id" {
  description = "ElastiCache replication group ID"
  value       = aws_elasticache_replication_group.main.id
}

output "elasticache_cluster_arn" {
  description = "ARN of the ElastiCache replication group"
  value       = aws_elasticache_replication_group.main.arn
}

output "elasticache_endpoint" {
  description = "Primary endpoint for ElastiCache cluster"
  value = var.cluster_mode_enabled ? aws_elasticache_replication_group.main.configuration_endpoint_address : (
    aws_elasticache_replication_group.main.primary_endpoint_address
  )
}

output "elasticache_reader_endpoint" {
  description = "Reader endpoint (non-cluster mode with replicas only)"
  value       = var.cluster_mode_enabled ? null : aws_elasticache_replication_group.main.reader_endpoint_address
}

output "elasticache_port" {
  description = "Port number for ElastiCache"
  value       = var.port
}

output "elasticache_connection_string" {
  description = "Connection string for Redis/Valkey clients"
  value = format("%s:%d",
    var.cluster_mode_enabled ? aws_elasticache_replication_group.main.configuration_endpoint_address : aws_elasticache_replication_group.main.primary_endpoint_address,
  var.port)
}

output "security_group_id" {
  description = "ID of the ElastiCache security group"
  value       = aws_security_group.elasticache.id
}

output "subnet_group_name" {
  description = "Name of the ElastiCache subnet group"
  value       = aws_elasticache_subnet_group.main.name
}

output "cloudwatch_log_group_name" {
  description = "CloudWatch log group name for ElastiCache logs"
  value       = aws_cloudwatch_log_group.elasticache.name
}

output "cloudwatch_dashboard_url" {
  description = "URL to CloudWatch dashboard"
  value = var.enable_cloudwatch_dashboard ? format(
    "https://console.aws.amazon.com/cloudwatch/home?region=%s#dashboards:name=%s",
    var.aws_region,
    aws_cloudwatch_dashboard.elasticache[0].dashboard_name
  ) : null
}

output "configuration_summary" {
  description = "Summary of applied configuration"
  value = {
    engine              = var.engine_type
    engine_version      = var.engine_version
    node_type           = var.node_type
    cluster_mode        = var.cluster_mode_enabled
    num_nodes_or_shards = var.cluster_mode_enabled ? var.num_node_groups : var.num_cache_nodes
    replicas_per_shard  = var.cluster_mode_enabled ? var.replicas_per_node_group : (var.num_cache_nodes > 1 ? var.num_cache_nodes - 1 : 0)
    encryption_at_rest  = var.at_rest_encryption_enabled
    encryption_transit  = var.transit_encryption_enabled
    automatic_failover  = var.automatic_failover_enabled
  }
}

# Load Generator Outputs
output "loadgen_cluster_name" {
  description = "ECS cluster name for load generators"
  value       = aws_ecs_cluster.loadgen.name
}

output "loadgen_service_name" {
  description = "ECS service name for load generators"
  value       = aws_ecs_service.loadgen.name
}

output "loadgen_log_group_name" {
  description = "CloudWatch log group for load generator output"
  value       = aws_cloudwatch_log_group.loadgen.name
}

output "loadgen_security_group_id" {
  description = "Security group ID for load generator tasks"
  value       = aws_security_group.loadgen.id
}

output "loadgen_configuration" {
  description = "Summary of load generator configuration"
  value = {
    task_count  = var.loadgen_task_count
    cpu         = var.loadgen_cpu
    memory      = var.loadgen_memory
    threads     = var.loadgen_memtier_threads
    clients     = var.loadgen_memtier_clients
    pipeline    = var.loadgen_memtier_pipeline
    data_size   = var.loadgen_memtier_data_size
    ratio       = var.loadgen_memtier_ratio
    test_time   = var.loadgen_memtier_test_time
    key_pattern = var.loadgen_memtier_key_pattern
  }
}

# Shutdown and Export Outputs
output "scheduled_shutdown_minutes" {
  description = "Minutes until auto-shutdown triggers"
  value       = var.test_duration_minutes
}

output "metrics_export_location" {
  description = "S3 location where metrics and logs will be exported"
  value       = "s3://${var.metrics_export_s3_bucket}/${var.metrics_export_s3_prefix}"
}

output "shutdown_lambda_name" {
  description = "Lambda function name for shutdown orchestration"
  value       = aws_lambda_function.shutdown.function_name
}
