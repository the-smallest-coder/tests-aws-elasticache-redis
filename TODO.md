# TODO

## Eviction pressure test
At t+30min of benchmark, launch a second fill task (pure writes, sequential keys, `--ratio 0:1 --key-pattern S:S`) to overflow memory and sustain eviction pressure for the remaining test window.
Implement as a `tfvars` flag so it applies consistently across all future node type / Valkey comparisons.

## Planned test matrix
- Different instance types (same load, same config)
- Valkey vs Redis (same node type, same load)
- Multi-node / cluster mode
