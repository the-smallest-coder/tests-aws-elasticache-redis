variable "aws_region" {
  description = "AWS region for resources"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name for resource naming and tagging"
  type        = string
  default     = "elasticache-perf-test"
}

variable "environment" {
  description = "Environment name (e.g., test, dev, prod)"
  type        = string
  default     = "test"
}

# VPC and Networking
variable "vpc_id" {
  description = "ID of existing VPC where ElastiCache will be deployed"
  type        = string
}

variable "subnet_ids" {
  description = "List of subnet IDs for ElastiCache subnet group"
  type        = list(string)
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed to connect to ElastiCache (defaults to VPC CIDR if empty)"
  type        = list(string)
  default     = []
}

# ElastiCache Configuration
variable "engine_type" {
  description = "Cache engine type: redis or valkey"
  type        = string
  default     = "redis"

  validation {
    condition     = contains(["redis", "valkey"], var.engine_type)
    error_message = "Engine type must be either 'redis' or 'valkey'."
  }
}

variable "engine_version" {
  description = "Engine version (e.g., 7.1 for Redis, 7.2 for Valkey)"
  type        = string
  default     = "7.1"
}

variable "node_type" {
  description = "ElastiCache node instance type"
  type        = string
  default     = "cache.t4g.micro"
}

variable "cluster_mode_enabled" {
  description = "Enable Redis/Valkey cluster mode (sharding)"
  type        = bool
  default     = false
}

# Non-cluster mode settings
variable "num_cache_nodes" {
  description = "Number of cache nodes for non-cluster mode (including primary)"
  type        = number
  default     = 1

  validation {
    condition     = var.num_cache_nodes >= 1 && var.num_cache_nodes <= 6
    error_message = "Number of cache nodes must be between 1 and 6."
  }
}

# Cluster mode settings
variable "num_node_groups" {
  description = "Number of node groups (shards) for cluster mode"
  type        = number
  default     = 1

  validation {
    condition     = var.num_node_groups >= 1 && var.num_node_groups <= 500
    error_message = "Number of node groups must be between 1 and 500."
  }
}

variable "replicas_per_node_group" {
  description = "Number of replica nodes per shard in cluster mode"
  type        = number
  default     = 0

  validation {
    condition     = var.replicas_per_node_group >= 0 && var.replicas_per_node_group <= 5
    error_message = "Replicas per node group must be between 0 and 5."
  }
}

# Performance and Configuration
variable "port" {
  description = "Port number for ElastiCache"
  type        = number
  default     = 6379
}

variable "parameter_group_family" {
  description = "Parameter group family (auto-determined from engine and version if not set)"
  type        = string
  default     = ""
}

variable "parameter_group_settings" {
  description = "Custom parameter group settings as key-value pairs"
  type        = map(string)
  default     = {}
}

variable "snapshot_retention_limit" {
  description = "Number of days to retain snapshots (0 to disable)"
  type        = number
  default     = 0
}

variable "snapshot_window" {
  description = "Daily time range for snapshots (HH:MM-HH:MM in UTC)"
  type        = string
  default     = "03:00-05:00"
}

variable "maintenance_window" {
  description = "Weekly time range for maintenance (ddd:HH:MM-ddd:HH:MM in UTC)"
  type        = string
  default     = "sun:05:00-sun:07:00"
}

variable "automatic_failover_enabled" {
  description = "Enable automatic failover (requires multi-node setup)"
  type        = bool
  default     = false
}

variable "at_rest_encryption_enabled" {
  description = "Enable encryption at rest"
  type        = bool
  default     = false
}

variable "transit_encryption_enabled" {
  description = "Enable encryption in transit (TLS)"
  type        = bool
  default     = false
}

variable "auth_token" {
  description = "Auth token for Redis AUTH (requires transit_encryption_enabled)"
  type        = string
  default     = null
  sensitive   = true
}

# CloudWatch
variable "cloudwatch_log_retention_days" {
  description = "Number of days to retain CloudWatch logs"
  type        = number
  default     = 7
}

variable "enable_cloudwatch_dashboard" {
  description = "Create CloudWatch dashboard for monitoring"
  type        = bool
  default     = true
}

# Tags
variable "tags" {
  description = "Additional tags for resources"
  type        = map(string)
  default     = {}
}
