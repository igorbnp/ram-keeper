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
  // daemon verbs (status/refresh/relieve/sweep) in ONE place.
  //
  // Why not a handler here: IpcHandler allows one registration per target, and
  // a second one silently loses every function it declares. An earlier revision
  // declared the panel verbs here as well, and the result was that
  // `omarchy-shell ram-keeper toggle` answered "Function not found" — the
  // panel could not be opened by IPC or by any keyboard-summon path.
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
  readonly property var forecast: root.loaded
    ? Model.forecastRow(root.snap.forecast) : null

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
  readonly property string percentText: loaded ? Math.round(snap.free_percent || 0) + "%" : "--"

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
    text: root.showPercent && !root.verticalBar
      ? root.percentText + " " + root.glyph
      : root.glyph
    tooltipText: root.loaded
      ? Math.round(snap.free_percent || 0) + "% memory free — " + Model.stateLabel(root.state)
      : "Measuring memory…"
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

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        // ---------------------------------------------------------- hero
        PanelHero {
          width: parent.width
          iconComponent: Component {
            Text {
              text: root.glyph
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
          meta: root.loaded
            ? (Model.isUnknown(root.state)
                ? "The daemon could not read memory"
                : Model.formatBytes(snap.mem_available) + " available of "
                  + Model.formatBytes(snap.mem_total))
            : ""
          detail: root.busy ? "Reclaiming memory…"
            : (root.keeper && root.keeper.lastResult !== "" ? root.keeper.lastResult : "")
          trailingControl: Component {
            PanelActionButton {
              // U+E2DB (Material Symbols "shield") has NO glyph on this machine:
              // the only icon font installed is JetBrainsMono Nerd Font, whose
              // Material range starts at U+F0xxx, so Qt drew a tofu box. Use
              // U+F0432 — the power glyph the Omarchy shell itself puts in a
              // PanelActionButton, proven to render in this exact setup.
              iconText: "󰐲"
              tooltipText: !root.actionsReady ? "Not connected"
                : (root.daemonUp ? "Automatic protection is on" : "Automatic protection is off")
              foreground: root.foreground
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

        // The stacked bar: apps / cache / free. Reading proportions off one
        // strip is far faster than reading three numbers.
        Item {
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
            text: root.busy ? "Working…" : "Free memory now"
            // `keeper` is null until the service loads. `!root.busy` alone is
            // then true, which INVERTS the guard and leaves an enabled button
            // whose handler throws on a null object.
            enabled: root.actionsReady && !root.busy
            foreground: root.foreground
            accent: root.accentColor
            onClicked: if (root.keeper) root.keeper.relieve()
          }

          Button {
            width: (parent.width - Style.space(8)) / 2
            text: "Drop cache"
            enabled: root.actionsReady && !root.busy
            foreground: root.foreground
            accent: root.accentColor
            onClicked: if (root.keeper) root.keeper.sweep()
          }
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        // ------------------------------------------------------- pressure
        PanelSectionHeader {
          width: parent.width
          text: "PRESSURE"
          foreground: root.foreground
        }

        // The forecast: where memory is heading. This is the one row that
        // can say something before any number looks wrong.
        Item {
          width: parent.width
          height: Style.font.body * 1.7
          visible: root.loaded && root.forecast !== null

          readonly property color fcColor: {
            if (!root.forecast) return root.foreground
            if (root.forecast.urgency === "imminent") return root.urgentColor
            return root.forecast.isWarning
              ? Qt.darker(root.accentColor, 1.2) : Qt.darker(root.foreground, 1.35)
          }

          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Forecast"
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width * 0.68
            horizontalAlignment: Text.AlignRight
            text: root.forecast ? root.forecast.text : ""
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
            height: Style.font.body * 1.7

            Text {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.label
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            Text {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: Model.formatBytes(modelData.bytes) + " / " + Model.formatBytes(modelData.size)
              color: modelData.isConcern ? root.urgentColor
                : (modelData.isZram ? Qt.darker(root.foreground, 1.4) : root.foreground)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        Text {
          width: parent.width
          visible: root.loaded && Model.swapRows(root.snap).length > 0
          text: "Swap on zram stays in RAM — it costs CPU, not speed. Only disk swap is slow."
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
            height: Style.font.body * 1.8

            Text {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width * 0.55
              text: modelData.label
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            Text {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: Model.formatBytes(modelData.bytes)
              color: Qt.darker(root.foreground, 1.3)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        Text {
          width: parent.width
          visible: root.loaded && Model.reclaimableGroups(root.snap, 8).length === 0
          text: "Nothing above the noise floor — every app is small right now."
          color: Qt.darker(root.foreground, 1.55)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        // ------------------------------------------------- protected list
        PanelSectionHeader {
          width: parent.width
          visible: root.loaded && Model.protectedGroups(root.snap, 4).length > 0
          text: "NEVER RECLAIMED"
          foreground: root.foreground
        }

        Repeater {
          model: root.loaded && !Model.isUnknown(root.state) ? Model.protectedGroups(root.snap, 4) : []

          Item {
            required property var modelData
            width: column.width
            height: Style.font.body * 1.7

            Text {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width * 0.55
              text: modelData.label
              color: Qt.darker(root.foreground, 1.5)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            Text {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: Model.formatBytes(modelData.bytes)
              color: Qt.darker(root.foreground, 1.65)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
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
            text: root.daemonUp
              ? "Watching automatically · nothing needed from you"
              : "Automatic protection is paused"
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
          color: root.urgentColor
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }
    }
  }
}