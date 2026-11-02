# RAM Keeper maintainer notes

This replaces the former machine-specific service-status snapshot. Service names,
enabled state, and protection choices change over time; do not treat this file as
live status or copy its contents into a user's configuration.

## Configuration and protection

- User settings live at `~/.config/ram-keeper/config.json`.
- The daemon protects entries in `protected` and its built-in inviolable set.
- `max_victim_fraction` applies to each target cgroup's current memory for an
  actual reclaim request. The no-argument planning bound is system-wide only.
- Inspect the live system before recommending that a service be enabled,
  disabled, stopped, or protected.

## Memory telemetry

- `memory_reading_valid` requires a positive `MemTotal` and a present,
  in-range `MemAvailable`. Invalid readings produce `state=unknown`, null
  derived metrics, a cleared forecast, and no reclaim—even with a forced request.
- `memory_breakdown_valid` additionally requires a valid `Cached` value. Free
  percentage remains usable without it, but the Apps/Cache/Free split is null
  and the panel hides the chart instead of treating missing cache as zero.
- PSI carries an explicit `available` flag. An unreadable or malformed source is
  not a real zero-pressure reading.

## Process Guardian status

`helper/process_guardian.py` currently provides process-discovery and protection
helpers, but the production daemon does not call it. Do not describe dynamic
new-process protection as active until the runtime integration is implemented
and verified. The daemon's current protections come from its own configuration
and built-in inviolable rules.

## Test safety

The deterministic request-sizing checks run by default. The optional live stress
case allocates about 4.8 GB and waits three minutes; run it only when that
resource use is explicitly acceptable by setting `RAM_KEEPER_DANGEROUS_TESTS=1`.
