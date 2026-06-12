#!/usr/bin/env bash

_run_timestamps_by_recency() {
    awk '
        NF >= 4 {
            key = $4
            if (match(key, /[0-9]{8}-[0-9]{6}(-[a-z0-9]{1,8})?/)) {
                run = substr(key, RSTART, RLENGTH)
                modified = $1 " " $2
                if (!(run in latest_modified) || modified > latest_modified[run]) {
                    latest_modified[run] = modified
                }
            }
        }
        END {
            for (run in latest_modified) {
                print latest_modified[run] "\t" run
            }
        }
    ' | sort -r | cut -f2
}

_s3_uri_key() {
    printf '%s\n' "${1#s3://*/}"
}

_current_run_from_tf_output() {
    local tf_output="$1"
    local run_folder
    local run_timestamp

    run_folder=$(jq -r '.run_folder.value // empty' <<<"$tf_output")
    if [[ -n "$run_folder" && "$run_folder" != "null" ]]; then
        printf '%s\n' "$run_folder"
        return 0
    fi

    run_timestamp=$(jq -r '.run_timestamp.value // empty' <<<"$tf_output")
    if [[ "$run_timestamp" =~ ^[0-9]{14}$ ]]; then
        printf '%s-%s\n' "${run_timestamp:0:8}" "${run_timestamp:8:6}"
        return 0
    fi

    return 1
}

_keys_contain() {
    local keys="$1"
    local wanted="$2"
    grep -Fxq "$wanted" <<<"$keys"
}

_report_status_ready() {
    local status_json="$1"
    local keys="$2"
    local report_uri
    local summary_uri
    local report_key
    local summary_key

    jq -e '.complete == true and (.report | type == "string" and length > 0) and (.summary | type == "string" and length > 0)' \
        >/dev/null 2>&1 <<<"$status_json" || return 1

    report_uri=$(jq -r '.report' <<<"$status_json")
    summary_uri=$(jq -r '.summary' <<<"$status_json")
    report_key=$(_s3_uri_key "$report_uri")
    summary_key=$(_s3_uri_key "$summary_uri")

    _keys_contain "$keys" "$report_key" && _keys_contain "$keys" "$summary_key"
}

_classify_current_run() {
    local ec_status="$1"
    local desired="$2"
    local running="$3"
    local pending="$4"
    local reporter_count="$5"
    local reporter_status="$6"
    local status_present="$7"
    local status_complete="$8"
    local status_has_outputs="$9"
    local fatal_error="${10:-}"

    if [[ -n "$fatal_error" ]]; then
        printf 'known fatal reporter error: %s\n' "$fatal_error"
    elif [[ "$reporter_count" -gt 0 && "$reporter_status" != "STOPPED" ]]; then
        printf 'report running\n'
    elif [[ "$status_present" == "true" && "$status_complete" != "true" ]]; then
        printf 'export failed\n'
    elif [[ "$status_present" == "true" && "$status_complete" == "true" && "$status_has_outputs" != "true" ]]; then
        printf 'report failed\n'
    elif [[ "$reporter_count" -gt 0 && "$reporter_status" == "STOPPED" ]]; then
        printf 'report failed\n'
    elif [[ "$ec_status" == "deleting" || ( "$desired" -eq 0 && "$running" -eq 0 && "$ec_status" == "available" ) ]]; then
        printf 'stopping/cleanup\n'
    elif [[ "$running" -gt 0 ]]; then
        printf 'running\n'
    elif [[ "$desired" -gt 0 || "$pending" -gt 0 || -n "$ec_status" ]]; then
        printf 'starting\n'
    else
        printf 'report not started\n'
    fi
}

_read_status_json() {
    local run="$1"
    local status_key="${S3_PREFIX}${run}/report_status.json"

    if ! _keys_contain "$ALL_KEYS" "$status_key"; then
        return 1
    fi

    aws s3 cp "s3://${S3_BUCKET}/${status_key}" - --region "$REGION" 2>/dev/null
}

_current_reporter_state() {
    REPORTER_COUNT=0
    REPORTER_STATUS=""
    local reporter_arns
    local reporter_family="${CLUSTER_ID}-reporter"

    reporter_arns=$(aws ecs list-tasks \
        --cluster "$CLUSTER_NAME" \
        --family "$reporter_family" \
        --region "$REGION" \
        --query "taskArns" \
        --output json 2>/dev/null) || reporter_arns="[]"
    REPORTER_COUNT=$(jq 'length' <<<"$reporter_arns")

    if [[ "$REPORTER_COUNT" -gt 0 ]]; then
        REPORTER_STATUS=$(aws ecs describe-tasks \
            --cluster "$CLUSTER_NAME" \
            --tasks $(jq -r '.[]' <<<"$reporter_arns") \
            --region "$REGION" \
            --query 'tasks | sort_by(@, &createdAt)[-1].lastStatus' \
            --output text 2>/dev/null) || REPORTER_STATUS=""
    fi
}

_current_service_state() {
    local service_json
    EC_STATUS=$(aws elasticache describe-replication-groups \
        --replication-group-id "$CLUSTER_ID" \
        --region "$REGION" \
        --query "ReplicationGroups[0].Status" \
        --output text 2>/dev/null) || EC_STATUS=""

    service_json=$(aws ecs describe-services \
        --cluster "$CLUSTER_NAME" \
        --services "$SERVICE_NAME" \
        --region "$REGION" \
        --query "services[0]" \
        --output json 2>/dev/null) || service_json=""
    DESIRED=$(jq -r '.desiredCount // 0' <<<"${service_json:-null}")
    RUNNING=$(jq -r '.runningCount // 0' <<<"${service_json:-null}")
    PENDING=$(jq -r '.pendingCount // 0' <<<"${service_json:-null}")
}

_reporter_fatal_error() {
    local latest_stream
    latest_stream=$(aws logs describe-log-streams \
        --log-group-name "$LOG_GROUP" \
        --log-stream-name-prefix "reporter/reporter/" \
        --region "$REGION" \
        --query 'sort_by(logStreams, &lastEventTimestamp)[-1].logStreamName' \
        --output text 2>/dev/null) || latest_stream=""
    [[ -z "$latest_stream" || "$latest_stream" == "None" ]] && return 0

    aws logs get-log-events \
        --log-group-name "$LOG_GROUP" \
        --log-stream-name "$latest_stream" \
        --limit 100 \
        --region "$REGION" \
        --query 'events[].message' \
        --output text 2>/dev/null |
        grep -E 'RuntimeError:|Traceback|ERROR:' |
        tail -n 1 || true
}

_classify_current_run_from_state() {
    local status_json="${1:-}"
    local status_present=false
    local status_complete=false
    local status_has_outputs=false
    local fatal_error=""

    if [[ -n "$status_json" ]]; then
        status_present=true
        status_complete=$(jq -r '.complete == true' <<<"$status_json")
        if jq -e '(.report | type == "string" and length > 0) and (.summary | type == "string" and length > 0)' \
            >/dev/null 2>&1 <<<"$status_json"; then
            status_has_outputs=true
        fi
    fi

    _current_service_state
    _current_reporter_state
    if [[ "$REPORTER_COUNT" -gt 0 && "$REPORTER_STATUS" == "STOPPED" && "$status_has_outputs" != "true" ]]; then
        fatal_error=$(_reporter_fatal_error)
    fi

    _classify_current_run "$EC_STATUS" "$DESIRED" "$RUNNING" "$PENDING" \
        "$REPORTER_COUNT" "$REPORTER_STATUS" "$status_present" "$status_complete" "$status_has_outputs" "$fatal_error"
}

_canonical_status_token() {
    local phrase="$1"
    case "$phrase" in
        "report not started")           printf 'not-started\n' ;;
        "starting")                     printf 'starting\n' ;;
        "running")                      printf 'running\n' ;;
        "stopping/cleanup")             printf 'stopping/cleanup\n' ;;
        "report running")               printf 'reporting\n' ;;
        "export failed")                printf 'failed\n' ;;
        "report failed")                printf 'failed\n' ;;
        "known fatal reporter error:"*) printf 'failed\n' ;;
        "complete")                     printf 'complete\n' ;;
        "destroyed/not-found")          printf 'destroyed/not-found\n' ;;
        "timeout")                      printf 'timeout\n' ;;
        *)                              printf 'failed\n' ;;
    esac
}

_status_exit_code() {
    local token="$1"
    case "$token" in
        complete)            printf '0\n' ;;
        running)             printf '10\n' ;;
        starting)            printf '11\n' ;;
        not-started)         printf '12\n' ;;
        stopping/cleanup)    printf '13\n' ;;
        reporting)           printf '14\n' ;;
        failed)              printf '20\n' ;;
        timeout)             printf '21\n' ;;
        destroyed/not-found) printf '22\n' ;;
        *)                   printf '20\n' ;;
    esac
}
