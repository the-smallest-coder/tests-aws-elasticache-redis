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
# Requires: AWS CLI configured, Terraform state accessible from project root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

OUTPUT_DIR=""
REPORTS_ONLY=false
LATEST=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
        --reports-only) REPORTS_ONLY=true; shift ;;
        --latest)      LATEST=true; shift ;;
        *)             echo "Unknown option: $1" >&2; exit 1 ;;
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

S3_BUCKET=$(echo "$S3_LOCATION" | sed 's|^s3://||' | cut -d/ -f1)
S3_PREFIX=$(echo "$S3_LOCATION" | sed "s|^s3://${S3_BUCKET}/||")

[[ -z "$OUTPUT_DIR" ]] && OUTPUT_DIR="${PROJECT_DIR}/results"

echo "  S3 Source:  s3://${S3_BUCKET}/${S3_PREFIX}"
echo "  Cluster:    $CLUSTER_ID"
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

# If --latest, filter to most recent timestamped run
# NOTE: grep -oP requires GNU grep with PCRE support (standard on Linux/GNU environments).
#       macOS/BSD grep does not support -P and is not a supported platform for this script.
if $LATEST; then
    LATEST_TS=$(echo "$ALL_KEYS" | grep -oP '\d{8}-?\d{6}' | sort -u | tail -1)
    if [[ -n "$LATEST_TS" ]]; then
        echo "  Latest run: $LATEST_TS"
        ALL_KEYS=$(echo "$ALL_KEYS" | grep "$LATEST_TS")
    else
        echo "  Could not identify timestamped runs. Downloading all files."
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

# -- Download files --
echo ""
echo "=== Downloading ==="

mkdir -p "$OUTPUT_DIR"

DOWNLOADED=0
FAILED=0

while IFS= read -r key; do
    [[ -z "$key" ]] && continue

    # Preserve directory structure relative to S3 prefix
    RELATIVE_PATH="${key#$S3_PREFIX}"
    LOCAL_PATH="${OUTPUT_DIR}/${RELATIVE_PATH}"
    LOCAL_DIR=$(dirname "$LOCAL_PATH")

    mkdir -p "$LOCAL_DIR"

    if aws s3 cp "s3://${S3_BUCKET}/${key}" "$LOCAL_PATH" --region "$REGION" --quiet 2>/dev/null; then
        DOWNLOADED=$((DOWNLOADED + 1))
        echo "  OK    $RELATIVE_PATH"
    else
        FAILED=$((FAILED + 1))
        echo "  FAIL  $RELATIVE_PATH"
    fi
done <<< "$ALL_KEYS"

# -- Summary --
echo ""
echo "=== Download Complete ==="
echo "  Downloaded: $DOWNLOADED"
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
