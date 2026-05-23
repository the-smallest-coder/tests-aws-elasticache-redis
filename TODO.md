# TODO

## Eviction pressure test
At t+30min of benchmark, launch a second fill task (pure writes, sequential keys, `--ratio 0:1 --key-pattern S:S`) to overflow memory and sustain eviction pressure for the remaining test window.
Implement as a `tfvars` flag so it applies consistently across all future node type / Valkey comparisons.

## Planned test matrix
- Different instance types (same load, same config)
- Valkey vs Redis (same node type, same load)
- Multi-node / cluster mode

## Results download sync
Change `scripts/download_results.sh` from unconditional copy to sync-style behavior:
skip local files that already exist with matching S3 size/metadata, add a `--force`
option for full re-downloads, and keep `--latest` scoped to the latest run while
still downloading only missing or changed artifacts.
