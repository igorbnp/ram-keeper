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
