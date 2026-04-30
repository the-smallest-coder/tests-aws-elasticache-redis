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


def _build_verify_html(header_title, header_bg, cluster_id, rows, aws_region=""):
    """Build a styled HTML email for verification results."""
    rows_html = ""
    for label, value, ok in rows:
        color = "#1e8e3e" if ok else "#d93025"
        icon = "&#9989;" if ok else "&#9888;&#65039;"
        rows_html += f"""\
              <tr>
                <td style="padding:10px 16px;font-size:14px;color:#202124;border-bottom:1px solid #e8eaed;">{label}</td>
                <td style="padding:10px 16px;font-size:14px;color:{color};font-weight:500;border-bottom:1px solid #e8eaed;">{icon} {value}</td>
              </tr>"""

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background-color:#f4f6f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f9;padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:{header_bg};padding:30px 40px;">
            <span style="font-size:14px;color:rgba(255,255,255,0.85);text-transform:uppercase;letter-spacing:1px;">Verification Check</span>
            <h1 style="margin:6px 0 0;font-size:24px;color:#ffffff;font-weight:600;">{header_title}</h1>
          </td>
        </tr>

        <!-- Cluster ID bar -->
        <tr>
          <td style="background-color:#e8f0fe;padding:14px 40px;border-bottom:1px solid #d2e3fc;">
            <span style="font-size:13px;color:#5f6368;">Cluster</span><br>
            <span style="font-size:16px;color:#1a73e8;font-weight:600;font-family:monospace;">{cluster_id}</span>
          </td>
        </tr>

        <!-- Status Table -->
        <tr>
          <td style="padding:28px 40px;">
            <h2 style="margin:0 0 16px;font-size:15px;color:#5f6368;text-transform:uppercase;letter-spacing:0.5px;">Resource Status</h2>
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e8eaed;border-radius:6px;overflow:hidden;">
              <tr style="background-color:#f8f9fa;">
                <td style="padding:10px 16px;font-size:13px;color:#5f6368;border-bottom:1px solid #e8eaed;">Resource</td>
                <td style="padding:10px 16px;font-size:13px;color:#5f6368;border-bottom:1px solid #e8eaed;">Status</td>
              </tr>
{rows_html}
            </table>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background-color:#f8f9fa;padding:20px 40px;border-top:1px solid #e8eaed;">
            <p style="margin:0;font-size:12px;color:#80868b;text-align:center;">
              Automated notification from ElastiCache Performance Lab&nbsp;&nbsp;&#8226;&nbsp;&nbsp;{aws_region}
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


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
    aws_region = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_DEFAULT_REGION", ""))

    ecs_is_running, ecs_detail = _ecs_running(ecs_cluster, ecs_service)
    elasticache_is_running, elasticache_detail = _elasticache_running(elasticache_id)

    if ecs_is_running:
        subject = f"[ElastiCache Test Warning] ECS tasks still running ({cluster_id})"
        body_text = (
            f"ECS tasks are still running.\n\n"
            f"Cluster: {cluster_id}\n"
            f"Service: {ecs_service}\n"
            f"Details: {ecs_detail}\n"
        )
        body_html = _build_verify_html(
            "&#9888;&#65039; ECS Tasks Still Running",
            "linear-gradient(135deg,#e37400,#c56200)",
            cluster_id,
            [("ECS Service", ecs_detail, False)],
            aws_region
        )
        _send_email(subject, body_text, body_html)

    if elasticache_is_running:
        subject = f"[ElastiCache Test Warning] ElastiCache still running ({cluster_id})"
        body_text = (
            f"ElastiCache replication group still exists.\n\n"
            f"Cluster: {cluster_id}\n"
            f"ReplicationGroupId: {elasticache_id}\n"
            f"Details: {elasticache_detail}\n"
        )
        body_html = _build_verify_html(
            "&#9888;&#65039; ElastiCache Still Running",
            "linear-gradient(135deg,#e37400,#c56200)",
            cluster_id,
            [(f"ElastiCache ({elasticache_id})", elasticache_detail, False)],
            aws_region
        )
        _send_email(subject, body_text, body_html)

    if not ecs_is_running and not elasticache_is_running:
        subject = f"[ElastiCache Test OK] All resources shut down ({cluster_id})"
        body_text = (
            f"Shutdown verification completed.\n\n"
            f"Cluster: {cluster_id}\n"
            f"ECS: {ecs_detail}\n"
            f"ElastiCache: {elasticache_detail}\n"
        )
        body_html = _build_verify_html(
            "&#9989; All Resources Shut Down",
            "linear-gradient(135deg,#1e8e3e,#137333)",
            cluster_id,
            [
                ("ECS Service", ecs_detail, True),
                (f"ElastiCache ({elasticache_id})", elasticache_detail, True),
            ],
            aws_region
        )
        _send_email(subject, body_text, body_html)

    return {
        "ecs_running": ecs_is_running,
        "ecs_detail": ecs_detail,
        "elasticache_running": elasticache_is_running,
        "elasticache_detail": elasticache_detail
    }
