# AWS ElastiCache Performance Testing Infrastructure

Terraform infrastructure for **automated** ElastiCache (Redis/Valkey) performance testing.

> `terraform apply` → auto-run load tests → auto-export metrics → auto-stop → `terraform destroy`

---

## 🚀 Quick Start

```bash
# 1. Create S3 bucket for exports (REQUIRED)
aws s3 mb s3://my-elasticache-perf-exports

# 2. Configure
cp terraform.tfvars.example terraform.tfvars
# Edit: vpc_id, subnet_ids, metrics_export_s3_bucket

# 3. Deploy
terraform init
terraform apply

# 4. Cleanup (after test auto-completes)
terraform destroy
```

> ⚠️ **`metrics_export_s3_bucket` is required** - Terraform will error if not set.

---

## 📋 Prerequisites

- Terraform >= 1.0
- AWS CLI configured
- Existing VPC + private subnets
- S3 bucket for exports

---

## 🎯 What It Does

1. **Provisions** ElastiCache (Redis/Valkey) + ECS load generators
2. **Runs** memtier_benchmark for configurable duration (default: 1 hour)
3. **Exports** metrics (CSV) + logs (text) to S3
4. **Stops** ECS and ElastiCache automatically

---

## 🏗️ Architecture

```mermaid
flowchart TB
    subgraph VPC["VPC"]
        subgraph Subnets["Private Subnets"]
            subgraph ECS_Cluster["ECS Cluster (Fargate)"]
                ECS_Tasks["memtier_benchmark<br/>Tasks"]
            end
            subgraph ElastiCache_Cluster["ElastiCache"]
                Redis["Redis/Valkey<br/>Replication Group"]
            end
        end
        SG_ECS["Security Group<br/>Load Generator"]
        SG_EC["Security Group<br/>ElastiCache"]
    end
    
    subgraph AWS_Services["AWS Services"]
        EventBridge["EventBridge<br/>Scheduler"]
        Lambda["Lambda<br/>Shutdown"]
        CloudWatch["CloudWatch<br/>Logs & Metrics"]
        S3["S3<br/>Exports"]
    end
    
    ECS_Tasks -->|"port 6379"| Redis
    SG_ECS -.->|allows| ECS_Tasks
    SG_EC -.->|allows from SG_ECS| Redis
    ECS_Tasks -->|logs| CloudWatch
    Redis -->|metrics| CloudWatch
    EventBridge -->|triggers| Lambda
    Lambda -->|reads| CloudWatch
    Lambda -->|exports| S3
```

---

## 🔄 Workflow

```mermaid
flowchart LR
    subgraph Start["terraform apply"]
        S1[Create ElastiCache] --> S2[Create ECS Cluster]
        S2 --> S3[Start memtier Tasks]
        S3 --> S4[Schedule EventBridge]
    end
    
    subgraph Run["Load Test"]
        R1[ECS memtier] -->|load test| R2[ElastiCache]
        R2 -->|metrics| R3[CloudWatch]
    end
    
    subgraph Stop["After Duration"]
        T1[EventBridge] -->|triggers| T2[Lambda]
        T2 -->|export logs| T3[S3]
        T2 -->|export metrics| T3
        T2 -->|stop| T4[ECS Service]
        T2 -->|stop| T5[ElastiCache]
    end
    
    Start --> Run --> Stop
```

---

## � Exports

| Data | Format | Path |
|------|--------|------|
| ElastiCache Metrics | CSV | `s3://{bucket}/exports/{timestamp}/metrics/{cluster}.csv` |
| ECS Task Metrics | CSV | `s3://{bucket}/exports/{timestamp}/metrics/{cluster}-ecs.csv` |
| Logs | Text | `s3://{bucket}/exports/{timestamp}/logs/{cluster}.txt` |

---

## 🔧 Configuration

Key variables in `terraform.tfvars`:

| Variable | Default | Description |
|----------|---------|-------------|
| `test_duration_minutes` | 60 | Minutes before auto-shutdown |
| `loadgen_task_count` | 1 | ECS tasks (scale factor) |
| `node_type` | cache.t4g.micro | ElastiCache instance |
| `engine_type` | redis | redis or valkey |

See `terraform.tfvars.example` for all options.

