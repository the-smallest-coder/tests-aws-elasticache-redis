import os
from datetime import datetime, timedelta, timezone

import boto3

ecs = boto3.client("ecs")
events = boto3.client("events")


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
    cluster = os.environ["ECS_CLUSTER"]
    service = os.environ["ECS_SERVICE"]
    rule_name = os.environ["SHUTDOWN_RULE_NAME"]
    duration_minutes = int(os.environ.get("TEST_DURATION_MINUTES", "60"))
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

    print(f"Scheduled shutdown at {shutdown_time.isoformat()} using {cron_expr}.")
    return {"scheduled": True, "shutdown_time": shutdown_time.isoformat()}
