#!/usr/bin/env bash

_run_timestamps_by_recency() {
    awk '
        NF >= 4 {
            key = $4
            if (match(key, /[0-9]{8}-[0-9]{6}/)) {
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
