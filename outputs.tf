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

output "connection_instructions" {
  description = "Instructions for connecting to the cluster"
  value = <<-EOT
    
    Connection Details:
    -------------------
    Endpoint: ${var.cluster_mode_enabled ? aws_elasticache_replication_group.main.configuration_endpoint_address : aws_elasticache_replication_group.main.primary_endpoint_address}
    Port: ${var.port}
    Engine: ${var.engine_type} ${var.engine_version}
    Mode: ${var.cluster_mode_enabled ? "Cluster Mode Enabled" : "Non-Cluster Mode"}
    
    ${var.cluster_mode_enabled ? "Using redis-cli:" : "Using redis-cli:"}
    redis-cli -h ${var.cluster_mode_enabled ? aws_elasticache_replication_group.main.configuration_endpoint_address : aws_elasticache_replication_group.main.primary_endpoint_address} -p ${var.port}${var.cluster_mode_enabled ? " -c" : ""}${var.transit_encryption_enabled ? " --tls" : ""}
    
    ${var.transit_encryption_enabled && var.auth_token != null ? "Note: AUTH token required for authentication" : ""}
    
    Security Group ID (add to ECS task SG): ${aws_security_group.elasticache.id}
  EOT
}
