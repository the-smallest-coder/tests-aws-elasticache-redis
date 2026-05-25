#!/usr/bin/env bash
#
# check_status.sh — Show current state of the ElastiCache performance test.
#
# Usage:
#   ./scripts/check_status.sh            # quick status
#   ./scripts/check_status.sh --detailed # verbose output
#
# Requires: AWS CLI configured, jq, terraform, Terraform state accessible from project root.

set -euo pipefail

# -- Dependency checks --
for cmd in aws jq terraform; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: Required dependency '$cmd' not found. Please install it and ensure it is on your PATH." >&2
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=download_results_lib.sh
source "$SCRIPT_DIR/download_results_lib.sh"
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
CURRENT_RUN=$(_current_run_from_tf_output "$TF_OUTPUT") || CURRENT_RUN=""

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
echo "  Run Folder:       ${CURRENT_RUN:-unknown}"

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
REPORTER_STATUS=""
REPORTER_ARNS_RUNNING=$(aws ecs list-tasks \
    --cluster "$CLUSTER_NAME" \
    --family "$REPORTER_FAMILY" \
    --desired-status RUNNING \
    --region "$REGION" \
    --query "taskArns" \
    --output json 2>/dev/null) || REPORTER_ARNS_RUNNING="[]"
REPORTER_ARNS_STOPPED=$(aws ecs list-tasks \
    --cluster "$CLUSTER_NAME" \
    --family "$REPORTER_FAMILY" \
    --desired-status STOPPED \
    --region "$REGION" \
    --query "taskArns" \
    --output json 2>/dev/null) || REPORTER_ARNS_STOPPED="[]"

REPORTER_ARNS=$(jq -n \
    --argjson running "${REPORTER_ARNS_RUNNING:-[]}" \
    --argjson stopped "${REPORTER_ARNS_STOPPED:-[]}" \
    '$running + $stopped | unique')

REPORTER_COUNT=$(echo "$REPORTER_ARNS" | jq 'length')

if [[ "$REPORTER_COUNT" -gt 0 ]]; then
    REPORTER_TASKS=$(aws ecs describe-tasks \
        --cluster "$CLUSTER_NAME" \
        --tasks $(echo "$REPORTER_ARNS" | jq -r '.[]') \
        --region "$REGION" \
        --query "tasks[*].{TaskArn:taskArn,Status:lastStatus,CreatedAt:createdAt,StartedAt:startedAt,StoppedAt:stoppedAt,StopReason:stoppedReason}" \
        --output json 2>/dev/null) || REPORTER_TASKS="[]"
    REPORTER_STATUS=$(echo "$REPORTER_TASKS" | jq -r 'sort_by(.CreatedAt // "") | .[-1].Status // ""')
    echo "$REPORTER_TASKS" | jq -r 'sort_by(.CreatedAt // "") | .[] |
        "  " + (.TaskArn | split("/") | last) + " : " + .Status +
        (if .StartedAt then " (started: " + .StartedAt + ")" else "" end) +
        (if .StoppedAt then " (stopped: " + .StoppedAt + ")" else "" end)'

    if $DETAILED; then
        echo "$REPORTER_TASKS" | jq -r '.[] | select(.StopReason != null and .StopReason != "") | "      Stop Reason: " + .StopReason'
    fi
else
    echo "  No reporter tasks found (stopped tasks may have aged out of ECS history)."
fi

# -- 4. Shutdown Schedule --
echo ""
echo "=== Shutdown Schedule ==="
SHUTDOWN_PASSED=false
VERIFY_PENDING=false
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
                SHUTDOWN_PASSED=true
                echo "  Status: Shutdown time has passed"
            fi
        fi
    fi
else
    echo "  Shutdown rule not found or already cleaned up."
fi

VERIFY_RULE="${CLUSTER_ID}-shutdown-verify"
VERIFY_JSON=$(aws events describe-rule \
    --name "$VERIFY_RULE" \
    --region "$REGION" \
    --output json 2>/dev/null) || VERIFY_JSON=""

if [[ -n "$VERIFY_JSON" ]]; then
    VERIFY_SCHEDULE=$(echo "$VERIFY_JSON" | jq -r '.ScheduleExpression')

    if [[ "$VERIFY_SCHEDULE" =~ cron\(([0-9]+)\ ([0-9]+)\ ([0-9]+)\ ([0-9]+)\ \?\ ([0-9]+)\) ]]; then
        VERIFY_EPOCH=$(date -u -d "${BASH_REMATCH[5]}-${BASH_REMATCH[4]}-${BASH_REMATCH[3]} ${BASH_REMATCH[2]}:${BASH_REMATCH[1]}:00" +%s 2>/dev/null || echo 0)
        if [[ "$VERIFY_EPOCH" -gt "$(date -u +%s)" ]]; then
            VERIFY_PENDING=true
        fi
    fi
fi

# -- 5. S3 Results --
echo ""
echo "=== S3 Results ==="
CURRENT_REPORT_READY=false
CURRENT_STATUS_PRESENT=false
CURRENT_STATUS_COMPLETE=false
CURRENT_HTML_COUNT=0
S3_LISTING=$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}" --recursive --region "$REGION" 2>/dev/null) || S3_LISTING=""

if [[ -n "$S3_LISTING" ]]; then
    ALL_KEYS=$(echo "$S3_LISTING" | awk 'NF >= 4 {print $4}')
    TOTAL=$(echo "$S3_LISTING" | wc -l | tr -d ' ')
    HTML_COUNT=$(echo "$S3_LISTING" | grep -c '\.html$' || true)
    CSV_COUNT=$(echo "$S3_LISTING" | grep -c '\.csv$' || true)
    TXT_COUNT=$(echo "$S3_LISTING" | grep -c '\.txt$' || true)

    echo "  All runs:"
    echo "    Total files:    $TOTAL"
    echo "    HTML reports:   $HTML_COUNT"
    echo "    CSV metrics:    $CSV_COUNT"
    echo "    Log files:      $TXT_COUNT"

    if [[ -n "$CURRENT_RUN" ]]; then
        CURRENT_PREFIX="${S3_PREFIX}${CURRENT_RUN}/"
        CURRENT_KEYS=$(awk -v prefix="$CURRENT_PREFIX" 'index($0, prefix) == 1 {print}' <<<"$ALL_KEYS")
        CURRENT_TOTAL=$(grep -c . <<<"$CURRENT_KEYS" || true)
        CURRENT_HTML_COUNT=$(grep -c '\.html$' <<<"$CURRENT_KEYS" || true)
        CURRENT_CSV_COUNT=$(grep -c '\.csv$' <<<"$CURRENT_KEYS" || true)
        CURRENT_TXT_COUNT=$(grep -c '\.txt$' <<<"$CURRENT_KEYS" || true)
        CURRENT_STATUS_KEY="${CURRENT_PREFIX}report_status.json"
        CURRENT_STATUS_JSON=""

        echo ""
        echo "  Current run ($CURRENT_RUN):"
        if [[ "$CURRENT_TOTAL" -gt 0 ]]; then
            echo "    Total files:    $CURRENT_TOTAL"
            echo "    HTML reports:   $CURRENT_HTML_COUNT"
            echo "    CSV metrics:    $CURRENT_CSV_COUNT"
            echo "    Log files:      $CURRENT_TXT_COUNT"
        else
            echo "    No objects found under s3://${S3_BUCKET}/${CURRENT_PREFIX}"
        fi

        if _keys_contain "$ALL_KEYS" "$CURRENT_STATUS_KEY"; then
            CURRENT_STATUS_PRESENT=true
            CURRENT_STATUS_JSON=$(aws s3 cp "s3://${S3_BUCKET}/${CURRENT_STATUS_KEY}" - --region "$REGION" 2>/dev/null || true)
            if [[ -n "$CURRENT_STATUS_JSON" ]]; then
                CURRENT_STATUS_COMPLETE=$(jq -r '.complete == true' <<<"$CURRENT_STATUS_JSON" 2>/dev/null || echo false)
                if _report_status_ready "$CURRENT_STATUS_JSON" "$ALL_KEYS"; then
                    CURRENT_REPORT_READY=true
                fi
            fi
            echo "    report_status.json: complete=$CURRENT_STATUS_COMPLETE"
        else
            echo "    report_status.json: missing"
        fi

        if $CURRENT_REPORT_READY; then
            CURRENT_REPORT_URI=$(jq -r '.report' <<<"$CURRENT_STATUS_JSON")
            CURRENT_SUMMARY_URI=$(jq -r '.summary' <<<"$CURRENT_STATUS_JSON")
            echo "    Report:"
            echo "      $CURRENT_REPORT_URI"
            echo "    Summary:"
            echo "      $CURRENT_SUMMARY_URI"
        elif [[ "$CURRENT_HTML_COUNT" -gt 0 ]]; then
            echo "    HTML files in current run:"
            echo "$CURRENT_KEYS" | grep '\.html$' | awk '{print "      s3://'"$S3_BUCKET"'/" $0}'
        fi
    fi

    if [[ "$HTML_COUNT" -gt 0 ]]; then
        echo ""
        if $DETAILED; then
            echo "  HTML reports across all runs:"
            echo "$S3_LISTING" | grep '\.html$' | awk '{print "    s3://'"$S3_BUCKET"'/" $4}'
        else
            echo "  Historical HTML reports: $HTML_COUNT across all runs (use --detailed to list)."
        fi
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
SVC_PENDING="${PENDING:-0}"

if $CURRENT_REPORT_READY; then
    PHASE="COMPLETE - Current run report available"
elif [[ "$EC_ST" == "available" && "$SVC_RUN" -gt 0 ]]; then
    PHASE="RUNNING - Load test in progress"
elif [[ "$EC_ST" == "available" && "$SVC_RUN" -eq 0 && "$SVC_DES" -eq 0 ]]; then
    PHASE="SHUTTING DOWN - Tasks stopped, waiting for cleanup"
elif [[ "$EC_ST" == "deleting" ]]; then
    PHASE="CLEANUP - ElastiCache cluster being deleted"
elif [[ "${REPORTER_COUNT:-0}" -gt 0 && "$REPORTER_STATUS" != "STOPPED" ]]; then
    PHASE="REPORTING - Current run report generator is running"
elif [[ "$CURRENT_STATUS_PRESENT" == "true" && "$CURRENT_STATUS_COMPLETE" != "true" ]]; then
    PHASE="FAILED - Current run export/report status is incomplete"
elif [[ "$CURRENT_STATUS_COMPLETE" == "true" && "$CURRENT_REPORT_READY" != "true" ]]; then
    PHASE="FAILED - Current run report status references missing outputs"
elif [[ "${REPORTER_COUNT:-0}" -gt 0 && "$REPORTER_STATUS" == "STOPPED" ]]; then
    PHASE="FAILED - Reporter stopped before current report became ready"
elif [[ "$VERIFY_PENDING" == "true" ]]; then
    PHASE="VERIFYING - Waiting for shutdown verification/report handoff"
elif [[ -z "$EC_ST" && "$SVC_RUN" -eq 0 && "$SVC_DES" -eq 0 && "$SVC_PENDING" -eq 0 && "$SHUTDOWN_PASSED" == "true" ]]; then
    PHASE="INCOMPLETE - Current run report not found"
elif [[ -z "$EC_ST" ]]; then
    PHASE="COMPLETE or NOT DEPLOYED - Resources not found"
else
    PHASE="STARTING - Cluster or tasks provisioning"
fi

echo "  Phase: $PHASE"
echo ""
