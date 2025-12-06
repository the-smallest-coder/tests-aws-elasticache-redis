# AWS ElastiCache Performance Testing Infrastructure

This repository contains Terraform code to provision **ephemeral** AWS ElastiCache resources (Redis/Valkey) for performance testing and network throughput analysis.

The idea is simple:

> `terraform apply` → run 1-hour ECS-driven load tests → export metrics/plots for the blog → `terraform destroy`.

Nothing in this stack is meant to be long-lived; you spin it up when you want to run a benchmark campaign and then tear it down.

---

## 🎯 Project Goals

- **Performance Testing**  
  Provision ElastiCache configurations (engine, topology, instance type) to test how they behave under 1-hour synthetic workloads.

- **Network & Resource Analysis**  
  Observe actual network throughput, CPU and memory usage and compare them with documented limits for the underlying instance types.

- **Decision Support**  
  Generate graphs and summaries that can feed into blog posts about how to choose:
  - Redis vs Valkey  
  - single instance vs cluster  
  - instance sizes for different workload patterns

- **Flexibility**  
  Support both Redis and Valkey engines, cluster and non-cluster modes, and allow you to sweep through multiple instance types and configurations via variables.

- **Observability**  
  Provision CloudWatch log groups and dashboards so you can:
  - monitor 1-hour test runs in real time  
  - export metrics for offline visualisation (PNG/PDF) and blog content.

---

## 📋 Prerequisites

- **Terraform**: >= 1.0  
- **AWS CLI**: configured with appropriate credentials  
- **Existing VPC**: VPC + private subnets where ElastiCache will live  
- **AWS Region**: default is `us-east-1` (cheap for testing; override if you prefer another region)

> This repo only manages ElastiCache-side infrastructure and basic observability.  
> Load generation (ECS tasks running tools like `memtier_benchmark`) lives in a separate repository.

---

## 🏗️ Architecture Overview

```mermaid
graph TB
    subgraph VPC["Existing VPC (your VPC)"]
        subgraph Subnets["Subnet Group"]
            S1[Subnet 1]
            S2[Subnet 2]
        end
        
        subgraph Cache["ElastiCache (Redis / Valkey)"]
            direction LR
            Node1["Replication Group<br/>Single-node or Cluster"]
        end
        
        SG["Security Group<br/>Allows VPC Access"]
        
        subgraph ECS["ECS Load Generators (separate repo)"]
            Client1[memtier Task 1]
            Client2[memtier Task 2]
            ClientN["memtier Task N (>=5)"]
        end
        
        Client1 -.->|connects to| Node1
        Client2 -.->|connects to| Node1
        ClientN -.->|connects to| Node1
        SG -->|controls access| Node1
        Subnets -->|hosts| Node1
    end
    
    subgraph CloudWatch["AWS CloudWatch"]
        Logs[Log Groups<br/>ElastiCache & Loadgen]
        Dashboard["Dashboard<br/>Key Metrics"]
    end
    
    Node1 -->|sends metrics| Dashboard
    Node1 -->|sends logs| Logs

    style VPC fill:#e1f5ff,stroke:#0066cc,stroke-width:2px
    style Cache fill:#fff4e6,stroke:#ff9800,stroke-width:2px
    style CloudWatch fill:#f3e5f5,stroke:#9c27b0,stroke-width:2px
    style ECS fill:#e8f5e9,stroke:#4caf50,stroke-width:2px,stroke-dasharray: 5 5
