import os
import re
from datetime import datetime, timedelta, timezone

import boto3

ecs = boto3.client("ecs")
events = boto3.client("events")


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
    identity = arn_match.group(2)
    
    # If identity is an email address, use it as-is; otherwise it's a domain
    if "@" in identity:
        source_email = identity
    else:
        source_email = f"aws-elasticache-lab@{identity}"
    
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


def _parse_cron_expression(expr):
    if not (expr.startswith("cron(") and expr.endswith(")")):
        return None

    parts = expr[5:-1].split()
    if len(parts) != 6:
        return None

    minute, hour, day, month, day_of_week, year = parts
    if day_of_week != "?":
        return None

    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _cron_at(dt):
    return f"cron({dt.minute} {dt.hour} {dt.day} {dt.month} ? {dt.year})"


def handler(event, context):
    cluster_id = os.environ.get("CLUSTER_ID", "")
    cluster = os.environ["ECS_CLUSTER"]
    service = os.environ["ECS_SERVICE"]
    rule_name = os.environ["SHUTDOWN_RULE_NAME"]
    verify_rule_name = os.environ.get("VERIFY_RULE_NAME", "")
    duration_minutes = int(os.environ.get("TEST_DURATION_MINUTES", "60"))
    verify_delay_minutes = int(os.environ.get("VERIFY_DELAY_MINUTES", str(duration_minutes + 15)))
    placeholder_schedule = os.environ.get("SHUTDOWN_RULE_PLACEHOLDER", "cron(0 0 1 1 ? 2099)")

    resp = ecs.describe_services(cluster=cluster, services=[service])
    services = resp.get("services", [])
    if not services:
        print("ECS service not found; skipping shutdown scheduling.")
        return {"scheduled": False, "reason": "service_not_found"}

    svc = services[0]
    desired_count = svc.get("desiredCount", 0)
    running_count = svc.get("runningCount", 0)
    if desired_count < 1 or running_count < 1:
        print("Service not running; skipping shutdown scheduling.")
        return {"scheduled": False, "reason": "not_running"}

    rule = events.describe_rule(Name=rule_name)
    schedule_expression = rule.get("ScheduleExpression", "")
    if schedule_expression == placeholder_schedule:
        schedule_expression = ""

    scheduled_at = _parse_cron_expression(schedule_expression)
    now = datetime.now(timezone.utc)
    if scheduled_at and scheduled_at > now + timedelta(minutes=1):
        print(f"Shutdown already scheduled for {scheduled_at.isoformat()}.")
        return {"scheduled": False, "reason": "already_scheduled"}

    shutdown_time = now + timedelta(minutes=duration_minutes)
    cron_expr = _cron_at(shutdown_time)

    events.put_rule(
        Name=rule_name,
        ScheduleExpression=cron_expr,
        State="ENABLED",
    )

    verify_time = now + timedelta(minutes=verify_delay_minutes)
    if verify_rule_name:
        events.put_rule(
            Name=verify_rule_name,
            ScheduleExpression=_cron_at(verify_time),
            State="ENABLED",
        )
    else:
        print("VERIFY_RULE_NAME not set; skipping verify scheduling.")

    subject = f"[ElastiCache Test Started] {cluster_id or service}"
    body = (
        f"ElastiCache performance test started.\n\n"
        f"Cluster: {cluster_id or service}\n"
        f"Shutdown scheduled at: {shutdown_time.isoformat()}\n"
        f"Verify scheduled at: {verify_time.isoformat()}\n"
    )
    _send_email(subject, body)

    print(f"Scheduled shutdown at {shutdown_time.isoformat()} using {cron_expr}.")
    return {
        "scheduled": True,
        "shutdown_time": shutdown_time.isoformat(),
        "verify_time": verify_time.isoformat()
    }
