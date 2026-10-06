# ram-keeper

Automatic memory steward for an Omarchy desktop that doesn't have RAM to
spare. It watches real pressure signals, gives memory back before anything
slows down, and stays out of the way.

Nothing to configure. Nothing to remember. If memory gets tight, it is already
being handled.

> **This plugin was written with AI assistance (vibe-coded), not by hand.**
> Every design decision and every measurement in this README comes from
> experiments run on the machine it was built on, and the code has been through
> five independent adversarial review passes — but you should know that no human
> typed it line by line. Section [Known limitations](#known-limitations) records
> the bugs that review actually caught, including one release where the daemon
> killed a browser. Judge it on the tests, not on the prose.

---

## About the code

Vibe-coded, then attacked. The workflow was: an AI wrote it, then four
independent agents were given the job of breaking it with no knowledge of the
design intent, and a fifth reviewed the diff. Findings and fixes are listed
under [Known limitations](#known-limitations) rather than hidden — including the
ones the author introduced while fixing earlier ones.

What that process caught, in one list, because it says more about the code than
any description would:

- A rate limit spent in the wrong unit, making it a no-op. The pass budget was
  decremented by *RAM returned*, which is ~0 for a move to compressed storage, so
  one pass could request 3 GB. This is the same class of bug that had killed a
  browser earlier.
- An alert that could never fire. It grepped the kernel's OOM message from the
  user journal, which does not contain kernel lines — dead code that looked
  perfectly correct.
- A button labelled "Drop cache" that also swapped anonymous memory to zram.
- A panel that froze on its first reading and reported stale numbers forever,
  because a Quickshell `Timer` needs `running: true` to be explicit.
- An installer path test that passed while the installer was broken: the test
  reimplemented the substitution instead of calling it, so a plugin directory
  containing a space had never been given to systemd. The fix needed quoting the
  whole argument, and the test now starts a throwaway unit to prove systemd
  accepts the result.

None of these would have shown up in normal use. They surfaced because the tests
assert on behaviour instead of on the presence of code.

---

## What makes this different from a memory monitor

Every other tool on this problem — `btop`, `free`, GNOME System Monitor, the
`earlyoom` family — reports or reacts to a number. Two things here do not.

**It predicts instead of only reacting.** A floor-based trigger fires when free
memory crosses a line, which is the same moment reclaim has become urgent. This
fits a least-squares trend to the last ten minutes of free memory and reports
*when the floor will be crossed*, plus which process is growing. Verified on a
live desktop: a process climbing at 12 MB/s produced an `imminent` verdict —
"floor in 92 seconds" — while free memory was still at 21% and the machine was
not yet slow. The daemon acts on that verdict before the number moves, which is
the only reason the stall never starts. A fit is scored (R²) and a forecast
built on a poor one is downgraded rather than acted on, so noise does not
manufacture urgency.

**It tells you the difference between fast swap and slow swap.** Every monitor
shows one combined "swap: 40%" figure, which is actively misleading. A full
`zram` device is *compressed RAM*: it costs CPU and nothing else. Five percent of
a disk swapfile is a machine you can feel. The panel lists them separately and
deliberately does not flag `zram` as a problem at any fill level, because a
"90% full" zram is a healthy zram.

**It reports what actually happened, not what the kernel counted.** Moving 300 MB
of anonymous memory into `zram` at a measured 4.4× compression ratio returns
~68 MB of real RAM. The panel's "freed" figure accounts for that; the raw
per-cgroup drop is kept alongside it for auditing rather than being passed off
as a fourfold benefit.

---

## What it actually does

The hard part of memory management on Linux is that **using RAM is not the
problem** — a healthy Linux machine is *supposed* to have most of its RAM in
use, and forcing that memory out costs performance. The problem is only
*sustained pressure*: reclaim thrashing where every allocation has to wait.

So this app never triggers on "memory is being used". It triggers on:

| Signal | Source | What it means |
|---|---|---|
| Free memory below 18% | `/proc/meminfo` `MemAvailable` | Normal, healthy pressure. Start giving memory back. |
| Free memory below 10% | same | Critical. Reclaim harder. |
| Memory stall above 6% | `/proc/pressure/memory` (`avg60`) | The machine is *waiting* on memory even if free RAM looks fine — the free memory is in the wrong place. |
| **Floor crossing in < 2 min** | least-squares trend | Imminent. Reclaim before the number breaks. |
| Per-cgroup `memory.current` | cgroup v2 | Which specific process can give memory back. |
| **Unreadable signals** | any of the above | Report `No reading` and do nothing. A signal that fails must never escalate into the most aggressive response available. |

`MemAvailable` is used rather than `MemFree` on purpose: `MemFree` counts
reclaimable page cache as unused, so it reads as "0 bytes free" on a completely
healthy machine.

### The order it gives memory back

Cheapest and least visible first:

1. **Page cache** — files the system read but hasn't needed again. Dropping it
   costs nothing; the next read just hits the disk once. Invisible to running
   apps.
2. **Anonymous memory → zram** — app heaps and stacks, moved into the
   compressed-RAM swap device. Costs a little CPU, no disk I/O, and the app
   never knows. A process that touches the memory again gets it back at
   compression speed.
3. **Anonymous memory → disk swap** — last resort. The `/swap/swapfile` is
   still there as a safety net for the case where zram is itself full, but
   reclaim deliberately prefers zram (see `CONFIG.md`).

A cgroup with `MemorySwapMax=0` can never return anonymous memory, so it is
skipped in the swap pass rather than asked every time — otherwise each pass
burns its budget on a victim that was never able to help.

### What it never does

- **Never kills a process.** That's `systemd-oomd`'s job, and it's better at it
  (it keys on PSI stall time, so it acts *while* the machine is thrashing
  rather than after an allocation has already failed). The desktop survived
  every test here with zero OOM kills.
- **Never touches the compositor.** Hyprland lives in `session-N.scope` and is
  protected by a hard-coded list that **no config file can override** — writing
  `"protected": []` does not unprotect it. A machine that kills its window
  manager has failed, not saved itself.
- **Never touches system daemons.** They live in `system.slice`, which the
  kernel refuses to let a user reclaim from anyway. The app reads them for
  reporting only.
- **Never writes a global sysctl.** No `swappiness`, no `drop_caches`, no
  `watermark` games. It only writes `memory.reclaim` inside cgroups it owns.
- **Never needs root.** Everything happens inside `user.slice`.

---

## Layout

```
~/.config/omarchy/plugins/io.github.igorbnp.ram-keeper/
├── manifest.json              # omarchy plugin manifest (service + bar-widget)
├── install.sh                 # idempotent installer
├── helper/
│   ├── ram_keeper.py          # the daemon — plain stdlib Python 3.9+
│   └── ram_keeper_notify      # event-driven alerts + one-click AI diagnosis
├── qml/
│   ├── Service.qml            # owns the daemon, reads the published snapshot
│   ├── Panel.qml              # the bar widget and its panel
│   └── Model.js               # pure data shaping (unit-tested under node)
├── units/
│   ├── ram-keeper.service     # the user systemd unit
│   └── ram-keeper-notify.service.in
├── CONFIG.md                  # thresholds and tuning
└── LICENSE                    # MIT
```

## Requirements

No installation step fetches anything from the network, and no root is ever
required. Everything used ships with Omarchy or is already required by a
desktop session:

| Dependency | Used for | Ships with |
|---|---|---|
| `python3` ≥ 3.9, standard library only | the daemon and the notifier | Omarchy (Arch) |
| cgroup v2 (`/sys/fs/cgroup`) | all memory accounting and reclaim | kernel 5.12+ |
| PSI (`/proc/pressure/memory`) | the stall signal | kernel 4.20+ |
| `systemd` user units | supervision | Omarchy |
| `zram` | compressed-RAM swap, the reclaim target | `zram-generator` (Arch) |
| `jq`, `grep`, `md5sum` | the notifier only | coreutils / Arch |
| `omarchy-notification-send` | desktop alerts | Omarchy |

The daemon requires cgroup v2 and a `zram` device. `install.sh` checks both and
says so plainly instead of failing later.

## Install

```bash
omarchy plugin add https://github.com/igorbnp/ram-keeper
bash ~/.config/omarchy/plugins/io.github.igorbnp.ram-keeper/install.sh
omarchy restart shell
```

Or, if you are reading this inside a checkout:

```bash
bash install.sh && omarchy restart shell
```

The installer is idempotent — safe to run after every update. It verifies its
own preconditions (cgroup v2, zram, swap priority, `systemd-oomd` active),
installs the units, restarts the daemon so new code actually runs, and proves
it can read your memory state before reporting success.

## Remove

```bash
systemctl --user disable --now ram-keeper.service ram-keeper-notify.service
rm -f ~/.config/systemd/user/ram-keeper.service \
      ~/.config/systemd/user/ram-keeper-notify.service
systemctl --user daemon-reload
rm -rf ~/.config/omarchy/plugins/io.github.igorbnp.ram-keeper
rm -rf ~/.local/state/ram-keeper ~/.config/ram-keeper
omarchy plugin disable io.github.igorbnp.ram-keeper
omarchy restart shell
```

Nothing else on the system is modified. There is no config written outside
those two directories, no sysctl changed, no system file touched — the daemon
only writes `memory.reclaim` inside cgroups it already owns.

## The panel

Click the bar widget (or `omarchy-shell shell toggle ram-keeper`). It shows:

- **Headline** — `Healthy · 42% free`, with the state as the first word.
- **Memory bar** — one strip split into Apps / Cache / Free, because reading
  proportions off one bar is faster than reading three numbers. The split sums
  exactly to `MemTotal`.
- **Free memory now** — act once, on demand.
- **Drop cache** — the manual version of the cheap pass.
- **Memory stall** — the PSI number, which is the honest answer to "is this
  machine actually struggling". `None` means genuinely none.
- **Swap rows** — disk swap and zram listed separately, and zram is explicitly
  *not* flagged red when it's full, because a full zram is compressed RAM, not
  a performance cliff.
- **Available to reclaim** — the processes the daemon would take from, largest
  first, with their sizes.
- **Never reclaimed** — what's protected and why it's still listed: Hyprland,
  the Hermes gateway, SIA, Syncthing. Transparency about what it's choosing not
  to touch.

Right-click the bar widget to toggle the percentage on and off.

## Verify it's working

```bash
# is the daemon alive?
systemctl --user status ram-keeper.service

# what does it think is going on?
python3 ~/.config/omarchy/plugins/ram-keeper/helper/ram_keeper.py --once

# what has it done today?
tail -5 ~/.local/state/ram-keeper/log.jsonl | python3 -m json.tool --json-lines

# force one pass by hand
python3 ~/.config/omarchy/plugins/ram-keeper/helper/ram_keeper.py --relieve
```

## Known limitations

Stated plainly, because a memory tool that hides its own edges is worse than
one that names them.

- **The forecast needs about two minutes of history.** A daemon that just
  started has no trend and says `Steady`. This is inherent: a trend needs
  samples. The panel shows `Steady — nothing is trending` rather than a blank.
- **The "freed" figure depends on the live zram compression ratio**, which
  varies with the data. Highly compressible memory (zeros, sparse buffers)
  inflates it; incompressible data deflates it. The ratio is read from
  `/sys/block/zram0/mm_stat` at the time of each pass, not assumed.
- **`per-cgroup PSI` is not used**, because `cgroup.pressure` is not populated
  on all kernels — on the kernel this was developed against (7.2.5) every
  cgroup reports a bare `"1"`. Victims are scored by `memory.current` and
  `memory.stat` instead. Only the global `/proc/pressure/memory` is read.
- **Reclaim cannot help if zram *and* disk swap are both full.** At that point
  the only remaining lever is killing a process, which this tool deliberately
  leaves to `systemd-oomd`. You will get a critical toast instead of a silent
  stall.
- **A cgroup under `MemorySwapMax=0` can only give back page cache.** Its
  anonymous memory is unreachable by design, so a machine relying on
  `MemorySwapMax=0` everywhere will see the cache pass do all the work.
- **Suspend does not distort the trend.** `CLOCK_MONOTONIC` stops during sleep
  on Linux, and the trend window is 10 minutes, so a long suspend falls out of
  the window. A short one coinciding with a real drop reads as the drop it is.
  Tested both ways.
- **Sustained-load testing was 30 minutes of synthetic pressure**, not weeks of
  real use. It was enough to catch a class of bugs (leaks, stale reads, restart
  storms, log growth) that burst tests miss, but it is not the same as living
  with it.
- **The reclaim budget must be spent in the same unit it is capped in.** An
  earlier revision tracked "RAM returned" (which is ~0 for an anon→zram move,
  because compressed storage is not RAM) and subtracted *that* from the pass
  budget. The budget therefore never decreased, and both `max_reclaim_per_pass`
  and the per-pass ceiling were decorative: one pass could ask 3 GB, the same
  shape that kills a process. The budget is now spent in kernel-visible bytes —
  what was asked — while the honest effective figure stays in the report.
- **"Drop cache" drops cache.** It once ran the anonymous passes too, so
  clicking it on a healthy desktop swapped out Chromium and the Hyprland
  launcher: 658 MB into zram for no memory benefit, under a button labelled
  "Drop cache". It is now cache-only.
- **The first version of this plugin killed a browser, and only sustained load
  revealed it.** The anon pass asked a cgroup for as much as all of its
  anonymous memory; under continuous load that became a 1.5 GB request against a
  300 MB Chromium, which emptied the process and got it OOM-killed. Every
  individual reclaim had reported `outcome: ok`. Reclaim is now capped at a
  fraction of each victim and at a fraction of what is actually reclaimable per
  pass, so pressure is relieved over several small passes instead of one
  destructive one. This is the single most important limit in the code, and it
  exists because guessing at it was not good enough.

## What the tests actually proved

| Claim | Evidence |
|---|---|
| Reclaim goes to RAM, not disk | the disk swapfile *shrank* (39 MB → 24 MB) during a 3 GB reclaim |
| It acts before the floor | `trending` verdict 92 s ahead, at 21% free |
| The desktop survives | 3.2 GB of extra load for 4 min unattended: worst case 21% free, 0.8% stall, zero OOM kills, Hyprland/quickshell/Hermes all alive |
| The compositor is unreachable | `"protected": []`, `"protected": ["my-app"]`, and an explicit unprotect all leave it protected — verified by enumeration, not by reading the code |
| A bad config cannot hurt you | 25 hostile configs (wrong types, `NaN`, `Infinity`, inverted thresholds, truncated JSON, non-list) all degrade to a working daemon |
| Nothing is killed | 0 OOM kills under sustained load, after the per-victim cap was added in response to a kill it *did* cause (see Known limitations) |

## Operational notes

- **Cost:** ~14 MB RSS idle. `MemoryHigh=192M`, `MemoryMax=384M` in the unit, so
  a runaway loop can't become the problem it was built to prevent.
- **Cadence:** every 20 s when healthy, every 5 s under pressure.
- **Sandbox:** `ProtectSystem=strict`, `ProtectHome=read-only` with a single
  `ReadWritePaths` for its own state directory. It needs nothing else.
- **Safety valve:** if the daemon is wrong, `systemctl --user stop
  ram-keeper.service` changes nothing about the machine's behaviour — the
  kernel, oomd, and zram were already handling it. The app only makes them act
  earlier.

## Tested on

Omarchy 4.0.4, kernel 7.2.5-3-omarchy, systemd 261, cgroup v2, 8 GB RAM, zram
at `ram` size with zstd, `vm.swappiness=150`, `systemd-oomd` on `app.slice`.

Verified under real pressure, not mocks: a 3 GB anonymous allocation drove the
machine to 6.8% free; the daemon recovered it to the 12–17% band unattended
over 90 seconds, moved 808 MB of the hog into zram, **kept the hog running**,
kept Hyprland/quickshell/Hermes alive, and produced zero kernel OOM kills. The
disk swapfile *shrank* during the test (39 MB → 24 MB), which is the proof that
reclaim went to RAM and not to the disk.