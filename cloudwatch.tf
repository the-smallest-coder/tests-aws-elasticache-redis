resource "aws_cloudwatch_dashboard" "elasticache" {
  count = var.enable_cloudwatch_dashboard ? 1 : 0

  dashboard_name = "${local.cluster_id}-dashboard"

  dashboard_body = jsonencode({
    widgets = [
      # Header with cluster info
      {
        type = "text"
        width = 24
        height = 1
        properties = {
          markdown = "# ElastiCache Performance Dashboard - ${var.engine_type} ${var.engine_version}\n**Instance Type:** ${var.node_type} | **Mode:** ${var.cluster_mode_enabled ? "Cluster" : "Non-Cluster"} | **Nodes:** ${var.cluster_mode_enabled ? var.num_node_groups : var.num_cache_nodes}"
        }
      },

      # Network Performance Section Header
      {
        type = "text"
        width = 24
        height = 1
        properties = {
          markdown = "## 📊 Network Performance (Primary Focus)"
        }
      },

      # Network Bytes In (Ingress) with rate calculation
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "NetworkBytesIn", { stat = "Sum", id = "m1" }],
            [{ expression = "m1/PERIOD(m1)/1024/1024", label = "Ingress (MB/s)", id = "e1" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Network Ingress Traffic"
          period  = 300
          yAxis = {
            left = {
              label = "MB/s"
            }
          }
          annotations = {
            horizontal = [
              {
                label = "Instance Network Limit (reference)"
                value = var.node_type == "cache.t4g.micro" ? 625 : (
                  var.node_type == "cache.t4g.small" ? 625 : (
                    var.node_type == "cache.t4g.medium" ? 625 : (
                      var.node_type == "cache.r7g.large" ? 1562.5 : (
                        var.node_type == "cache.r7g.xlarge" ? 1562.5 : 0
                      )
                    )
                  )
                )
                fill = "above"
                color = "#ff7f0e"
              }
            ]
          }
        }
      },

      # Network Bytes Out (Egress) with rate calculation
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "NetworkBytesOut", { stat = "Sum", id = "m1" }],
            [{ expression = "m1/PERIOD(m1)/1024/1024", label = "Egress (MB/s)", id = "e1" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Network Egress Traffic"
          period  = 300
          yAxis = {
            left = {
              label = "MB/s"
            }
          }
          annotations = {
            horizontal = [
              {
                label = "Instance Network Limit (reference)"
                value = var.node_type == "cache.t4g.micro" ? 625 : (
                  var.node_type == "cache.t4g.small" ? 625 : (
                    var.node_type == "cache.t4g.medium" ? 625 : (
                      var.node_type == "cache.r7g.large" ? 1562.5 : (
                        var.node_type == "cache.r7g.xlarge" ? 1562.5 : 0
                      )
                    )
                  )
                )
                fill = "above"
                color = "#ff7f0e"
              }
            ]
          }
        }
      },

      # Combined Network Throughput in Gbps
      {
        type   = "metric"
        width  = 24
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "NetworkBytesIn", { stat = "Sum", id = "m1", visible = false }],
            [".", "NetworkBytesOut", { stat = "Sum", id = "m2", visible = false }],
            [{ expression = "m1/PERIOD(m1)*8/1000/1000/1000", label = "Ingress (Gbps)", id = "e1" }],
            [{ expression = "m2/PERIOD(m2)*8/1000/1000/1000", label = "Egress (Gbps)", id = "e2" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Network Throughput (Gbps) vs AWS Limits"
          period  = 300
          yAxis = {
            left = {
              label = "Gbps"
              min   = 0
            }
          }
          annotations = {
            horizontal = [
              {
                label = "AWS Network Limit (${var.node_type})"
                value = var.node_type == "cache.t4g.micro" ? 5 : (
                  var.node_type == "cache.t4g.small" ? 5 : (
                    var.node_type == "cache.t4g.medium" ? 5 : (
                      var.node_type == "cache.r7g.large" ? 12.5 : (
                        var.node_type == "cache.r7g.xlarge" ? 12.5 : 10
                      )
                    )
                  )
                )
                fill = "above"
                color = "#d62728"
              }
            ]
          }
        }
      },

      # Performance Metrics Section Header
      {
        type = "text"
        width = 24
        height = 1
        properties = {
          markdown = "## ⚡ Performance Metrics"
        }
      },

      # CPU Utilization
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "CPUUtilization", { stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "CPU Utilization"
          period  = 300
          yAxis = {
            left = {
              label = "Percent"
              min   = 0
              max   = 100
            }
          }
        }
      },

      # Database Memory Usage Percentage
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "DatabaseMemoryUsagePercentage", { stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Memory Usage"
          period  = 300
          yAxis = {
            left = {
              label = "Percent"
              min   = 0
              max   = 100
            }
          }
        }
      },

      # Swap Usage
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "SwapUsage", { stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Swap Usage"
          period  = 300
          yAxis = {
            left = {
              label = "Bytes"
            }
          }
        }
      },

      # Cache Hit Rate
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "CacheHits", { stat = "Sum", id = "hits", visible = false }],
            [".", "CacheMisses", { stat = "Sum", id = "misses", visible = false }],
            [{ expression = "100*(hits/(hits+misses))", label = "Hit Rate %", id = "hitrate" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Cache Hit Rate"
          period  = 300
          yAxis = {
            left = {
              label = "Percent"
              min   = 0
              max   = 100
            }
          }
        }
      },

      # Cache Operations (Hits vs Misses)
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "CacheHits", { stat = "Sum" }],
            [".", "CacheMisses", { stat = "Sum" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Cache Hits vs Misses"
          period  = 300
        }
      },

      # Operations & Connections Section Header
      {
        type = "text"
        width = 24
        height = 1
        properties = {
          markdown = "## 🔄 Operations & Connections"
        }
      },

      # Current Connections
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "CurrConnections", { stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Current Connections"
          period  = 300
        }
      },

      # New Connections
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "NewConnections", { stat = "Sum" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "New Connections"
          period  = 300
        }
      },

      # Evictions
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "Evictions", { stat = "Sum" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Evictions"
          period  = 300
        }
      },

      # Commands Processed (GET/SET operations)
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "GetTypeCmds", { stat = "Sum" }],
            [".", "SetTypeCmds", { stat = "Sum" }],
            [".", "HashBasedCmds", { stat = "Sum" }],
            [".", "ListBasedCmds", { stat = "Sum" }],
            [".", "SetBasedCmds", { stat = "Sum" }],
            [".", "SortedSetBasedCmds", { stat = "Sum" }]
          ]
          view    = "timeSeries"
          stacked = true
          region  = var.aws_region
          title   = "Commands Processed by Type"
          period  = 300
        }
      },

      # Total Commands/sec
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "GetTypeCmds", { stat = "Sum", id = "m1", visible = false }],
            [".", "SetTypeCmds", { stat = "Sum", id = "m2", visible = false }],
            [".", "HashBasedCmds", { stat = "Sum", id = "m3", visible = false }],
            [".", "ListBasedCmds", { stat = "Sum", id = "m4", visible = false }],
            [".", "SetBasedCmds", { stat = "Sum", id = "m5", visible = false }],
            [".", "SortedSetBasedCmds", { stat = "Sum", id = "m6", visible = false }],
            [{ expression = "(m1+m2+m3+m4+m5+m6)/PERIOD(m1)", label = "Operations/sec", id = "e1" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Total Operations per Second"
          period  = 300
          yAxis = {
            left = {
              label = "ops/sec"
            }
          }
        }
      },

      # Additional Metrics Section Header
      {
        type = "text"
        width = 24
        height = 1
        properties = {
          markdown = "## 📈 Additional Metrics"
        }
      },

      # Current Items
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "CurrItems", { stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Current Items (Keys)"
          period  = 300
        }
      },

      # Memory Fragmentation Ratio
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "MemoryFragmentationRatio", { stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Memory Fragmentation"
          period  = 300
        }
      },

      # Replication Lag (if applicable)
      {
        type   = "metric"
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/ElastiCache", "ReplicationLag", { stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Replication Lag"
          period  = 300
          yAxis = {
            left = {
              label = "Seconds"
            }
          }
        }
      }
    ]
  })
}
