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

OUTPUT_DIR=""
REPORTS_ONLY=false
LATEST=false
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

# If --latest, filter to the timestamped run with the newest S3 LastModified time.
# Run folder timestamps can come from the test's own clock/timezone and are not
# always ordered the same way as upload completion time.
if $LATEST; then
    LATEST_TS=$(echo "$S3_LISTING" | awk '
        NF >= 4 {
            key = $4
            if (match(key, /[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]/)) {
                run = substr(key, RSTART, RLENGTH)
                modified = $1 " " $2
                if (!(run in latest_modified) || modified > latest_modified[run]) {
                    latest_modified[run] = modified
                }
            }
        }
        END {
            for (run in latest_modified) {
                if (best_run == "" ||
                    latest_modified[run] > best_modified ||
                    (latest_modified[run] == best_modified && run > best_run)) {
                    best_run = run
                    best_modified = latest_modified[run]
                }
            }
            if (best_run != "") {
                print best_run
            }
        }
    ')
    if [[ -n "$LATEST_TS" ]]; then
        echo "  Latest run by upload time: $LATEST_TS"
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

# -- Download files (parallel) --
echo ""
echo "=== Downloading ==="

mkdir -p "$OUTPUT_DIR"

RESULTS_DIR=$(mktemp -d)
mkdir -p "${RESULTS_DIR}/ok" "${RESULTS_DIR}/fail"
_download_one() {
    local key="$1"
    local s3_bucket="$2"
    local s3_prefix="$3"
    local output_dir="$4"
    local region="$5"
    local results_dir="$6"

    local relative_path="${key#$s3_prefix}"
    local local_path="${output_dir}/${relative_path}"
    local local_dir
    local_dir=$(dirname "$local_path")

    mkdir -p "$local_dir"

    # Use per-job marker files to avoid concurrent write races on shared files.
    local safe_name
    safe_name=$(echo "$relative_path" | tr '/' '_' | tr -d ' ')

    if aws s3 cp "s3://${s3_bucket}/${key}" "$local_path" --region "$region" --quiet 2>/dev/null; then
        touch "${results_dir}/ok/${safe_name}"
        echo "  OK    $relative_path"
    else
        touch "${results_dir}/fail/${safe_name}"
        echo "  FAIL  $relative_path"
    fi
}
export -f _download_one

echo "$ALL_KEYS" | grep -v '^$' | \
    xargs -P "$PARALLEL" -I{} bash -c \
        '_download_one "$@"' _ {} \
        "$S3_BUCKET" "$S3_PREFIX" "$OUTPUT_DIR" "$REGION" "$RESULTS_DIR"

DOWNLOADED=$(find "${RESULTS_DIR}/ok" -maxdepth 1 -type f | wc -l | tr -d ' ')
FAILED=$(find "${RESULTS_DIR}/fail" -maxdepth 1 -type f | wc -l | tr -d ' ')
rm -rf "$RESULTS_DIR"

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
