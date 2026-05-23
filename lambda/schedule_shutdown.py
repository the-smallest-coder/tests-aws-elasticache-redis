import html
import os
import re
from datetime import datetime, timedelta, timezone

import boto3

ecs = boto3.client("ecs")
events = boto3.client("events")


def _html(value):
    return html.escape(str(value), quote=True)


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


def _send_email(subject, body_text, body_html=None):
    config = _ses_config()
    if not config:
        print("Email notification disabled (NOTIFICATION_EMAIL or SES_IDENTITY_ARN not set)")
        return None

    body_payload = {"Text": {"Data": body_text, "Charset": "UTF-8"}}
    if body_html:
        body_payload["Html"] = {"Data": body_html, "Charset": "UTF-8"}

    response = config["client"].send_email(
        Source=config["source"],
        Destination={"ToAddresses": [config["to"]]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": body_payload
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
    body_text = (
        f"ElastiCache performance test started.\n\n"
        f"Cluster: {cluster_id or service}\n"
        f"Shutdown scheduled at: {shutdown_time.isoformat()}\n"
        f"Verify scheduled at: {verify_time.isoformat()}\n"
    )

    engine_type = os.environ.get("ENGINE_TYPE", "redis").capitalize()
    engine_version = os.environ.get("ENGINE_VERSION", "")
    node_type = os.environ.get("NODE_TYPE", "")
    node_count = os.environ.get("NODE_COUNT", "")
    loadgen_tasks = os.environ.get("LOADGEN_TASK_COUNT", "")
    aws_region = os.environ.get("AWS_REGION_NAME", "")

    remaining = shutdown_time - datetime.now(timezone.utc)
    remaining_min = int(remaining.total_seconds() / 60)
    remaining_h = remaining_min // 60
    remaining_m = remaining_min % 60
    eta_str = f"{remaining_h}h {remaining_m}m" if remaining_h > 0 else f"{remaining_m}m"
    cluster_label_html = _html(cluster_id or service)
    engine_label_html = _html(f"{engine_type} {engine_version}")
    node_type_html = _html(node_type)
    node_count_html = _html(node_count)
    loadgen_tasks_html = _html(loadgen_tasks)
    duration_minutes_html = _html(duration_minutes)
    aws_region_html = _html(aws_region)
    eta_html = _html(eta_str)
    shutdown_time_short_html = _html(shutdown_time.strftime('%Y-%m-%d %H:%M UTC'))
    shutdown_time_full_html = _html(shutdown_time.strftime('%Y-%m-%d %H:%M:%S'))
    verify_time_full_html = _html(verify_time.strftime('%Y-%m-%d %H:%M:%S'))

    body_html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f4f6f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f9;padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1a73e8,#0d47a1);padding:30px 40px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  <span style="font-size:14px;color:rgba(255,255,255,0.85);text-transform:uppercase;letter-spacing:1px;">Performance Test</span>
                  <h1 style="margin:6px 0 0;font-size:24px;color:#ffffff;font-weight:600;">&#9889; Test Started</h1>
                </td>
                <td align="right" valign="top">
                  <span style="display:inline-block;background:rgba(255,255,255,0.2);color:#fff;padding:6px 14px;border-radius:20px;font-size:13px;font-weight:500;">
                    {engine_label_html}
                  </span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Cluster ID bar -->
        <tr>
          <td style="background-color:#e8f0fe;padding:14px 40px;border-bottom:1px solid #d2e3fc;">
            <span style="font-size:13px;color:#5f6368;">Cluster</span><br>
            <span style="font-size:16px;color:#1a73e8;font-weight:600;font-family:monospace;">{cluster_label_html}</span>
          </td>
        </tr>

        <!-- Config Grid -->
        <tr>
          <td style="padding:28px 40px 10px;">
            <h2 style="margin:0 0 16px;font-size:15px;color:#5f6368;text-transform:uppercase;letter-spacing:0.5px;">Configuration</h2>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">Engine</span><br>
                  <span style="font-size:15px;color:#202124;font-weight:500;">{engine_label_html}</span>
                </td>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">Node Type</span><br>
                  <span style="font-size:15px;color:#202124;font-weight:500;">{node_type_html}</span>
                </td>
              </tr>
              <tr>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">Cache Nodes</span><br>
                  <span style="font-size:15px;color:#202124;font-weight:500;">{node_count_html}</span>
                </td>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">Load Generator Tasks</span><br>
                  <span style="font-size:15px;color:#202124;font-weight:500;">{loadgen_tasks_html}</span>
                </td>
              </tr>
              <tr>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">Test Duration</span><br>
                  <span style="font-size:15px;color:#202124;font-weight:500;">{duration_minutes_html} min</span>
                </td>
                <td width="50%" style="padding:8px 0;">
                  <span style="font-size:12px;color:#80868b;">Region</span><br>
                  <span style="font-size:15px;color:#202124;font-weight:500;">{aws_region_html}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Countdown -->
        <tr>
          <td style="padding:20px 40px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#fef7e0;border-radius:8px;border-left:4px solid #f9ab00;">
              <tr>
                <td style="padding:16px 20px;">
                  <span style="font-size:13px;color:#e37400;font-weight:600;">&#9200; ESTIMATED TIME REMAINING</span><br>
                  <span style="font-size:22px;color:#202124;font-weight:700;">{eta_html}</span>
                  <span style="font-size:13px;color:#5f6368;margin-left:10px;">&#8594; Shutdown at {shutdown_time_short_html}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Schedule Details -->
        <tr>
          <td style="padding:10px 40px 28px;">
            <h2 style="margin:0 0 12px;font-size:15px;color:#5f6368;text-transform:uppercase;letter-spacing:0.5px;">Schedule</h2>
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e8eaed;border-radius:6px;overflow:hidden;">
              <tr style="background-color:#f8f9fa;">
                <td style="padding:10px 16px;font-size:13px;color:#5f6368;border-bottom:1px solid #e8eaed;">Event</td>
                <td style="padding:10px 16px;font-size:13px;color:#5f6368;border-bottom:1px solid #e8eaed;">Scheduled At (UTC)</td>
              </tr>
              <tr>
                <td style="padding:10px 16px;font-size:14px;color:#202124;border-bottom:1px solid #e8eaed;">&#128308; Shutdown</td>
                <td style="padding:10px 16px;font-size:14px;color:#202124;font-family:monospace;border-bottom:1px solid #e8eaed;">{shutdown_time_full_html}</td>
              </tr>
              <tr>
                <td style="padding:10px 16px;font-size:14px;color:#202124;">&#9989; Verification</td>
                <td style="padding:10px 16px;font-size:14px;color:#202124;font-family:monospace;">{verify_time_full_html}</td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background-color:#f8f9fa;padding:20px 40px;border-top:1px solid #e8eaed;">
            <p style="margin:0;font-size:12px;color:#80868b;text-align:center;">
              Automated notification from ElastiCache Performance Lab&nbsp;&nbsp;&#8226;&nbsp;&nbsp;{aws_region_html}
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    _send_email(subject, body_text, body_html)

    print(f"Scheduled shutdown at {shutdown_time.isoformat()} using {cron_expr}.")
    return {
        "scheduled": True,
        "shutdown_time": shutdown_time.isoformat(),
        "verify_time": verify_time.isoformat()
    }
