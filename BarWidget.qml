import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Gliff Door: a switch per machine for remote-desktopping into this PC with
// Gliff. door.py does the work: switching on puts the machine's key in
// ~/.ssh/authorized_keys, pinned to its tailnet address and able to start
// gliff-server only; switching off takes it out and ends its session.
BarWidget {
  id: root
  moduleName: "io.github.yassinesawahi.gliff-door"

  readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("door.py").toString().replace(/^file:\/\//, ""))
  // The same path with the home folder shortened, for commands shown to type.
  readonly property string addCommand: "python3 " + root.scriptPath.replace(Quickshell.env("HOME"), "~") + " add <name> <tailscale-name>"

  readonly property var glyphs: ({
    idle: String.fromCodePoint(0xF0318),     // nf-md-lan_connect
    watched: String.fromCodePoint(0xF13B4),  // nf-md-monitor_eye
    close: String.fromCodePoint(0xF0156),    // nf-md-close
    copy: String.fromCodePoint(0xF018F),     // nf-md-content_copy
    alert: String.fromCodePoint(0xF0026)     // nf-md-alert
  })

  // Latest `door.py status` reading; null until the first one lands.
  property var status: null
  property bool popupOpen: false
  property bool refreshQueued: false

  // Peer name → "on" or "off" while a click waits for door.py, so the switch
  // flips straight away.
  property var peerPending: ({})

  // The last action's answer, shown under the switches for a while.
  property string message: ""
  property bool messageOk: true

  readonly property var peers: root.status ? root.status.peers || [] : []
  readonly property var sessions: root.status ? root.status.sessions || [] : []
  readonly property bool watched: root.sessions.length > 0
  readonly property bool sshdUp: !!root.status && root.status.sshd === "active"
  readonly property var downloadsKey: root.status ? root.status.downloadsKey : null
  readonly property var keylessPeers: root.peers.filter(function(p) { return !p.hasKey })
  readonly property string hostAddress: root.status ? (root.status.host || "this-pc") : ""
  readonly property string connectCommand: root.status ? "gliff " + root.status.user + "@" + root.hostAddress : ""
  readonly property string keyCommand: root.status ? "omarchy tailscale send " + root.hostAddress + " ~/.ssh/id_ed25519.pub" : ""

  function peerAllowed(p) {
    var pend = root.peerPending[p.name]
    return pend !== undefined ? pend === "on" : !!p.allowed
  }

  readonly property var openPeers: root.peers.filter(function(p) { return root.peerAllowed(p) })

  function findPeer(name) {
    var want = String(name).toLowerCase()
    for (var i = 0; i < root.peers.length; i++)
      if (root.peers[i].name.toLowerCase() === want) return root.peers[i]
    return null
  }

  function peerLine(p) {
    var pend = root.peerPending[p.name]
    if (pend === "on") return "Letting in…"
    if (pend === "off") return p.sessions > 0 ? "Ending the session…" : "Shutting out…"
    if (!p.hasKey) return "Needs its SSH key first, see below"
    if (p.sessions > 0) return "On your screen now · switch off to end it"
    if (p.allowed) return "Can connect · " + (p.online ? "online" : "offline")
    return "Can't connect"
  }

  function sessionName(s) {
    return s.peer || s.address || "Someone"
  }

  function sessionLine(s) {
    var since = new Date(s.since * 1000).toLocaleTimeString(Qt.locale(Commons.I18n.formatLocale), Locale.ShortFormat)
    return (s.peer ? "" : "not through this switch · ") + s.mode + " · since " + since
  }

  readonly property string watcherNames: root.sessions.map(root.sessionName).join(", ")

  readonly property string tooltipLabel: {
    if (!root.status) return "Gliff Door"
    var s = root.watched ? root.watcherNames + " on your screen (Gliff)"
      : root.openPeers.length ? "Gliff open to " + root.openPeers.map(function(p) { return p.name }).join(", ")
      : "Gliff Door closed"
    var first = root.peers.length ? root.peers[0] : null
    if (first && first.hasKey)
      s += "\nMiddle-click to " + (root.peerAllowed(first) ? "shut " + first.name + " out" : "let " + first.name + " in")
    return s
  }

  function refresh() {
    if (readProc.running) root.refreshQueued = true
    else readProc.running = true
  }

  function applyReading(text) {
    var data
    try { data = JSON.parse(text) } catch (e) { return }
    root.status = data
    if (!actionProc.running) root.peerPending = ({})
  }

  function applyAnswer(text) {
    var a
    try { a = JSON.parse(text) } catch (e) { a = { ok: false, message: "door.py gave no answer" } }
    root.messageOk = !!a.ok
    root.message = a.message || ""
    messageTimer.restart()
  }

  // Runs `door.py <args…>`, shows its answer, and re-reads. One action at a
  // time; clicks while one is in flight are dropped.
  function run(args) {
    if (actionProc.running) return false
    actionProc.command = ["python3", "-I", root.scriptPath].concat(args)
    actionProc.running = true
    return true
  }

  function setPeer(name, on) {
    if (actionProc.running) return
    var p = Object.assign({}, root.peerPending)
    p[name] = on ? "on" : "off"
    root.peerPending = p
    root.run(["allow", name, on ? "on" : "off"])
  }

  function togglePeer(name) {
    var p = root.findPeer(name)
    if (p && p.hasKey) root.setPeer(p.name, !root.peerAllowed(p))
  }

  // door.py builds the command ("connect", "key" or "add") and hands it to
  // wl-copy on stdin, so nothing copied appears on a command line.
  function copy(what) {
    root.run(["copy", what])
  }

  // Contract KeyboardPanel expects on its `owner`.
  function close() {
    root.popupOpen = false
  }

  function toggle() {
    root.popupOpen = !root.popupOpen
    if (root.popupOpen) root.refresh()
  }

  // The floating bar can mount a hidden second copy of a widget; only the
  // copy on screen takes IPC, so shortcuts open the panel you can see.
  readonly property bool onScreen: {
    var w = button.QsWindow.window
    return !!w && w.visible && root.visible && root.width > 0 && root.height > 0
  }

  // `omarchy-shell io.github.yassinesawahi.gliff-door <fn>` — bindable to keys.
  IpcHandler {
    target: "io.github.yassinesawahi.gliff-door"
    enabled: root.onScreen

    function toggle() { root.toggle() }
    function close() { root.close() }
    function refresh() { root.refresh() }
    // `omarchy-shell io.github.yassinesawahi.gliff-door togglePeer laptop`
    function togglePeer(name: string): void { root.togglePeer(name) }
    function allow(name: string): void { var p = root.findPeer(name); if (p) root.setPeer(p.name, true) }
    function deny(name: string): void { var p = root.findPeer(name); if (p) root.setPeer(p.name, false) }
  }

  Process {
    id: readProc
    command: ["python3", "-I", root.scriptPath, "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyReading(text)
    }
    onExited: {
      if (!root.refreshQueued) return
      root.refreshQueued = false
      Qt.callLater(root.refresh)
    }
  }

  Process {
    id: actionProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyAnswer(text)
    }
    onExited: root.refresh()
  }

  Timer {
    id: messageTimer
    interval: 8000
    onTriggered: root.message = ""
  }

  // gliff-server captures through ext-image-copy-capture, so a session
  // starting shows up as a Hyprland screencast event. Same cue the built-in
  // remote-session indicator uses; the debounce folds a burst into one read.
  Connections {
    target: Hyprland
    function onRawEvent(event) {
      if (event && event.name === "screencast") eventDebounce.restart()
    }
  }

  Timer {
    id: eventDebounce
    interval: 300
    onTriggered: root.refresh()
  }

  // Catches sessions ending (no event for that) and changes made from a
  // terminal.
  Timer {
    interval: root.popupOpen || root.watched ? 3000 : 20000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    tooltipText: root.tooltipLabel
    fixedWidth: root.vertical ? -1 : glyph.width + button.scaledHorizontalMargin * 2
    fixedHeight: root.vertical ? Style.bar.iconSlot : -1

    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else if (b === Qt.MiddleButton) { if (root.peers.length) root.togglePeer(root.peers[0].name) }
      else root.toggle()
    }

    OpticalGlyph {
      id: glyph
      anchors.centerIn: parent
      width: Style.bar.iconCanvas
      height: Style.bar.iconCanvas
      text: root.watched ? root.glyphs.watched : root.glyphs.idle
      fontFamily: button.fontFamily
      fontSize: Style.bar.iconFont
      // Urgent while someone is on the screen, accent while the door is open.
      color: root.watched ? root.bar.urgent
        : root.openPeers.length ? Commons.Color.accent
        : button.foreground
      opacity: root.watched || root.openPeers.length ? 1 : 0.5
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.popupOpen
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(340))
    contentHeight: popup.fittedContentHeight(col.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: col
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        readonly property color dim: Qt.darker(root.bar.foreground, 1.4)

        PanelSectionHeader {
          text: "REMOTE DESKTOP · GLIFF"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
        }

        Text {
          visible: !root.status
          text: "Reading…"
          color: col.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }

        // One switch per machine. Indexed rather than bound to the array, so
        // a refresh doesn't rebuild the rows under the pointer.
        Repeater {
          model: root.peers.length

          delegate: Toggle {
            required property int index
            readonly property var peer: root.peers[index] || ({ name: "", hasKey: false, allowed: false, sessions: 0, online: false })

            width: col.width
            enabled: peer.hasKey || peer.allowed
            opacity: enabled ? 1 : 0.6
            label: peer.name
            description: root.peerLine(peer)
            checked: root.peerAllowed(peer)
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            titleSize: Style.font.bodySmall
            onClicked: root.setPeer(peer.name, !root.peerAllowed(peer))
          }
        }

        // No machines yet: adding one is a terminal job, since it takes a name
        // and the machine's Tailscale name or address.
        Column {
          visible: !!root.status && root.peers.length === 0
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width
            text: "No machines yet. Add one that runs Hyprland and Gliff, by its Tailscale name or IP:"
            textFormat: Text.PlainText
            color: col.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Item {
            width: parent.width
            height: Math.max(addCmd.implicitHeight, addCopy.height)

            Text {
              id: addCmd
              anchors.left: parent.left
              anchors.right: addCopy.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              text: root.addCommand
              textFormat: Text.PlainText
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.features: ({ "liga": 0, "calt": 0 })
              wrapMode: Text.WrapAnywhere
            }

            PanelActionButton {
              id: addCopy
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: root.glyphs.copy
              tooltipText: "Copy"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.copy("add")
            }
          }
        }

        Text {
          visible: root.message !== ""
          width: parent.width
          text: root.message
          textFormat: Text.PlainText
          color: root.messageOk ? col.dim : root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Text {
          visible: !!root.status && !root.sshdUp
          width: parent.width
          text: root.glyphs.alert + "  The SSH server is off, so nobody can connect. Turn it on with: omarchy setup security sshd"
          textFormat: Text.PlainText
          color: root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Text {
          visible: !!root.status && !root.status.gliff
          width: parent.width
          text: root.glyphs.alert + "  gliff-server isn't installed here: omarchy pkg add gliff"
          textFormat: Text.PlainText
          color: root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        // First-time setup: the machine's public key has to come over once.
        Column {
          visible: root.keylessPeers.length > 0
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader {
            text: "FIRST, " + (root.keylessPeers.length ? root.keylessPeers[0].name.toUpperCase() : "") + "'S KEY"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

          Text {
            width: parent.width
            text: "On " + (root.keylessPeers.length ? root.keylessPeers[0].name : "it") + ", send its public key here with Taildrop (run ssh-keygen -t ed25519 first if it has none):"
            textFormat: Text.PlainText
            color: col.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Item {
            width: parent.width
            height: Math.max(keyCmd.implicitHeight, keyCopy.height)

            Text {
              id: keyCmd
              anchors.left: parent.left
              anchors.right: keyCopy.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              text: root.keyCommand
              textFormat: Text.PlainText
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              // Commands get typed on the other machine: show `--` as two
              // hyphens, not the font's joined ligature.
              font.features: ({ "liga": 0, "calt": 0 })
              wrapMode: Text.WrapAnywhere
            }

            PanelActionButton {
              id: keyCopy
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: root.glyphs.copy
              tooltipText: "Copy"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.copy("key")
            }
          }

          Text {
            width: parent.width
            text: root.downloadsKey
              ? "Found " + root.downloadsKey.file + (root.downloadsKey.comment ? " (" + root.downloadsKey.comment + ")" : "") + " in Downloads. Use it only if it came from " + (root.keylessPeers.length ? root.keylessPeers[0].name : "that machine") + "."
              : "It lands in ~/Downloads; an Import button shows up here when it does."
            textFormat: Text.PlainText
            color: col.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Button {
            visible: !!root.downloadsKey && root.keylessPeers.length > 0
            text: "Import as " + (root.keylessPeers.length ? root.keylessPeers[0].name : "") + "'s key"
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.run(["key", root.keylessPeers[0].name, "--downloads"])
          }
        }

        PanelSectionHeader {
          visible: root.watched
          text: "ON YOUR SCREEN NOW"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
        }

        Repeater {
          model: root.sessions

          delegate: Item {
            id: sessionRow
            required property var modelData

            width: col.width
            height: Math.max(sessionText.implicitHeight, dropBtn.height)

            Column {
              id: sessionText
              anchors.left: parent.left
              anchors.right: dropBtn.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: root.sessionName(sessionRow.modelData)
                textFormat: Text.PlainText
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: root.sessionLine(sessionRow.modelData)
                textFormat: Text.PlainText
                color: col.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            PanelActionButton {
              id: dropBtn
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: root.glyphs.close
              tooltipText: "Disconnect " + root.sessionName(sessionRow.modelData) + (sessionRow.modelData.peer ? " (Gliff reconnects while the switch is on)" : "")
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.run(["kick", String(sessionRow.modelData.pid)])
            }
          }
        }

        PanelSeparator {
          foreground: root.bar.foreground
        }

        Text {
          width: parent.width
          text: "On " + (root.peers.length ? root.peers[0].name : "the other machine") + ", connect with the command below. Add --headless for a screen of its own instead of mirroring this one. Shift+Esc gives its own shortcuts back."
          textFormat: Text.PlainText
          color: col.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          font.features: ({ "liga": 0, "calt": 0 })
          wrapMode: Text.WordWrap
        }

        Item {
          visible: !!root.status
          width: parent.width
          height: Math.max(connectCmd.implicitHeight, connectCopy.height)

          Text {
            id: connectCmd
            anchors.left: parent.left
            anchors.right: connectCopy.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: root.connectCommand
            textFormat: Text.PlainText
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            font.features: ({ "liga": 0, "calt": 0 })
            wrapMode: Text.WrapAnywhere
          }

          PanelActionButton {
            id: connectCopy
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: root.glyphs.copy
            tooltipText: "Copy"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            onClicked: root.copy("connect")
          }
        }
      }
    }
  }
}
