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
//   3. The daemon's on/off and timed snooze switch.
//
// The panel's buttons are what talk to the daemon; its decisions are never
// second-guessed from here.
Item {
  id: root

  property var shell: null

  // The installed manifest, read by this panel. The shell does not have to
  // inject one: a missing injection used to make the version check a no-op.
  readonly property string manifestPath: {
    var helper = decodeURIComponent(
      String(Qt.resolvedUrl("../helper/ram_keeper.py")).replace(/^file:\/\//, ""))
    return helper.replace("/helper/ram_keeper.py", "/manifest.json")
  }
  property string diskVersion: ""

  // Quattro sanitizes third-party manifests, so resolve the helper from this
  // component and decode the file URL before handing it to a process.
  readonly property string helperPath: decodeURIComponent(
    String(Qt.resolvedUrl("../helper/ram_keeper.py")).replace(/^file:\/\//, ""))

  readonly property string unitName: "ram-keeper.service"

  // Honour RAM_KEEPER_STATE_DIR just like the Python helper and notifier do,
  // falling back to ~/.local/state/ram-keeper.
  readonly property string stateDir: {
    var custom = String(Quickshell.env("RAM_KEEPER_STATE_DIR") || "").trim()
    if (custom !== "") return custom
    var home = String(Quickshell.env("HOME") || "").trim()
    return (home !== "" ? home : "/tmp") + "/.local/state/ram-keeper"
  }

  // The daemon publishes its own snapshot to state.json on every pass. The
  // panel READS THAT FILE rather than spawning `--once` on a timer: the daemon
  // already has the data, including the forecast's trend history, which only
  // exists inside its long-running process.
  readonly property string statePath: root.stateDir + "/state.json"
  readonly property string pauseFlagPath: root.stateDir + "/paused-by-user"

  property var snapshot: ({})
  property bool loaded: false
  property string lastError: ""
  property bool busy: false
  property string activeAction: ""
  property string lastResult: ""

  // Whether automatic protection is actively watching.
  property bool daemonUp: false
  property bool pausedByUser: false
  property bool pauseKnown: false
  property bool versionRestarted: false
  // Wall-clock of the last snapshot the daemon published.
  property double lastHeartbeat: 0
  // Incremented by the poll timer and surfaced over IPC, so a stalled panel is
  // detectable rather than looking like a stable one.
  property int pollTicks: 0

  // B-06 / F2: single serialized state machine for start/stop/pause/resume.
  property bool daemonCtlBusy: false
  property string ctlStep: ""
  property string ctlPauseArg: "indefinite"
  property var queuedCtl: null
  property bool startFailed: false
  property double failedMinHeartbeat: 0
  property double stoppedAt: 0
  property double lastCtlAt: 0
  property double stateReadStartedAt: 0

  // D-22: remember a post-action refresh requested while stateReader was busy.
  property bool refreshPending: false

  // F2: snooze/pause state published by the daemon.
  property bool snoozed: false
  property int snoozeRemaining: 0

  // F3: immediate button feedback while --protected-add/--protected-remove runs.
  property string pendingProtectKey: ""

  // ---------------------------------------------------------------------
  // Snapshot
  // ---------------------------------------------------------------------

  // Re-read the published snapshot. Cheap (one small file), and immediate after
  // our own actions.
  function refresh() {
    if (!stateReader) return
    if (stateReader.running) {
      root.refreshPending = true
      return
    }
    root.refreshPending = false
    root.stateReadStartedAt = Date.now() / 1000
    stateReader.command = ["cat", root.statePath]
    stateReader.running = true
  }

  function applySnapshot(raw) {
    var parsed = Model.parseSnapshot(raw)
    if (!parsed) return          // partial write or not published yet; keep last good
    root.snapshot = parsed
    root.loaded = true
    var nowSec = Date.now() / 1000
    // checked_at is the daemon's own clock at the moment it measured, so a
    // stale file shows a stale timestamp no matter when we read it.
    if (isFinite(Number(parsed.checked_at)) && Number(parsed.checked_at) > 0) {
      root.lastHeartbeat = Number(parsed.checked_at)
      if (root.startFailed && parsed.paused !== true
          && root.lastHeartbeat > (root.failedMinHeartbeat || 0)
          && root.lastHeartbeat <= nowSec + 0.001) {
        root.startFailed = false
      }
    }
    // F2: sync pause/snooze fields published by the daemon only when no
    // lifecycle transition is in flight and this read did not start before the
    // latest control request.
    if (!root.daemonCtlBusy && root.queuedCtl === null
        && (root.stateReadStartedAt || 0) >= (root.lastCtlAt || 0)
        && typeof parsed.paused === "boolean") {
      var ps = Model.parsePauseState(parsed, nowSec)
      root.pausedByUser = ps.paused
      root.snoozed = ps.paused
      root.snoozeRemaining = ps.remaining
    }
    root.noteHeartbeat()
    root.daemonActive()
    root.considerVersionRestart()
  }

  // The daemon publishes the version it loaded at start. This file is the
  // version on disk now. They differ after `omarchy plugin update` until the
  // unit is restarted. One restart per shell start: the flag is not cleared.
  function considerVersionRestart() {
    var running = root.snapshot && root.snapshot.version ? String(root.snapshot.version) : ""
    if (running && root.diskVersion && running !== root.diskVersion
        && !root.pausedByUser && !root.versionRestarted) {
      root.versionRestarted = true
      root.lastError = "Restarting protection so it matches this version"
      if (!versionRestart.running) {
        versionRestart.command = ["systemctl", "--user", "restart", root.unitName]
        versionRestart.running = true
      }
    }
  }

  function refreshManifest() {
    // B-07: read manifest.json once instead of spawning `cat` every 5 seconds.
    if (root.diskVersion !== "" || root.versionRestarted) return
    if (!manifestReader || manifestReader.running) return
    manifestReader.command = ["cat", root.manifestPath]
    manifestReader.running = true
  }

  function applyManifest(raw) {
    var parsed = null
    try {
      parsed = JSON.parse(String(raw || ""))
    } catch (e) {
      return
    }
    if (!parsed || !parsed.version) return
    root.diskVersion = String(parsed.version)
    root.considerVersionRestart()
  }

  // ---------------------------------------------------------------------
  // Manual actions
  // ---------------------------------------------------------------------

  function relieve() {
    if (root.busy) return
    root.busy = true
    root.activeAction = "relieve"
    actionProc.command = ["python3", root.helperPath, "--relieve"]
    actionProc.running = true
  }

  property string listVerb: "release"

  function protect(name) {
    if (!name || protectProc.running) return
    root.listVerb = "protect"
    root.pendingProtectKey = String(name)
    protectProc.command = ["python3", root.helperPath, "--protected-add", String(name)]
    protectProc.running = true
  }

  function unprotect(name) {
    if (!name || protectProc.running) return
    root.listVerb = "release"
    root.pendingProtectKey = String(name)
    protectProc.command = ["python3", root.helperPath, "--protected-remove", String(name)]
    protectProc.running = true
  }

  function sweep() {
    if (root.busy) return
    root.busy = true
    root.activeAction = "sweep"
    actionProc.command = ["python3", root.helperPath, "--sweep-cache"]
    actionProc.running = true
  }

  // ---------------------------------------------------------------------
  // F2 / B-06: Serialized Daemon Pause / Snooze / Resume
  //
  // Every lifecycle transition flows through `daemonCtlProc` one step at a
  // time. If a second click or IPC call arrives mid-flight, `queuedCtl` records
  // the latest intent and runs immediately when the current subprocess exits —
  // two subprocesses never race on `paused-by-user` or `systemctl`.
  // ---------------------------------------------------------------------

  function _parseMinutesArg(raw) {
    if (raw === undefined || raw === null) return 0
    var s = String(raw).trim().toLowerCase()
    if (s === "" || s === "indefinite" || s === "-1" || s === "0") return 0
    if (s === "1h") return 60
    if (s === "30m") return 30
    var n = parseInt(s, 10)
    if (!isFinite(n) || n <= 0) return 0
    return Math.min(1440, n)
  }

  function _requestCtl(action, minutes) {
    var req = { action: action, minutes: minutes || 0 }
    var nowSec = Date.now() / 1000
    root.lastCtlAt = nowSec
    if (action === "pause") {
      root.pausedByUser = true
      root.snoozed = true
      root.snoozeRemaining = req.minutes > 0 ? req.minutes * 60 : 0
      root.daemonUp = false
      root.startFailed = false
      root.stoppedAt = nowSec
    } else {
      root.pausedByUser = false
      root.snoozed = false
      root.snoozeRemaining = 0
    }
    if (root.daemonCtlBusy || daemonCtlProc.running) {
      root.queuedCtl = req
      return
    }
    root._beginCtl(req)
  }

  function _beginCtl(req) {
    root.daemonCtlBusy = true
    if (req.action === "pause") {
      var mins = Number(req.minutes) || 0
      root.ctlPauseArg = mins > 0 ? String(Math.round(mins)) : "indefinite"
      root.ctlStep = "pause-cli"
      daemonCtlProc.command = ["python3", root.helperPath, "--pause", root.ctlPauseArg]
      daemonCtlProc.running = true
    } else {
      root.ctlStep = "resume-cli"
      daemonCtlProc.command = ["python3", root.helperPath, "--resume"]
      daemonCtlProc.running = true
    }
  }

  function snoozeDaemon(minutes) {
    root._requestCtl("pause", root._parseMinutesArg(minutes))
  }

  function pauseDaemon() {
    root._requestCtl("pause", 0)
  }

  function resumeDaemon() {
    root._requestCtl("resume", 0)
  }

  function startDaemon() {
    root.resumeDaemon()
  }

  function stopDaemon() {
    root.pauseDaemon()
  }

  // ---------------------------------------------------------------------
  // Panel lifecycle over IPC
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

  function daemonActive() {
    if (root.pausedByUser || root.snoozed || root.startFailed || root.lastHeartbeat <= 0) {
      root.daemonUp = false
      return
    }
    var now = Date.now() / 1000
    var age = now - root.lastHeartbeat
    // D-22: reject timestamps far in the future (post-NTP step on a dead file)
    // while tolerating small sub-second/clock jitter.
    if (age < -5) {
      root.daemonUp = false
      return
    }
    var idleThreshold = 180
    if (root.snapshot && root.snapshot.interval_idle !== undefined) {
      var di = Number(root.snapshot.interval_idle)
      if (isFinite(di) && di > 0) idleThreshold = Math.max(180, di * 3)
    }
    root.daemonUp = age < idleThreshold
  }

  function noteHeartbeat() {
    // Record the timestamp only. Deciding here would let a stale file revive a
    // dead daemon the instant it is re-read; daemonActive() decides instead.
  }

  // ---------------------------------------------------------------------
  // Processes
  // ---------------------------------------------------------------------

  Process {
    id: manifestReader
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyManifest(text)
    }
  }

  Process {
    id: stateReader
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applySnapshot(text)
    }
    onExited: function(code) {
      if (code !== 0 && root.loaded) root.daemonActive()
      if (root.refreshPending) root.refresh()
    }
  }

  Process {
    id: protectProc
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var msg = String(text || "").trim()
        if (msg !== "") root.lastError = msg.split("\n")[0]
      }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.pendingProtectKey = ""
        if (root.lastError === "") {
          root.lastError = root.listVerb === "protect"
            ? "Could not protect that app"
            : "Could not release that app"
        }
        actionDoneTimer.restart()
      } else {
        root.lastError = ""
      }
      root.refresh()
      protectRefreshTimer.restart()
    }
  }

  Timer {
    id: protectRefreshTimer
    interval: 1200
    repeat: false
    onTriggered: {
      root.pendingProtectKey = ""
      root.refresh()
    }
  }

  Process {
    id: actionProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = Model.parseResult(text)
        if (parsed) {
          root.lastResult = Model.summaryLine(parsed)
          root.refresh()
        } else {
          root.lastResult = ""
        }
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (String(text || "").trim() !== "") root.lastError = String(text).trim().split("\n")[0]
    }
    onExited: {
      root.busy = false
      root.activeAction = ""
      actionDoneTimer.restart()
    }
  }

  Process {
    id: daemonCtlProc
    onExited: function(exitCode) {
      if (root.queuedCtl !== null) {
        var nextReq = root.queuedCtl
        root.queuedCtl = null
        root._beginCtl(nextReq)
        return
      }

      if (root.ctlStep === "pause-cli") {
        if (exitCode === 0) {
          root.daemonCtlBusy = false
          root.ctlStep = ""
          root.lastError = ""
          root.refresh()
          daemonCheckTimer.restart()
        } else {
          // Fallback for pre-v1.2 helper without --pause: write paused-by-user
          // safely via argv (no shell interpolation) and stop the unit.
          root.snoozeRemaining = 0
          root.ctlStep = "pause-fallback-flag"
          daemonCtlProc.command = ["python3", "-c",
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('1\\n', encoding='utf-8')",
            root.pauseFlagPath]
          daemonCtlProc.running = true
        }
        return
      }

      if (root.ctlStep === "pause-fallback-flag") {
        if (exitCode !== 0) {
          root.daemonCtlBusy = false
          root.ctlStep = ""
          root.lastError = "Could not remember that protection is paused"
          daemonCheckTimer.restart()
        } else {
          root.ctlStep = "pause-fallback-stop"
          daemonCtlProc.command = ["systemctl", "--user", "stop", root.unitName]
          daemonCtlProc.running = true
        }
        return
      }

      if (root.ctlStep === "pause-fallback-stop") {
        root.daemonCtlBusy = false
        root.ctlStep = ""
        root.lastHeartbeat = 0
        root.lastError = exitCode !== 0 ? "Could not stop the ram-keeper daemon" : ""
        daemonCheckTimer.restart()
        return
      }

      if (root.ctlStep === "resume-cli") {
        if (exitCode === 0) {
          root.ctlStep = "resume-start"
          daemonCtlProc.command = ["systemctl", "--user", "start", root.unitName]
          daemonCtlProc.running = true
        } else {
          // Fallback for pre-v1.2 helper without --resume.
          root.ctlStep = "resume-fallback-clear"
          daemonCtlProc.command = ["rm", "-f", "--", root.pauseFlagPath]
          daemonCtlProc.running = true
        }
        return
      }

      if (root.ctlStep === "resume-fallback-clear") {
        if (exitCode !== 0) {
          root.daemonCtlBusy = false
          root.ctlStep = ""
          root.lastError = "Could not clear the pause"
          daemonCheckTimer.restart()
        } else {
          root.ctlStep = "resume-start"
          daemonCtlProc.command = ["systemctl", "--user", "start", root.unitName]
          daemonCtlProc.running = true
        }
        return
      }

      if (root.ctlStep === "resume-start") {
        root.daemonCtlBusy = false
        root.ctlStep = ""
        if (exitCode !== 0) {
          root.startFailed = true
          root.failedMinHeartbeat = Math.max(Date.now() / 1000, root.lastHeartbeat || 0)
          root.daemonUp = false
          root.lastError = "Could not start the ram-keeper daemon"
        } else {
          root.startFailed = false
          root.lastError = ""
          root.refresh()
          postStartRefreshTimer.restart()
        }
        daemonCheckTimer.restart()
        return
      }

      root.daemonCtlBusy = false
      root.ctlStep = ""
    }
  }

  Process {
    id: pauseRead
    onExited: function(code) {
      root.pauseKnown = true
      if ((root.lastCtlAt || 0) > 0 || root.daemonCtlBusy || root.queuedCtl !== null
          || (root.loaded && root.snapshot && typeof root.snapshot.paused === "boolean")) {
        root.daemonActive()
        return
      }
      root.pausedByUser = (code === 0)
      root.snoozed = (code === 0)
      root.daemonActive()
      if (!root.pausedByUser && !root.daemonUp) startDaemonTimer.restart()
    }
  }

  Process {
    id: versionRestart
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.lastError = "Could not restart protection for this version"
      } else {
        root.lastError = ""
        postStartRefreshTimer.restart()
      }
    }
  }

  // ---------------------------------------------------------------------
  // Timers
  // ---------------------------------------------------------------------

  Timer {
    id: actionDoneTimer
    interval: 9000
    repeat: false
    onTriggered: {
      root.lastResult = ""
      root.lastError = ""
    }
  }

  Timer {
    id: daemonCheckTimer
    interval: 1500
    repeat: false
    onTriggered: root.daemonActive()
  }

  Timer {
    id: postStartRefreshTimer
    interval: 1200
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: pollTimer
    interval: 5000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: {
      root.pollTicks += 1
      root.daemonActive()
      root.refresh()
      root.refreshManifest()
    }
  }

  Component.onCompleted: {
    root.pollTicks += 1
    pauseRead.command = ["test", "-f", root.pauseFlagPath]
    pauseRead.running = true
    root.refresh()
    root.refreshManifest()
  }

  Timer {
    id: startDaemonTimer
    interval: 600
    repeat: false
    onTriggered: {
      if (root.pauseKnown && !root.daemonUp && !root.pausedByUser
          && !root.snoozed && !root.daemonCtlBusy && root.queuedCtl === null) {
        root.startDaemon()
      }
    }
  }

  // ---------------------------------------------------------------------
  // IPC
  // ---------------------------------------------------------------------

  IpcHandler {
    target: "io.github.igorbnp.ram-keeper"

    function status(): string {
      var hasReading = root.loaded && root.snapshot
      var stateVal = hasReading && root.snapshot.state
        ? String(root.snapshot.state) : "unknown"
      var freeNum = hasReading ? Number(root.snapshot.free_percent) : NaN
      var freeVal = (hasReading && root.snapshot.free_percent !== null
        && isFinite(freeNum)) ? freeNum : null
      var ageVal = (root.loaded && root.lastHeartbeat > 0)
        ? Math.max(0, Math.round(Date.now() / 1000 - root.lastHeartbeat))
        : null
      return JSON.stringify({
        loaded: root.loaded,
        state: stateVal,
        free: freeVal,
        daemon: root.daemonUp,
        ticks: root.pollTicks,
        ageSeconds: ageVal,
        paused: root.pausedByUser || root.snoozed,
        snoozed: root.snoozed,
        snoozeRemaining: root.snoozeRemaining,
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

    // F2: Pause / timed snooze / resume over IPC.
    function pause(): void { root.pauseDaemon() }
    function snooze(minutes: string): void { root.snoozeDaemon(minutes || "30") }
    function resume(): void { root.resumeDaemon() }
  }
}
