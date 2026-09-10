#!/usr/bin/env bash
#
# git_sha.sh — provenance of the Terraform actually applied: the git commit
# this `terraform apply` ran from, and whether the working tree was dirty.
#
# Invoked by Terraform's `data "external" "git_sha"` block (main.tf). Same
# external-program protocol as fetch_elasticache_price.sh: print exactly one
# flat JSON object of string->string to stdout, and ALWAYS exit 0 -- a
# non-zero exit fails the entire `terraform apply`/`terraform destroy`, which
# provenance metadata must never do. On any failure (git missing, this
# directory not a git repo) this prints {"sha":"unknown","dirty":"unknown"}.
#
# `data` blocks are re-evaluated on every plan, including `terraform destroy`
# -- same reasoning as enable_price_lookup in variables.tf -- but this script
# has no external dependency to fail launching (git is assumed present on
# any machine that checked this repo out to run `terraform apply` from).

set -uo pipefail

cat >/dev/null # consume Terraform's query JSON; this script takes no input

_unknown() {
    printf '{"sha":"unknown","dirty":"unknown"}\n'
    exit 0
}

command -v git &>/dev/null || _unknown

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
[[ -n "$REPO_ROOT" ]] || _unknown
cd "$REPO_ROOT" 2>/dev/null || _unknown

git rev-parse --is-inside-work-tree &>/dev/null || _unknown

SHA="$(git rev-parse HEAD 2>/dev/null)"
[[ -n "$SHA" ]] || _unknown

if git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
    DIRTY="false"
else
    DIRTY="true"
fi

printf '{"sha":"%s","dirty":"%s"}\n' "$SHA" "$DIRTY"
