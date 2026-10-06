# ram-keeper — configuration and tuning

You shouldn't need to touch any of this. It's documented so you can see exactly
what the daemon decided, and change it if your machine is unusual.

Edit `~/.config/ram-keeper/config.json`. Only the keys you list are overridden;
the rest keep their defaults. Delete the file to return to defaults.

```json
{
  "target_free_percent": 18.0
}
```

## Thresholds

| Key | Default | What it does |
|---|---|---|
| `target_free_percent` | `18.0` | Free-memory floor. Below this, reclaim starts. This is the number that matters most: set it higher on a machine that must stay responsive, lower to let apps keep more resident. |
| `critical_free_percent` | `10.0` | Below this, reclaim is urgent. Must be lower than `target_free_percent`. |
| `psi_some_percent` | `6.0` | Memory stall (`/proc/pressure/memory`, `some` avg over 60 s) that counts as pressure even when free RAM looks healthy. This catches the case where free memory exists but everything is stalling anyway. Raise it to be more patient; lower it to react sooner. |
| `min_target_bytes` | `100663296` (96 MB) | Cgroups smaller than this are left alone. Reclaiming from a small cgroup costs more than it frees. |
| `max_victim_fraction` | `25` | Largest share of **one** cgroup's memory a single pass may reclaim. **Do not raise this casually** — see below. |
| `max_reclaim_per_pass` | `2147483648` (2 GB) | Ceiling on one pass. Guarantees a spike can't turn into a system-wide swap storm. |

## Cadence

| Key | Default | What it does |
|---|---|---|
| `interval_idle` | `20.0` | Seconds between passes when healthy. |
| `interval_pressure` | `5.0` | Seconds between passes under pressure. |

Reading cgroups is cheap (a few dozen small files), so `interval_idle` can go
lower. There's no reason to raise it — the daemon does nothing when healthy.

## Policy

| Key | Default | What it does |
|---|---|---|
| `cache_first` | `true` | Drop page cache before touching anonymous memory. Set to `false` only if you're convinced the cache churn is hurting more than it helps — normally it never does. |

## Why `max_victim_fraction` exists

It is not a tuning knob; it is a safety limit, and it exists because of a
measured failure during sustained-load testing.

The anon pass originally asked a cgroup for as much as its entire anonymous
memory. Under sustained load that produced a **1.5 GB request against a Chromium
process holding ~300 MB**. The kernel honoured as much as it could, which
emptied the process: it ended up with almost no resident pages, faulted hard
while touching them, and `systemd-oomd` killed it.

Reclaim had become the thing that killed an app — the exact opposite of its
purpose — and the symptom was invisible: every individual reclaim reported
`outcome: ok`, and the daemon logged healthy passes right up to the kill.

At 25% the same pressure takes four or five short passes spread over a few
seconds. The user cannot tell the difference, and no process is ever emptied
enough to fault fatally. If you raise this above ~35% you are re-enabling the
failure mode; lower values are safe but converge on the situation more slowly.

The per-pass total is separately bounded to 25% of what is actually reclaimable
at that moment, so many victims in one pass cannot add up to the same drain.

## Protected cgroups

`protected` — **never reclaimed**, and listed in the panel so you can see what
it's choosing not to touch. These are the compositor, the shell, and the
services that keep your session alive:

```
  wayland-wm@hyprland.desktop   the compositor; a dead WM is a dead desktop
  quickshell                    the shell that draws the panel
  pipewire.service              audio
  syncthing.service             file sync
  ollama.service                local model server
  ram-keeper                    the daemon itself
  ```

`never_reclaim` — also left alone, for a different reason: large background
indexers that rebuild their caches slowly and cost nothing to leave running.

```
localsearch-3.service
```

To protect something else, add its cgroup name to `never_reclaim`:

```bash
python3 ~/.config/omarchy/plugins/ram-keeper/helper/ram_keeper.py \
  --protected-add your-app.scope
```

To find a cgroup name:

```bash
for d in /sys/fs/cgroup/user.slice/user-$(id -u).slice/**/*/; do
  n=$(basename "$d")
  m=$(cat "$d/memory.current" 2>/dev/null)
  [ -n "$m" ] && [ "$m" -gt 100000000 ] && echo "$((m/1048576))MB $n"
done
```

---

## Why these values, given what Omarchy already ships

Omarchy 4.0.4 already does several things right, and this app does not undo
any of them:

- **`vm.swappiness = 150`** — correct for a zram machine. Swapping *to* RAM-backed
  zram is cheaper than evicting hot page cache, so the kernel should be eager
  about it. The common "set swappiness to 10" advice is for disk-swap machines
  and actively hurts here. See Chris Down, *In Defence of Swap*.
- **`zram-size = ram`, `swap-priority = 100`** — zram is the same size as RAM
  (thin-provisioned; the real cost is only the compressed pages actually stored)
  and outranks the disk swapfile.
- **`systemd-oomd` with `ManagedOOMMemoryPressure` on `app.slice` only** — the
  compositor is structurally ineligible for killing, which is exactly right.
- **`zswap` disabled** — redundant next to zram.

What Omarchy does *not* do is reclaim **before** pressure arrives. oomd waits for
sustained PSI pressure; by then the machine is already sluggish. This app acts
on the much earlier "free memory is dropping toward the floor" signal, so the
stall never starts in the first place.

## Tuning for a different machine

**More RAM (32 GB+)** — raise `target_free_percent` isn't needed; instead lower
it to `12` and raise `min_target_bytes` to `268435456` (256 MB). Bigger machines
should let apps keep much more resident.

**Less RAM (4 GB)** — raise `target_free_percent` to `25`, drop
`interval_pressure` to `3`. Reclaim earlier and more eagerly.

**A local LLM server you want resident** (llama-server, ollama): add its unit
to `never_reclaim`. A model that has been swapped out has to be read back from
zram before it can answer, and that is the one workload where paying RAM is
worth it:

```bash
python3 ~/.config/omarchy/plugins/ram-keeper/helper/ram_keeper.py \
  --protected-add llama-server.service
```

## Troubleshooting

**The panel says the daemon is off** — it polls `systemctl is-active` every 15 s.
Start it:

```bash
systemctl --user start ram-keeper.service
journalctl --user -u ram-keeper -n 40
```

**Nothing is ever reclaimed** — check the state and free memory:

```bash
python3 ~/.config/omarchy/plugins/ram-keeper/helper/ram_keeper.py --once | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["state"], d["free_percent"], "reclaimable:", d["reclaimable_total"]//1048576, "MB")'
```

`state=ok` with free memory above `target_free_percent` means the daemon is
correctly doing nothing. Force a pass with `--relieve` to confirm it can act.

**Actions report `denied`** — that cgroup isn't yours to reclaim. Expected for
anything in `system.slice`; they're filtered out before they can be attempted.

**Actions report `eagain`** — normal, not a failure. The kernel reclaimed
everything it could and reports EAGAIN for the rest. The panel counts the bytes
that were actually freed.

## The log

Every pass that freed something is one JSON line in
`~/.local/state/ram-keeper/log.jsonl`:

```json
{"event":"reclaim","reason":"state=tight, free=14.5%, target=18.0%","freed":146800640,"swapped_out":44040192,"actions":[{"label":"Chromium 37812","requested":139460608,"freed":146800640,"swapped_out":44040192,"outcome":"ok"}],"ts":1791228677}
```

Note this is what a *correct* entry looks like when only a fraction of the
system's memory is under pressure: the daemon gives back some, free memory
climbs, and it does not escalate. A log full of ever-growing `requested` values
would mean it is asking for too much per pass — lower `target_free_percent` or
raise `min_target_bytes`.