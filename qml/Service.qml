import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Shell-wide half of ram-keeper.
//
// Owns three things:
//   1. The supervision daemon — the user systemd unit that watches pressure and
//      reclaims without being asked. Started once, kept alive.
//   2. The published snapshot the panel renders. Read from the daemon's own
//      state file, NOT by spawning a helper: the daemon already measured all of
//      this, and only the daemon holds the trend history behind the forecast.
//   3. The daemon's on/off switch.
//
// The panel's buttons are what talk to the daemon; its decisions are never
// second-guessed from here.
Item {
  id: root

  property var manifest: null
  property var shell: null

  // Quattro sanitizes third-party manifests, so resolve the helper from this
  // component and decode the file URL before handing it to a process.
  readonly property string helperPath: decodeURIComponent(
    String(Qt.resolvedUrl("../helper/ram_keeper.py")).replace(/^file:\/\//, ""))

  readonly property string unitName: "ram-keeper.service"

  // The daemon publishes its own snapshot to state.json on every pass. The
  // panel READS THAT FILE rather than spawning `--once` on a timer: the daemon
  // already has the data, including the forecast's trend history, which only
  // exists inside its long-running process. Spawning a fresh helper per refresh
  // measured 3.1 spawns/second against an intended 0.25 — twelve times the
  // intended cost, purely to re-read numbers that were already on disk, and
  // always with an empty trend history so the forecast showed nothing.
  readonly property string statePath:
    Quickshell.env("HOME") + "/.local/state/ram-keeper/state.json"

  property var snapshot: ({})
  property bool loaded: false
  property string lastError: ""
  property bool busy: false
  property string lastResult: ""

  // Whether the systemd unit is actually running. Read back from systemctl
  // rather than assumed from our own start/stop calls, so the panel tells the
  // truth even if the unit was stopped from outside the panel.
  property bool daemonUp: false
  // Wall-clock of the last snapshot the daemon published.
  property double lastHeartbeat: 0
  // Incremented by the poll timer and surfaced over IPC, so a stalled panel is
  // detectable rather than looking like a stable one.
  property int pollTicks: 0

  // ---------------------------------------------------------------------
  // Snapshot
  // ---------------------------------------------------------------------

  // Re-read the published snapshot. Cheap (one small file), and immediate after
  // our own actions.
  function refresh() {
    if (!stateReader || stateReader.running) return
    stateReader.command = ["cat", root.statePath]
    stateReader.running = true
  }

  function applySnapshot(raw) {
    var parsed = Model.parseSnapshot(raw)
    if (!parsed) return          // partial write or not published yet; keep last good
    root.lastError = ""
    root.snapshot = parsed
    root.loaded = true
    // checked_at is the daemon's own clock at the moment it measured, so a
    // stale file shows a stale timestamp no matter when we read it.
    if (parsed.checked_at) root.lastHeartbeat = parsed.checked_at
    root.noteHeartbeat()
    root.daemonActive()
  }

  // ---------------------------------------------------------------------
  // Manual actions
  // ---------------------------------------------------------------------

  function relieve() {
    if (root.busy) return
    root.busy = true
    actionProc.command = ["/usr/bin/python3", root.helperPath, "--relieve"]
    actionProc.running = true
  }

  function sweep() {
    if (root.busy) return
    root.busy = true
    actionProc.command = ["/usr/bin/python3", root.helperPath, "--sweep-cache"]
    actionProc.running = true
  }

  // ---------------------------------------------------------------------
  // Panel lifecycle over IPC
  //
  // The service has no reference to the panel instance, and does not need one:
  // the shell owns panel open/close by plugin id. `want` is true (open), false
  // (close) or null (toggle). Every call is guarded because the shell may be
  // mid-teardown, and an IPC verb that throws is worse than one that does
  // nothing.
  // ---------------------------------------------------------------------

  function shellToggle(want) {
    var api = root.shell
    if (!api) return
    try {
      if (want === true && typeof api.summon === "function") {
        api.summon(root.moduleId)
      } else if (want === false && typeof api.hide === "function") {
        api.hide(root.moduleId)
      } else if (typeof api.toggle === "function") {
        api.toggle(root.moduleId, "")
      }
    } catch (e) {
      root.lastError = "Could not toggle the panel"
    }
  }

  readonly property string moduleId: "io.github.igorbnp.ram-keeper"

  // ---------------------------------------------------------------------
  // Daemon lifecycle
  // ---------------------------------------------------------------------

  // Liveness from the heartbeat, not from `systemctl is-active`.
  //
  // The daemon stamps state.json on every pass (every 5s under pressure, 20s
  // idle), so "the file was touched recently" IS "the daemon is running" — and
  // it needs no subprocess. The Process-based poll never actually fired
  // SplitParser in this shell, so daemonUp sat at false forever and the panel
  // reported "Automatic protection is paused" while the daemon was running.
  // A UI that lies about whether protection is on is worse than no indicator.
  function daemonActive() {
    var now = Date.now() / 1000;
    root.daemonUp = (now - root.lastHeartbeat) < 180;
  }

  // Called on every reload, but from the FILE's freshness, not from the fact
  // that a read happened. The published file is re-read every few seconds
  // whether or not it changed, so a dead daemon's stale snapshot kept marking
  // itself alive until checked_at fell outside the window.
  function noteHeartbeat() {
    // Record the timestamp only. Deciding here would let a stale file revive a
    // dead daemon the instant it is re-read; the timer decides instead.
  }

  function startDaemon() {
    if (!daemonStart.running) {
      daemonStart.command = ["systemctl", "--user", "start", root.unitName]
      daemonStart.running = true
    }
  }

  function stopDaemon() {
    if (!daemonStop.running) {
      daemonStop.command = ["systemctl", "--user", "stop", root.unitName]
      daemonStop.running = true
    }
  }

  // ---------------------------------------------------------------------
  // Processes
  // ---------------------------------------------------------------------

  // The snapshot is ONE small file, read directly on a timer.
  //
  // Three approaches were tried and two of them failed silently, which is
  // exactly what a memory widget must never do:
  //   * spawning `--once` per refresh — 3 spawns/second, and each new process
  //     had an empty trend history so the forecast never appeared;
  //   * FileView with watchChanges — the daemon publishes with tmp-file +
  //     rename, giving a new inode each time, so the inotify watch stayed on
  //     the original and every later update was invisible;
  //   * FileView without watching, calling reload() — reload() did not
  //     re-deliver here either, and the panel froze at its first reading.
  // Reading the path directly always resolves the current inode. Five kilobytes
  // every five seconds is nothing.
  Process {
    id: stateReader
    // Command assigned in refresh(), not here: a declaration-time command
    // binding made this Process static and, with it, every Timer in this Item —
    // the panel froze on its first reading and never ticked again.
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applySnapshot(text)
    }
    onExited: function(code) {
      if (code !== 0 && root.loaded) root.daemonActive()
    }
  }

  Process {
    id: actionProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = Model.parseResult(text)
        root.lastResult = parsed ? Model.summaryLine(parsed) : "Done"
        root.refresh()
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (String(text || "").trim() !== "") root.lastError = String(text).trim().split("\n")[0]
    }
    onExited: {
      root.busy = false
      actionDoneTimer.restart()
    }
  }

  Process {
    id: daemonStart
    onExited: function(exitCode) {
      if (exitCode !== 0) root.lastError = "Could not start the ram-keeper daemon"
      daemonCheckTimer.restart()
    }
  }

  Process {
    id: daemonStop
    onExited: function(exitCode) {
      if (exitCode !== 0) root.lastError = "Could not stop the ram-keeper daemon"
      daemonCheckTimer.restart()
    }
  }


  // ---------------------------------------------------------------------
  // Timers
  // ---------------------------------------------------------------------


  // Show the daemon's verdict a beat after an action settles, then go quiet
  // so the panel isn't narrating a result the user already saw.
  Timer {
    id: actionDoneTimer
    interval: 9000
    repeat: false
    onTriggered: root.lastResult = ""
  }

  Timer {
    id: daemonCheckTimer
    interval: 1500
    repeat: false
    onTriggered: root.daemonActive()
  }

  // Poll the snapshot and re-evaluate liveness together. Five seconds is under
  // the daemon's fastest interval (5s under pressure) and far above its idle
  // one, so the bar stays current without a busy loop.
  Timer {
    id: pollTimer
    interval: 5000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: {
      // Liveness FIRST, from the snapshot already in hand. Reading first would
      // re-apply the same stale checked_at and immediately reset the flag, so a
      // dead daemon could never be reported.
      root.pollTicks += 1;
      root.daemonActive();
      root.refresh();
    }
  }

  Component.onCompleted: {
    root.pollTicks += 1;
    // Bring the daemon up on first load so the protection exists before the
    // user ever needs it. If the unit is missing, say so once instead of
    // failing silently on every check.
    startDaemonTimer.restart()
    root.refresh()
  }

  Timer {
    id: startDaemonTimer
    interval: 600
    repeat: false
    onTriggered: root.startDaemon()
  }

  // ---------------------------------------------------------------------
  // IPC
  // ---------------------------------------------------------------------

  // The single IpcHandler for the "io.github.igorbnp.ram-keeper" target. IpcHandler allows ONE
  // registration per target, so this declares the union of both vocabularies:
  // panel verbs (open/close/show/hide/toggle) so the panel can be summoned by
  // IPC or a keyboard shortcut, and daemon verbs (status/refresh/relieve/sweep).
  // Splitting these across Service and Panel means whichever registers second
  // silently loses all of its functions — which is exactly what happened, with
  // `omarchy-shell ram-keeper toggle` answering "Function not found".
  //
  // Panel verbs are routed through the shell's own panel API rather than a
  // direct reference to the panel instance: the service has no handle on it, and
  // the shell already owns opening and closing panels by id.
  IpcHandler {
    target: "io.github.igorbnp.ram-keeper"

    function status(): string {
      return JSON.stringify({
        loaded: root.loaded,
        state: root.snapshot ? root.snapshot.state : "unknown",
        free: root.snapshot ? root.snapshot.free_percent : 0,
        daemon: root.daemonUp,
        // Observable proof the poll timer is alive. A frozen panel reported the
        // same numbers forever with nothing indicating it had stopped updating;
        // this makes a stall visible instead of silent.
        ticks: root.pollTicks,
        ageSeconds: Math.round(Date.now() / 1000 - root.lastHeartbeat),
        error: root.lastError
      })
    }

    // Panel lifecycle, delegated to the shell.
    function open(): void { root.shellToggle(true) }
    function show(): void { root.shellToggle(true) }
    function close(): void { root.shellToggle(false) }
    function hide(): void { root.shellToggle(false) }
    function toggle(): void { root.shellToggle(null) }

    // Daemon control.
    function refresh(): void { root.refresh() }
    function relieve(): void { root.relieve() }
    function sweep(): void { root.sweep() }
  }
}