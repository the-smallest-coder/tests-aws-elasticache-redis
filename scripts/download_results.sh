#!/usr/bin/env bash
#
# download_results.sh — Download test results from S3 to a local folder.
#
# Usage:
#   ./scripts/download_results.sh                        # download everything
#   ./scripts/download_results.sh --reports-only         # just HTML reports
#   ./scripts/download_results.sh --latest               # latest run only
#   ./scripts/download_results.sh --output-dir ./my-dir  # custom destination
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

OUTPUT_DIR=""
REPORTS_ONLY=false
LATEST=false
FORCE=false
PARALLEL=8

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir)
            if [[ $# -lt 2 || "$2" == --* ]]; then
                echo "ERROR: --output-dir requires a directory argument." >&2
                exit 1
            fi
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --reports-only) REPORTS_ONLY=true; shift ;;
        --latest)       LATEST=true; shift ;;
        --force)        FORCE=true; shift ;;
        --parallel)
            if [[ $# -lt 2 || "$2" == --* ]]; then
                echo "ERROR: --parallel requires a positive integer argument." >&2
                exit 1
            fi
            if ! [[ "$2" =~ ^[1-9][0-9]*$ ]]; then
                echo "ERROR: --parallel must be a positive integer, got '$2'." >&2
                exit 1
            fi
            PARALLEL="$2"
            shift 2
            ;;
        *)              echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# -- Resolve Terraform outputs --
echo ""
echo "=== Resolving Terraform outputs ==="

cd "$PROJECT_DIR"
TF_OUTPUT=$(terraform output -json 2>/dev/null) || {
    echo "ERROR: Failed to read Terraform outputs. Has 'terraform apply' been run?" >&2
    exit 1
}

REGION=$(echo "$TF_OUTPUT" | jq -r '.aws_region.value // "us-east-1"' 2>/dev/null || echo "us-east-1")
S3_LOCATION=$(echo "$TF_OUTPUT" | jq -r '.metrics_export_location.value')
CLUSTER_ID=$(echo "$TF_OUTPUT" | jq -r '.elasticache_cluster_id.value')
CLUSTER_NAME=$(echo "$TF_OUTPUT" | jq -r '.loadgen_cluster_name.value')
SERVICE_NAME=$(echo "$TF_OUTPUT" | jq -r '.loadgen_service_name.value')
LOG_GROUP=$(echo "$TF_OUTPUT" | jq -r '.loadgen_log_group_name.value')
CURRENT_RUN=$(_current_run_from_tf_output "$TF_OUTPUT") || {
    echo "ERROR: Terraform outputs do not include run_folder or a parseable run_timestamp." >&2
    exit 1
}
if ! echo "$TF_OUTPUT" | jq -e '.run_folder.value? // empty' >/dev/null; then
    echo "WARNING: Terraform output 'run_folder' is missing; falling back to legacy run_timestamp parsing." >&2
fi

S3_BUCKET=$(echo "$S3_LOCATION" | sed 's|^s3://||' | cut -d/ -f1)
S3_PREFIX=$(echo "$S3_LOCATION" | sed "s|^s3://${S3_BUCKET}/||")

[[ -z "$OUTPUT_DIR" ]] && OUTPUT_DIR="${PROJECT_DIR}/results"

echo "  S3 Source:  s3://${S3_BUCKET}/${S3_PREFIX}"
echo "  Cluster:    $CLUSTER_ID"
echo "  Current Run:$CURRENT_RUN"
echo "  Local Dir:  $OUTPUT_DIR"

# -- List available files --
echo ""
echo "=== Listing S3 objects ==="

S3_LISTING=$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}" --recursive --region "$REGION" 2>/dev/null) || S3_LISTING=""

if [[ -z "$S3_LISTING" ]]; then
    echo "  No results found in S3. The test may still be running."
    echo "  Run ./scripts/check_status.sh to see current progress."
    echo ""
    exit 0
fi

# Extract S3 keys (4th column from aws s3 ls output)
ALL_KEYS=$(echo "$S3_LISTING" | awk '{print $4}')

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

_print_current_run_status() {
    local status_json="${1:-}"
    local status_present=false
    local status_complete=false
    local status_has_outputs=false
    local fatal_error=""
    local phase

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
    phase=$(_classify_current_run "$EC_STATUS" "$DESIRED" "$RUNNING" "$PENDING" \
        "$REPORTER_COUNT" "$REPORTER_STATUS" "$status_present" "$status_complete" "$status_has_outputs" "$fatal_error")
    echo "  Current Terraform run $CURRENT_RUN is not ready: $phase."
}

# If --latest, select the newest ready run rather than the newest run with any object.
# Run folder timestamps can come from the test's own clock/timezone and are not
# always ordered the same way as upload completion time.
if $LATEST; then
    NEWEST_S3_RUN=$(printf '%s\n' "$S3_LISTING" | _run_timestamps_by_recency | head -n 1)
    LATEST_READY_RUN=""
    CURRENT_STATUS_JSON=""

    while IFS= read -r run; do
        [[ -z "$run" ]] && continue
        status_json=$(_read_status_json "$run" || true)
        if [[ "$run" == "$CURRENT_RUN" ]]; then
            CURRENT_STATUS_JSON="$status_json"
        fi
        if [[ -n "$status_json" ]] && _report_status_ready "$status_json" "$ALL_KEYS"; then
            LATEST_READY_RUN="$run"
            break
        fi
    done < <(printf '%s\n' "$S3_LISTING" | _run_timestamps_by_recency)

    if [[ "$NEWEST_S3_RUN" == "$CURRENT_RUN" && "$LATEST_READY_RUN" != "$CURRENT_RUN" ]]; then
        _print_current_run_status "$CURRENT_STATUS_JSON"
    fi

    if [[ -n "$LATEST_READY_RUN" ]]; then
        if [[ "$NEWEST_S3_RUN" == "$CURRENT_RUN" && "$LATEST_READY_RUN" != "$CURRENT_RUN" ]]; then
            echo "  Latest completed run is $LATEST_READY_RUN; downloading that result set instead."
        else
            echo "  Latest ready run by upload time: $LATEST_READY_RUN"
        fi
        ALL_KEYS=$(grep "$LATEST_READY_RUN" <<<"$ALL_KEYS")
    else
        echo "  No ready result sets found yet."
        if [[ "$NEWEST_S3_RUN" == "$CURRENT_RUN" ]]; then
            echo "  Run ./scripts/check_status.sh for full infrastructure details."
        fi
        exit 0
    fi
fi

# If --reports-only, filter to HTML files
if $REPORTS_ONLY; then
    ALL_KEYS=$(echo "$ALL_KEYS" | grep '\.html$' || true)
    if [[ -z "$ALL_KEYS" ]]; then
        echo "  No HTML reports found yet. The report generator may not have run."
        exit 0
    fi
fi

FILE_COUNT=$(echo "$ALL_KEYS" | wc -l | tr -d ' ')
echo "  Found $FILE_COUNT file(s) to download."

DOWNLOAD_TASKS=$(echo "$S3_LISTING" | awk '
    NR==FNR {a[$0]=1; next}
    NF>=4 {
        # get size
        size = $3
        # key is everything from the 4th field onwards, preserving internal spaces
        match($0, / [0-9]+ /)
        if (RSTART > 0) {
            key_start = RSTART + RLENGTH
            key = substr($0, key_start)
            if (key in a) {
                printf "%s|%s\n", size, key
            }
        }
    }' <(echo "$ALL_KEYS") -)

# -- Download files (parallel) --
echo ""
echo "=== Downloading ==="

mkdir -p "$OUTPUT_DIR"

RESULTS_DIR=$(mktemp -d)
mkdir -p "${RESULTS_DIR}/ok" "${RESULTS_DIR}/fail" "${RESULTS_DIR}/skip"
_download_one() {
    local task="$1"
    local s3_bucket="$2"
    local s3_prefix="$3"
    local output_dir="$4"
    local region="$5"
    local results_dir="$6"
    local force="$7"

    local expected_size="${task%%|*}"
    local key="${task#*|}"

    local relative_path="${key#$s3_prefix}"
    local local_path="${output_dir}/${relative_path}"
    local local_dir
    local_dir=$(dirname "$local_path")

    mkdir -p "$local_dir"

    # Use per-job marker files to avoid concurrent write races on shared files.
    local safe_name
    safe_name=$(echo "$relative_path" | tr '/' '_' | tr -d ' ')

    if [[ "$force" == "false" && -f "$local_path" ]]; then
        local local_size
        local_size=$(wc -c < "$local_path" | tr -d ' ')
        if [[ "$local_size" == "$expected_size" ]]; then
            touch "${results_dir}/skip/${safe_name}"
            echo "  SKIP  $relative_path"
            return 0
        fi
    fi

    if aws s3 cp "s3://${s3_bucket}/${key}" "$local_path" --region "$region" --quiet 2>/dev/null; then
        touch "${results_dir}/ok/${safe_name}"
        echo "  OK    $relative_path"
    else
        touch "${results_dir}/fail/${safe_name}"
        echo "  FAIL  $relative_path"
    fi
}
export -f _download_one

echo "$DOWNLOAD_TASKS" | grep -v '^$' | \
    xargs -P "$PARALLEL" -I{} bash -c \
        '_download_one "$@"' _ {} \
        "$S3_BUCKET" "$S3_PREFIX" "$OUTPUT_DIR" "$REGION" "$RESULTS_DIR" "$FORCE"

DOWNLOADED=$(find "${RESULTS_DIR}/ok" -maxdepth 1 -type f | wc -l | tr -d ' ')
SKIPPED=$(find "${RESULTS_DIR}/skip" -maxdepth 1 -type f | wc -l | tr -d ' ')
FAILED=$(find "${RESULTS_DIR}/fail" -maxdepth 1 -type f | wc -l | tr -d ' ')
rm -rf "$RESULTS_DIR"

# -- Summary --
echo ""
echo "=== Download Complete ==="
echo "  Downloaded: $DOWNLOADED"
[[ "$SKIPPED" -gt 0 ]] && echo "  Skipped:    $SKIPPED"
[[ "$FAILED" -gt 0 ]] && echo "  Failed:     $FAILED"
echo "  Location:   $(cd "$OUTPUT_DIR" && pwd)"

# Highlight HTML reports
HTML_FILES=$(find "$OUTPUT_DIR" -name "*.html" 2>/dev/null) || HTML_FILES=""
if [[ -n "$HTML_FILES" ]]; then
    echo ""
    echo "  HTML Reports:"
    echo "$HTML_FILES" | sed 's/^/    /'
    echo ""
    echo "  Open a report in your browser to view the dashboard."
else
    echo ""
    echo "  No HTML reports downloaded. The report generator may not have run yet."
    echo "  Run ./scripts/check_status.sh to see current progress."
fi
echo ""
