import os
import re

import boto3

ecs = boto3.client("ecs")
elasticache = boto3.client("elasticache")


def _ses_config():
    email = os.environ.get("NOTIFICATION_EMAIL", "").strip()
    ses_arn = os.environ.get("SES_IDENTITY_ARN", "").strip()
    if not email or not ses_arn:
        return None

    arn_match = re.match(r"arn:aws:ses:([^:]+):[^:]+:identity/(.+)", ses_arn)
    if not arn_match:
        print(f"Invalid SES ARN format: {ses_arn}")
        return None

    ses_region = arn_match.group(1)
    domain = arn_match.group(2)
    source_email = f"aws@{domain}"
    return {
        "client": boto3.client("ses", region_name=ses_region),
        "source": source_email,
        "to": email
    }


def _send_email(subject, body):
    config = _ses_config()
    if not config:
        print("Email notification disabled (NOTIFICATION_EMAIL or SES_IDENTITY_ARN not set)")
        return None

    response = config["client"].send_email(
        Source=config["source"],
        Destination={"ToAddresses": [config["to"]]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}}
        }
    )
    print(f"Notification sent to {config['to']}, MessageId: {response['MessageId']}")
    return True


def _ecs_running(cluster, service):
    try:
        resp = ecs.describe_services(cluster=cluster, services=[service])
        services = resp.get("services", [])
        if not services:
            return False, "service_not_found"
        svc = services[0]
        running = svc.get("runningCount", 0)
        desired = svc.get("desiredCount", 0)
        status = svc.get("status", "unknown")
        return running > 0, f"running={running} desired={desired} status={status}"
    except Exception as exc:
        return True, f"describe_failed: {exc}"


def _elasticache_running(replication_group_id):
    try:
        resp = elasticache.describe_replication_groups(
            ReplicationGroupId=replication_group_id
        )
        groups = resp.get("ReplicationGroups", [])
        if not groups:
            return False, "not_found"
        status = groups[0].get("Status", "unknown")
        return True, f"status={status}"
    except elasticache.exceptions.ReplicationGroupNotFoundFault:
        return False, "not_found"
    except Exception as exc:
        return True, f"describe_failed: {exc}"


def handler(event, context):
    cluster_id = os.environ.get("CLUSTER_ID", "")
    ecs_cluster = os.environ["ECS_CLUSTER"]
    ecs_service = os.environ["ECS_SERVICE"]
    elasticache_id = os.environ["ELASTICACHE_ID"]

    ecs_is_running, ecs_detail = _ecs_running(ecs_cluster, ecs_service)
    elasticache_is_running, elasticache_detail = _elasticache_running(elasticache_id)

    if ecs_is_running:
        subject = f"[ElastiCache Test Warning] ECS tasks still running ({cluster_id})"
        body = (
            f"ECS tasks are still running.\n\n"
            f"Cluster: {cluster_id}\n"
            f"Service: {ecs_service}\n"
            f"Details: {ecs_detail}\n"
        )
        _send_email(subject, body)

    if elasticache_is_running:
        subject = f"[ElastiCache Test Warning] ElastiCache still running ({cluster_id})"
        body = (
            f"ElastiCache replication group still exists.\n\n"
            f"Cluster: {cluster_id}\n"
            f"ReplicationGroupId: {elasticache_id}\n"
            f"Details: {elasticache_detail}\n"
        )
        _send_email(subject, body)

    if not ecs_is_running and not elasticache_is_running:
        subject = f"[ElastiCache Test OK] All resources shut down ({cluster_id})"
        body = (
            f"Shutdown verification completed.\n\n"
            f"Cluster: {cluster_id}\n"
            f"ECS: {ecs_detail}\n"
            f"ElastiCache: {elasticache_detail}\n"
        )
        _send_email(subject, body)

    return {
        "ecs_running": ecs_is_running,
        "ecs_detail": ecs_detail,
        "elasticache_running": elasticache_is_running,
        "elasticache_detail": elasticache_detail
    }
