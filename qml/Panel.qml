import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The bar slot and the panel behind it.
//
// The bar shows one number — how much memory is free — plus a glyph that
// changes with the pressure state. Everything else lives in the panel, so the
// bar stays quiet enough to live on a 7GB machine without becoming the thing
// the user has to look at.
Panel {
  id: root
  moduleName: "io.github.igorbnp.ram-keeper"
  ipcTarget: "io.github.igorbnp.ram-keeper"
  // manageIpc: false hands the single per-target IpcHandler to the Service,
  // which declares the union of panel verbs (open/close/show/hide/toggle) and
  // daemon verbs (status/refresh/relieve/sweep/pause/snooze/resume) in ONE place.
  manageIpc: false

  readonly property var keeper: root.bar && root.bar.shell
    && typeof root.bar.shell.serviceFor === "function"
    ? root.bar.shell.serviceFor("io.github.igorbnp.ram-keeper") : null

  readonly property var snap: root.keeper && root.keeper.loaded ? root.keeper.snapshot : ({})

  readonly property string state: snap.state || "unknown"
  readonly property bool showPercent: root.setting("showPercent", true) === true
  readonly property bool loaded: !!(root.keeper && root.keeper.loaded)
  readonly property bool busy: !!(root.keeper && root.keeper.busy)
  readonly property bool daemonUp: !!(root.keeper && root.keeper.daemonUp)
  readonly property bool pausedOrSnoozed: !!(root.keeper && (root.keeper.pausedByUser || root.keeper.snoozed))
  readonly property bool daemonOff: root.pausedOrSnoozed || (root.loaded && !root.daemonUp)
  readonly property bool ctlBusy: !!(root.keeper && root.keeper.daemonCtlBusy)
  readonly property double liveNow: {
    var tick = root.keeper ? root.keeper.pollTicks : 0
    void tick
    return Date.now() / 1000
  }
  readonly property var forecast: root.loaded
    ? Model.forecastForSnapshot(root.snap) : null
  readonly property var sessionImpact: root.loaded
    ? Model.sessionImpactRow(root.snap, root.liveNow) : null

  // True once the service object exists. Every control that calls into it must
  // gate on this, not on `!busy`: a null keeper makes `busy` false, so the busy
  // guard alone leaves the button enabled and the handler throws.
  readonly property bool actionsReady: root.keeper !== null && root.keeper !== undefined

  // `vertical` lives on BarWidget, not on Ui/Panel — this widget extends Panel,
  // so root.vertical does not exist and silently evaluates false.
  readonly property bool verticalBar: root.bar ? root.bar.vertical : false

  readonly property color foreground: root.bar ? root.bar.barForeground : Color.foreground
  readonly property color accentColor: root.bar && root.bar.accentColor
    ? root.bar.accentColor : Color.accent
  readonly property color urgentColor: root.bar && root.bar.urgent ? root.bar.urgent : Color.urgent

  // Urgency drives colour everywhere in the panel; the bar glyph is the only
  // place we tint, so a healthy machine is visually quiet.
  readonly property color stateColor: Model.isUrgent(state)
    ? root.urgentColor
    : (Model.isWarning(state) ? Qt.darker(root.accentColor, 1.25) : root.foreground)

  readonly property string glyph: Model.stateGlyph(state)
  readonly property string percentText: loaded ? Model.freePercentLabel(snap) : "--"
  readonly property string barLabelText: loaded ? Model.barLabel(snap) : "--"

  // Free memory is the headline: the number the user actually cares about is
  // "how much room do I have", not "how much am I using".
  //
  // Sizing note: "100% " plus a Nerd Font glyph paints ~50px at this theme's
  // 11px base, while Style.space(30) is only 28px. Nothing in the widget
  // chain clips, so an undersized slot lets the text overflow and collide with
  // the neighbouring bar modules. A vertical bar has no room for text at all, so
  // it gets the bare glyph.
  readonly property real barWidth: (showPercent && !verticalBar)
    ? Style.bar.iconSlot * 2
    : Style.bar.iconSlot

  // The bar reads this to place the open-panel indicator under a widget that
  // paints text rather than a centred glyph (see power/Panel.qml).
  readonly property real openPanelIndicatorWidth:
    (showPercent && !verticalBar) ? button.glyphPaintedWidth : 0

  function togglePercent() {
    root.settings = Object.assign({}, root.settings, { showPercent: !root.showPercent })
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, root.settings)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---------------------------------------------------------------------
  // Bar button
  // ---------------------------------------------------------------------

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    slotSize: root.barWidth
    text: root.glyph
    labelText: (root.showPercent && !root.verticalBar) ? root.barLabelText : ""
    tooltipText: root.loaded ? Model.barTooltip(root.snap, root.liveNow) : "Measuring memory…"
    onPressed: function(b) {
      if (b === Qt.RightButton) root.togglePercent()
      else root.toggle()
    }
  }

  // ---------------------------------------------------------------------
  // Panel
  // ---------------------------------------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Keys.onUpPressed: flickable.contentY = Math.max(0, flickable.contentY - 40)
      Keys.onDownPressed: {
        var maxY = Math.max(0, flickable.contentHeight - flickable.height)
        flickable.contentY = Math.min(maxY, flickable.contentY + 40)
      }
      Keys.onPressed: function(event) {
        var maxY = Math.max(0, flickable.contentHeight - flickable.height)
        if (event.key === Qt.Key_PageUp) {
          flickable.contentY = Math.max(0, flickable.contentY - flickable.height * 0.8)
          event.accepted = true
        } else if (event.key === Qt.Key_PageDown) {
          flickable.contentY = Math.min(maxY, flickable.contentY + flickable.height * 0.8)
          event.accepted = true
        }
      }

      Flickable {
        id: flickable
        anchors.fill: parent
        contentHeight: column.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        clip: true

        ScrollBar.vertical: ScrollBar {
          policy: flickable.contentHeight > flickable.height
            ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        }

        Column {
          id: column
          width: parent.width
          spacing: Style.space(12)

        // ---------------------------------------------------------- hero
        PanelHero {
          width: parent.width
          iconComponent: Component {
            Text {
              text: root.glyph
              textFormat: Text.PlainText
              color: root.stateColor
              font.family: Style.font.family
              font.pixelSize: Style.font.displayLarge
            }
          }
          title: root.loaded
            ? (Model.isUnknown(root.state)
                ? "No memory reading"
                : Model.stateLabel(root.state) + " · " + root.percentText + " free")
            : "Measuring memory…"
          meta: root.loaded ? Model.heroMeta(root.snap, root.liveNow) : ""
          detail: root.busy ? "Reclaiming memory…"
            : (root.keeper && root.keeper.lastResult !== "" ? root.keeper.lastResult : "")
          trailingControl: Component {
            PanelActionButton {
              // U+F0432 — proven to render in JetBrainsMono Nerd Font.
              iconText: "󰐲"
              tooltipText: !root.actionsReady ? "Not connected"
                : (root.daemonUp ? "Automatic protection is on" : "Automatic protection is off")
              foreground: root.daemonUp ? root.foreground : root.urgentColor
              hoverColor: root.daemonUp ? root.foreground : root.urgentColor
              enabled: root.actionsReady
              onClicked: {
                if (!root.keeper) return
                root.daemonUp ? root.keeper.stopDaemon() : root.keeper.startDaemon()
              }
            }
          }
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        // ------------------------------------------------- what is using RAM
        PanelSectionHeader {
          width: parent.width
          text: "MEMORY"
          foreground: root.foreground
        }

        Text {
          visible: root.loaded && !Model.isUnknown(root.state)
            && root.snap.memory_breakdown_valid === false
          width: parent.width
          text: "Memory breakdown unavailable"
          textFormat: Text.PlainText
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.65)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }

        // The stacked bar: apps / cache / free.
        Item {
          visible: root.loaded && !Model.isUnknown(root.state)
            && root.snap.memory_breakdown_valid !== false
          width: parent.width
          height: Style.space(14)

          Rectangle {
            id: strip
            anchors.fill: parent
            radius: Style.cornerRadius
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)
            clip: true

            Row {
              anchors.fill: parent
              spacing: 0

              Repeater {
                model: root.loaded && !Model.isUnknown(root.state) ? Model.memoryPercentages(root.snap) : []

                Rectangle {
                  required property var modelData
                  width: (strip.width - 2) * modelData.fraction
                  height: strip.height
                  color: modelData.tone === "apps"
                    ? root.accentColor
                    : (modelData.tone === "cache"
                        ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.35)
                        : "transparent")
                }
              }
            }
          }
        }

        // Legend doubles as the exact numbers.
        Row {
          visible: root.loaded && !Model.isUnknown(root.state)
            && root.snap.memory_breakdown_valid !== false
          width: parent.width
          spacing: Style.space(12)

          Repeater {
            model: root.loaded && !Model.isUnknown(root.state) ? Model.memorySegments(root.snap) : []

            Row {
              required property var modelData
              spacing: Style.space(5)

              Rectangle {
                width: Style.space(7)
                height: Style.space(7)
                radius: 2
                anchors.verticalCenter: parent.verticalCenter
                color: modelData.tone === "apps"
                  ? root.accentColor
                  : (modelData.tone === "cache"
                      ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.5)
                      : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.2))
              }

              Text {
                text: modelData.label + " " + Model.formatBytes(modelData.bytes)
                textFormat: Text.PlainText
                color: root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                anchors.verticalCenter: parent.verticalCenter
              }
            }
          }
        }

        // -------------------------------------------------------- actions
        Row {
          width: parent.width
          spacing: Style.space(8)

          Button {
            width: (parent.width - Style.space(8)) / 2
            text: root.keeper && root.keeper.activeAction === "relieve" ? "Working…" : "Free memory now"
            enabled: root.actionsReady && !root.busy
            foreground: root.foreground
            accent: root.accentColor
            onClicked: if (root.keeper) root.keeper.relieve()
          }

          Button {
            width: (parent.width - Style.space(8)) / 2
            text: root.keeper && root.keeper.activeAction === "sweep" ? "Working…" : "Drop cache"
            enabled: root.actionsReady && !root.busy
            foreground: root.foreground
            accent: root.accentColor
            onClicked: if (root.keeper) root.keeper.sweep()
          }
        }

        // ---------------------------------------------- F2: snooze / pause
        Row {
          width: parent.width
          spacing: Style.space(8)
          visible: root.actionsReady

          Button {
            width: (parent.width - Style.space(8) * 2) / 3
            text: "Snooze 30m"
            enabled: root.actionsReady && !root.ctlBusy
            foreground: root.foreground
            accent: root.accentColor
            onClicked: if (root.keeper) root.keeper.snoozeDaemon(30)
          }

          Button {
            width: (parent.width - Style.space(8) * 2) / 3
            text: "Snooze 1h"
            enabled: root.actionsReady && !root.ctlBusy
            foreground: root.foreground
            accent: root.accentColor
            onClicked: if (root.keeper) root.keeper.snoozeDaemon(60)
          }

          Button {
            width: (parent.width - Style.space(8) * 2) / 3
            text: root.pausedOrSnoozed ? "Resume" : (root.daemonOff ? "Start" : "Pause")
            enabled: root.actionsReady && !root.ctlBusy
            foreground: root.daemonOff ? root.accentColor : root.foreground
            accent: root.accentColor
            onClicked: {
              if (!root.keeper) return
              if (root.daemonOff) root.keeper.startDaemon()
              else root.keeper.pauseDaemon()
            }
          }
        }

        Text {
          width: parent.width
          visible: root.pausedOrSnoozed
          text: {
            var ps = Model.parsePauseState(root.snap, root.liveNow)
            if (ps.paused) return ps.label
            if (root.keeper && root.keeper.snoozeRemaining > 0) {
              return "Snoozed · resumes in " + Model.horizonLabel(root.keeper.snoozeRemaining)
            }
            return "Automatic protection is paused"
          }
          textFormat: Text.PlainText
          color: Qt.darker(root.foreground, 1.35)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        // ------------------------------------------------------- pressure
        PanelSectionHeader {
          width: parent.width
          text: "PRESSURE"
          foreground: root.foreground
        }

        Item {
          width: parent.width
          height: Style.font.body * 1.7
          visible: root.loaded && root.forecast !== null

          readonly property color fcColor: {
            if (!root.forecast) return root.foreground
            if (root.forecast.isWarning && root.forecast.urgency === "imminent") return root.urgentColor
            return root.forecast.isWarning
              ? Qt.darker(root.accentColor, 1.2) : Qt.darker(root.foreground, 1.35)
          }

          Text {
            id: forecastTitle
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Forecast"
            textFormat: Text.PlainText
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            anchors.left: forecastTitle.right
            anchors.leftMargin: Style.space(8)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            horizontalAlignment: Text.AlignRight
            text: root.forecast ? root.forecast.text : ""
            textFormat: Text.PlainText
            color: parent.fcColor
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
        }

        Item {
          width: parent.width
          height: Style.font.body * 1.7

          readonly property var psi: root.loaded ? Model.psiRow(root.snap.psi) : ({ stallPercent: 0, severity: "ok" })
          readonly property color psiColor: psi.severity === "critical" ? root.urgentColor
            : (psi.severity === "warning" || psi.severity === "notice"
                ? Qt.darker(root.accentColor, 1.2) : root.foreground)

          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Memory stall"
            textFormat: Text.PlainText
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: root.loaded
              ? Model.psiLabel(parent.psi) + (parent.psi.stallPercent > 0 ? " of the last minute" : "")
              : "—"
            textFormat: Text.PlainText
            color: parent.psiColor
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }

        // ---------------------------------------------------------- swap
        Repeater {
          model: root.loaded && !Model.isUnknown(root.state) ? Model.swapRows(root.snap) : []

          Item {
            required property var modelData
            width: column.width
            height: Style.font.body * 2.2

            Item {
              id: swapHeaderRow
              anchors.top: parent.top
              width: parent.width
              height: Style.font.body * 1.4

              Text {
                anchors.left: parent.left
                anchors.right: swapSizeText.left
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                text: modelData.label
                textFormat: Text.PlainText
                color: modelData.isConcern ? root.urgentColor : root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }

              Text {
                id: swapSizeText
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: Model.formatBytes(modelData.bytes) + " / " + Model.formatBytes(modelData.size)
                textFormat: Text.PlainText
                color: modelData.isConcern ? root.urgentColor
                  : (modelData.isZram ? Qt.darker(root.foreground, 1.4) : root.foreground)
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
            }

            // F3: swap mini-bar contrasting fast zram vs slow disk swap.
            Rectangle {
              id: swapBarBg
              anchors.top: swapHeaderRow.bottom
              anchors.topMargin: Style.space(2)
              width: parent.width
              height: Style.space(4)
              radius: 2
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)
              clip: true

              Rectangle {
                width: swapBarBg.width * Math.max(0, Math.min(1, modelData.fraction))
                height: swapBarBg.height
                radius: 2
                color: modelData.isConcern
                  ? root.urgentColor
                  : (modelData.isZram
                      ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.35)
                      : root.accentColor)
              }
            }
          }
        }

        Text {
          width: parent.width
          visible: root.loaded && !Model.isUnknown(root.state)
            && Model.swapRows(root.snap).length > 0
          text: "Swap on zram stays in RAM — it costs CPU, not speed. Only disk swap is slow."
          textFormat: Text.PlainText
          color: Qt.darker(root.foreground, 1.55)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        // ------------------------------------------------------ processes
        PanelSectionHeader {
          width: parent.width
          text: "AVAILABLE TO RECLAIM"
          foreground: root.foreground
        }

        Repeater {
          model: root.loaded && !Model.isUnknown(root.state) ? Model.reclaimableGroups(root.snap, 8) : []

          Item {
            required property var modelData
            width: column.width
            height: Style.font.body * 2.5

            readonly property bool isProtecting: !!(root.keeper
              && root.keeper.pendingProtectKey === modelData.key
              && root.keeper.listVerb === "protect")

            Column {
              anchors.left: parent.left
              anchors.right: protectBtn.visible ? protectBtn.left : reclaimSize.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              spacing: 1

              Text {
                width: parent.width
                text: modelData.label
                textFormat: Text.PlainText
                color: root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: modelData.subLabel || ("can free " + (modelData.giveLabel || ""))
                textFormat: Text.PlainText
                color: modelData.thrashing
                  ? root.urgentColor
                  : Qt.darker(root.foreground, 1.5)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            Text {
              id: protectBtn
              anchors.right: reclaimSize.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              visible: modelData.action === "protect"
              text: parent.isProtecting ? "Protecting…" : "Protect"
              textFormat: Text.PlainText
              color: parent.isProtecting
                ? Qt.darker(root.accentColor, 1.3) : root.accentColor
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              MouseArea {
                anchors.fill: parent
                anchors.margins: -Style.space(5)
                cursorShape: Qt.PointingHandCursor
                enabled: root.actionsReady
                  && !(root.keeper && root.keeper.pendingProtectKey !== "")
                onClicked: if (root.keeper) root.keeper.protect(modelData.key)
              }
            }

            Text {
              id: reclaimSize
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: Model.formatBytes(modelData.bytes)
              textFormat: Text.PlainText
              color: Qt.darker(root.foreground, 1.3)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        Text {
          width: parent.width
          visible: root.loaded && !Model.isUnknown(root.state)
            && Model.reclaimableGroups(root.snap, 8).length === 0
          text: "Nothing above the noise floor — every app is small right now."
          textFormat: Text.PlainText
          color: Qt.darker(root.foreground, 1.55)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        // ------------------------------------------------- protected list
        PanelSectionHeader {
          width: parent.width
          visible: root.loaded && !Model.isUnknown(root.state)
            && Model.protectedGroups(root.snap, 4).length > 0
          text: "NEVER RECLAIMED"
          foreground: root.foreground
        }

        Repeater {
          model: root.loaded && !Model.isUnknown(root.state) ? Model.protectedGroups(root.snap, 4) : []

          Item {
            required property var modelData
            width: column.width
            height: Style.font.body * 1.8

            readonly property bool isReleasing: !!(root.keeper
              && root.keeper.pendingProtectKey === modelData.key
              && root.keeper.listVerb === "release")

            Text {
              anchors.left: parent.left
              anchors.right: releaseBtn.visible ? releaseBtn.left : sizeText.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.label
              textFormat: Text.PlainText
              color: Qt.darker(root.foreground, 1.5)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            Text {
              id: releaseBtn
              anchors.right: sizeText.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              visible: modelData.action === "release"
              text: parent.isReleasing ? "Releasing…" : "Release"
              textFormat: Text.PlainText
              color: parent.isReleasing
                ? Qt.darker(root.accentColor, 1.3) : root.accentColor
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              MouseArea {
                anchors.fill: parent
                anchors.margins: -Style.space(5)
                cursorShape: Qt.PointingHandCursor
                enabled: root.actionsReady
                  && !(root.keeper && root.keeper.pendingProtectKey !== "")
                onClicked: if (root.keeper) root.keeper.unprotect(modelData.key)
              }
            }

            Text {
              id: sizeText
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: Model.formatBytes(modelData.bytes)
              textFormat: Text.PlainText
              color: Qt.darker(root.foreground, 1.65)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }

        // ---------------------------------------------- F3: session impact
        Column {
          width: parent.width
          spacing: Style.space(8)
          visible: root.sessionImpact !== null

          PanelSeparator { width: parent.width; foreground: root.foreground }

          Text {
            width: parent.width
            text: root.sessionImpact ? root.sessionImpact.text : ""
            textFormat: Text.PlainText
            color: Qt.darker(root.foreground, 1.35)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
          }
        }

        // ---------------------------------------------------------- footer
        PanelSeparator { width: parent.width; visible: root.loaded; foreground: root.foreground }

        Row {
          width: parent.width
          visible: root.loaded
          spacing: Style.space(8)

          Text {
            width: parent.width
            text: Model.watchLine(root.snap, root.daemonUp, root.liveNow)
            textFormat: Text.PlainText
            color: root.daemonUp ? Qt.darker(root.foreground, 1.5) : root.urgentColor
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }

        Text {
          width: parent.width
          visible: root.keeper !== null && root.keeper.lastError !== ""
          text: root.keeper ? root.keeper.lastError : ""
          textFormat: Text.PlainText
          color: root.urgentColor
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
        }
      }
    }
  }
}
