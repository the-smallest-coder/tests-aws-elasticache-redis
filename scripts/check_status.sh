#!/usr/bin/env bash
#
# check_status.sh — Show current state of the ElastiCache performance test.
#
# Usage:
#   ./scripts/check_status.sh            # quick status
#   ./scripts/check_status.sh --detailed # verbose output
#
# Requires: AWS CLI configured, Terraform state accessible from project root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DETAILED=false

[[ "${1:-}" == "--detailed" ]] && DETAILED=true

# -- Resolve Terraform outputs --
echo ""
echo "=== Resolving Terraform outputs ==="

cd "$PROJECT_DIR"
TF_OUTPUT=$(terraform output -json 2>/dev/null) || {
    echo "ERROR: Failed to read Terraform outputs. Has 'terraform apply' been run?" >&2
    exit 1
}

REGION=$(echo "$TF_OUTPUT" | jq -r '.aws_region.value // "us-east-1"' 2>/dev/null || echo "us-east-1")
CLUSTER_NAME=$(echo "$TF_OUTPUT" | jq -r '.loadgen_cluster_name.value')
SERVICE_NAME=$(echo "$TF_OUTPUT" | jq -r '.loadgen_service_name.value')
CLUSTER_ID=$(echo "$TF_OUTPUT" | jq -r '.elasticache_cluster_id.value')
LOG_GROUP=$(echo "$TF_OUTPUT" | jq -r '.loadgen_log_group_name.value')
SHUTDOWN_LAMBDA=$(echo "$TF_OUTPUT" | jq -r '.shutdown_lambda_name.value')
S3_LOCATION=$(echo "$TF_OUTPUT" | jq -r '.metrics_export_location.value')
SCHEDULED_MIN=$(echo "$TF_OUTPUT" | jq -r '.scheduled_shutdown_minutes.value')
RUN_TIMESTAMP=$(echo "$TF_OUTPUT" | jq -r '.run_timestamp.value')

S3_BUCKET=$(echo "$S3_LOCATION" | sed 's|^s3://||' | cut -d/ -f1)
S3_PREFIX=$(echo "$S3_LOCATION" | sed "s|^s3://${S3_BUCKET}/||")

echo "  Region:           $REGION"
echo "  Cluster ID:       $CLUSTER_ID"
echo "  ECS Cluster:      $CLUSTER_NAME"
echo "  ECS Service:      $SERVICE_NAME"
echo "  Log Group:        $LOG_GROUP"
echo "  S3 Location:      $S3_LOCATION"
echo "  Shutdown Timer:   ${SCHEDULED_MIN} minutes"
echo "  Run Timestamp:    $RUN_TIMESTAMP"

# -- 1. ElastiCache Cluster Status --
echo ""
echo "=== ElastiCache Cluster ==="
EC_STATUS=$(aws elasticache describe-replication-groups \
    --replication-group-id "$CLUSTER_ID" \
    --region "$REGION" \
    --query "ReplicationGroups[0]" \
    --output json 2>/dev/null) || EC_STATUS=""

if [[ -n "$EC_STATUS" ]]; then
    STATUS=$(echo "$EC_STATUS" | jq -r '.Status')
    echo "  Status: $STATUS"

    echo "$EC_STATUS" | jq -r '.NodeGroups[]? | "    Node Group \(.NodeGroupId): \(.Status)"'

    if $DETAILED; then
        echo "$EC_STATUS" | jq -r '.NodeGroups[]?.NodeGroupMembers[]? | "      \(.CacheClusterId) [\(.CurrentRole)] in \(.PreferredAvailabilityZone)"'
    fi
else
    echo "  Cluster not found or already deleted."
fi

# -- 2. ECS Load Generator Tasks --
echo ""
echo "=== ECS Load Generator ==="
SVC_JSON=$(aws ecs describe-services \
    --cluster "$CLUSTER_NAME" \
    --services "$SERVICE_NAME" \
    --region "$REGION" \
    --query "services[0]" \
    --output json 2>/dev/null) || SVC_JSON=""

if [[ -n "$SVC_JSON" && "$SVC_JSON" != "null" ]]; then
    SVC_STATUS=$(echo "$SVC_JSON" | jq -r '.status')
    DESIRED=$(echo "$SVC_JSON" | jq -r '.desiredCount')
    RUNNING=$(echo "$SVC_JSON" | jq -r '.runningCount')
    PENDING=$(echo "$SVC_JSON" | jq -r '.pendingCount')

    echo "  Service Status: $SVC_STATUS"
    echo "  Desired: $DESIRED  Running: $RUNNING  Pending: $PENDING"

    TASK_ARNS=$(aws ecs list-tasks \
        --cluster "$CLUSTER_NAME" \
        --service-name "$SERVICE_NAME" \
        --region "$REGION" \
        --query "taskArns" \
        --output json 2>/dev/null)

    TASK_COUNT=$(echo "$TASK_ARNS" | jq 'length')

    if [[ "$TASK_COUNT" -gt 0 ]]; then
        TASKS=$(aws ecs describe-tasks \
            --cluster "$CLUSTER_NAME" \
            --tasks $(echo "$TASK_ARNS" | jq -r '.[]') \
            --region "$REGION" \
            --query "tasks[*].{TaskArn:taskArn,Status:lastStatus,StartedAt:startedAt,StoppedAt:stoppedAt,StopReason:stoppedReason}" \
            --output json 2>/dev/null)

        echo "  Tasks:"
        echo "$TASKS" | jq -r '.[] | 
            "    " + (.TaskArn | split("/") | last) + " : " + .Status + 
            (if .StartedAt then " (started: " + .StartedAt + ")" else "" end)' 

        if $DETAILED; then
            echo "$TASKS" | jq -r '.[] | select(.StopReason != null and .StopReason != "") | "      Stop Reason: " + .StopReason'
        fi
    else
        echo "  No active tasks."
    fi
else
    echo "  ECS service not found or already deleted."
fi

# -- 3. Reporter Task --
echo ""
echo "=== Reporter Task ==="
REPORTER_FAMILY="${CLUSTER_ID}-reporter"
REPORTER_ARNS=$(aws ecs list-tasks \
    --cluster "$CLUSTER_NAME" \
    --family "$REPORTER_FAMILY" \
    --region "$REGION" \
    --query "taskArns" \
    --output json 2>/dev/null) || REPORTER_ARNS="[]"

REPORTER_COUNT=$(echo "$REPORTER_ARNS" | jq 'length')

if [[ "$REPORTER_COUNT" -gt 0 ]]; then
    aws ecs describe-tasks \
        --cluster "$CLUSTER_NAME" \
        --tasks $(echo "$REPORTER_ARNS" | jq -r '.[]') \
        --region "$REGION" \
        --query "tasks[*].{TaskArn:taskArn,Status:lastStatus,StartedAt:startedAt,StoppedAt:stoppedAt}" \
        --output json 2>/dev/null | jq -r '.[] | "  " + (.TaskArn | split("/") | last) + " : " + .Status'
else
    echo "  No reporter tasks (report generation has not started yet)."
fi

# -- 4. Shutdown Schedule --
echo ""
echo "=== Shutdown Schedule ==="
SHUTDOWN_RULE="${CLUSTER_ID}-shutdown"
RULE_JSON=$(aws events describe-rule \
    --name "$SHUTDOWN_RULE" \
    --region "$REGION" \
    --output json 2>/dev/null) || RULE_JSON=""

if [[ -n "$RULE_JSON" ]]; then
    RULE_STATE=$(echo "$RULE_JSON" | jq -r '.State')
    SCHEDULE=$(echo "$RULE_JSON" | jq -r '.ScheduleExpression')

    echo "  Rule State:  $RULE_STATE"
    echo "  Schedule:    $SCHEDULE"

    # Parse cron and compute remaining time
    if [[ "$SCHEDULE" =~ cron\(([0-9]+)\ ([0-9]+)\ ([0-9]+)\ ([0-9]+)\ \?\ ([0-9]+)\) ]]; then
        MINUTE="${BASH_REMATCH[1]}"
        HOUR="${BASH_REMATCH[2]}"
        DAY="${BASH_REMATCH[3]}"
        MONTH="${BASH_REMATCH[4]}"
        YEAR="${BASH_REMATCH[5]}"

        if [[ "$YEAR" -eq 2099 ]]; then
            echo "  Status: Placeholder (shutdown not yet scheduled or already fired)"
        else
            SHUTDOWN_EPOCH=$(date -u -d "${YEAR}-${MONTH}-${DAY} ${HOUR}:${MINUTE}:00" +%s 2>/dev/null || echo 0)
            NOW_EPOCH=$(date -u +%s)
            REMAINING=$((SHUTDOWN_EPOCH - NOW_EPOCH))

            if [[ "$REMAINING" -gt 0 ]]; then
                HOURS=$((REMAINING / 3600))
                MINS=$(( (REMAINING % 3600) / 60 ))
                SECS=$((REMAINING % 60))
                printf "  Time Remaining: %02d:%02d:%02d\n" "$HOURS" "$MINS" "$SECS"
                echo "  Shutdown At:    ${YEAR}-${MONTH}-${DAY} ${HOUR}:${MINUTE}:00 UTC"
            else
                echo "  Status: Shutdown time has passed"
            fi
        fi
    fi
else
    echo "  Shutdown rule not found or already cleaned up."
fi

# -- 5. S3 Results --
echo ""
echo "=== S3 Results ==="
S3_LISTING=$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}" --recursive --region "$REGION" 2>/dev/null) || S3_LISTING=""

if [[ -n "$S3_LISTING" ]]; then
    TOTAL=$(echo "$S3_LISTING" | wc -l | tr -d ' ')
    HTML_COUNT=$(echo "$S3_LISTING" | grep -c '\.html$' || true)
    CSV_COUNT=$(echo "$S3_LISTING" | grep -c '\.csv$' || true)
    TXT_COUNT=$(echo "$S3_LISTING" | grep -c '\.txt$' || true)

    echo "  Total files:    $TOTAL"
    echo "  HTML reports:   $HTML_COUNT"
    echo "  CSV metrics:    $CSV_COUNT"
    echo "  Log files:      $TXT_COUNT"

    if [[ "$HTML_COUNT" -gt 0 ]]; then
        echo ""
        echo "  Reports found:"
        echo "$S3_LISTING" | grep '\.html$' | awk '{print "    s3://'"$S3_BUCKET"'/" $4}'
    fi

    if $DETAILED; then
        echo ""
        echo "  All files:"
        echo "$S3_LISTING" | sed 's/^/    /'
    fi
else
    echo "  No results exported yet."
fi

# -- Summary --
echo ""
echo "=== Summary ==="
PHASE="Unknown"

EC_ST="${STATUS:-}"
SVC_RUN="${RUNNING:-0}"
SVC_DES="${DESIRED:-0}"

if [[ "$EC_ST" == "available" && "$SVC_RUN" -gt 0 ]]; then
    PHASE="RUNNING - Load test in progress"
elif [[ "$EC_ST" == "available" && "$SVC_RUN" -eq 0 && "$SVC_DES" -eq 0 ]]; then
    PHASE="SHUTTING DOWN - Tasks stopped, waiting for cleanup"
elif [[ "$EC_ST" == "deleting" ]]; then
    PHASE="CLEANUP - ElastiCache cluster being deleted"
elif [[ "${HTML_COUNT:-0}" -gt 0 ]]; then
    PHASE="COMPLETE - Results available"
elif [[ -z "$EC_ST" ]]; then
    PHASE="COMPLETE or NOT DEPLOYED - Resources not found"
else
    PHASE="STARTING - Cluster or tasks provisioning"
fi

echo "  Phase: $PHASE"
echo ""
