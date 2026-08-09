#!/usr/bin/env bash
#
# fetch_elasticache_price.sh — Live AWS Price List lookup for one ElastiCache
# node's on-demand hourly rate, in the run's own region and engine.
#
# Invoked by Terraform's `data "external"` block in ecs.tf. Terraform's
# external-program protocol: read a JSON object from stdin (query args),
# print exactly one flat JSON object of string->string to stdout, and
# ALWAYS exit 0 — a non-zero exit fails the entire `terraform apply`, which
# a cost-reporting side value must never do.
#
# On any failure (missing aws/jq, no pricing:GetProducts permission, no
# matching SKU, region not in the location map below) this prints
# {"hourly_usd": "", "source": "unavailable", "reason": "..."} and exits 0.
# Callers must treat an empty hourly_usd as "not available", never as 0.
#
# Requires the `aws` CLI and `jq` on the machine running `terraform apply`
# (both are already required elsewhere in this repo, see check_status.sh).
#
# The AWS Price List Query API is only served from us-east-1 (and
# ap-south-1) regardless of which region the cluster itself runs in --
# that --region below is an AWS API constraint, not a bug.

set -uo pipefail

_unavailable() {
    local reason="$1"
    jq -n --arg reason "$reason" '{hourly_usd: "", source: "unavailable", reason: $reason}'
    exit 0
}

for cmd in aws jq; do
    command -v "$cmd" &>/dev/null || _unavailable "missing dependency: $cmd"
done

QUERY_JSON="$(cat)"
NODE_TYPE="$(jq -r '.node_type' <<<"$QUERY_JSON")"
ENGINE_TYPE="$(jq -r '.engine_type' <<<"$QUERY_JSON")"
AWS_REGION_ARG="$(jq -r '.aws_region' <<<"$QUERY_JSON")"

case "$ENGINE_TYPE" in
    redis) CACHE_ENGINE="Redis" ;;
    valkey) CACHE_ENGINE="Valkey" ;;
    *) _unavailable "unknown engine_type: $ENGINE_TYPE" ;;
esac

# AWS Price List "location" attribute string, keyed by region code. This is
# static AWS naming data (not pricing), stable across years -- not a
# staleness risk like a hardcoded price would be. Extend as needed; verify
# an unfamiliar region with:
#   aws pricing get-attribute-values --service-code AmazonElastiCache --attribute-name location --region us-east-1
declare -A REGION_LOCATIONS=(
    [us-east-1]="US East (N. Virginia)"
    [us-east-2]="US East (Ohio)"
    [us-west-1]="US West (N. California)"
    [us-west-2]="US West (Oregon)"
    [af-south-1]="Africa (Cape Town)"
    [ap-east-1]="Asia Pacific (Hong Kong)"
    [ap-south-1]="Asia Pacific (Mumbai)"
    [ap-south-2]="Asia Pacific (Hyderabad)"
    [ap-northeast-1]="Asia Pacific (Tokyo)"
    [ap-northeast-2]="Asia Pacific (Seoul)"
    [ap-northeast-3]="Asia Pacific (Osaka)"
    [ap-southeast-1]="Asia Pacific (Singapore)"
    [ap-southeast-2]="Asia Pacific (Sydney)"
    [ap-southeast-3]="Asia Pacific (Jakarta)"
    [ap-southeast-4]="Asia Pacific (Melbourne)"
    [ca-central-1]="Canada (Central)"
    [ca-west-1]="Canada West (Calgary)"
    [eu-central-1]="EU (Frankfurt)"
    [eu-central-2]="Europe (Zurich)"
    [eu-west-1]="EU (Ireland)"
    [eu-west-2]="EU (London)"
    [eu-west-3]="EU (Paris)"
    [eu-north-1]="EU (Stockholm)"
    [eu-south-1]="EU (Milan)"
    [eu-south-2]="Europe (Spain)"
    [me-south-1]="Middle East (Bahrain)"
    [me-central-1]="Middle East (UAE)"
    [sa-east-1]="South America (Sao Paulo)"
    [il-central-1]="Israel (Tel Aviv)"
)

LOCATION="${REGION_LOCATIONS[$AWS_REGION_ARG]:-}"
[[ -n "$LOCATION" ]] || _unavailable "no known Price List location for region: $AWS_REGION_ARG"

ERR_FILE="$(mktemp)"
trap 'rm -f "$ERR_FILE"' EXIT

RESULT_JSON="$(aws pricing get-products \
    --service-code AmazonElastiCache \
    --region us-east-1 \
    --format-version aws_v1 \
    --filters \
        "Type=TERM_MATCH,Field=instanceType,Value=${NODE_TYPE}" \
        "Type=TERM_MATCH,Field=location,Value=${LOCATION}" \
        "Type=TERM_MATCH,Field=cacheEngine,Value=${CACHE_ENGINE}" \
    --output json 2>"$ERR_FILE")"
AWS_EXIT=$?

if [[ $AWS_EXIT -ne 0 ]]; then
    REASON="aws pricing get-products failed: $(tr '\n' ' ' <"$ERR_FILE" | cut -c1-200)"
    _unavailable "$REASON"
fi

# Each PriceList entry is itself a JSON *string* that must be re-decoded.
# `.. | objects | .USD?` walks the decoded product recursively rather than
# hardcoding the SKU/rateCode nesting, which AWS does not guarantee is
# stable; on-demand ElastiCache pricing has exactly one USD leaf per SKU.
PRICE="$(jq -r '
    [.PriceList[]? | fromjson | .. | objects | .USD? // empty] | first // empty
' <<<"$RESULT_JSON" 2>/dev/null)"

[[ -n "$PRICE" && "$PRICE" != "null" ]] || \
    _unavailable "no AWS Price List match for ${NODE_TYPE}/${CACHE_ENGINE}/${LOCATION}"

jq -n \
    --arg hourly_usd "$PRICE" \
    --arg location "$LOCATION" \
    --arg engine "$CACHE_ENGINE" \
    '{hourly_usd: $hourly_usd, source: "aws_pricing_api", location: $location, engine: $engine}'
