import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root
  property string omarchyPath: ""
  property var shell: null
  property var manifest: null

  property var snapshot: ({ online: false, messages: [], unread: 0 })
  property var detail: null
  property var preview: null
  property string sendState: ""
  // Counts from the last confirmed storage cleanup, until the panel closes.
  property var cleaned: null
  property string error: ""
  property bool busy: false
  property bool discardSensitiveReply: false
  property string pendingAction: ""
  property bool stopping: false

  // Follows dev-dji4g.device, the unit 71-dji4g-hotplug.rules gives the
  // module: while it is unplugged the engine does not run and the bar widget
  // hides. systemd is the only source of truth; dji4g-sms-panel.service's IPC
  // call, the engine's snapshots and the timers below merely ask for another look.
  property bool deviceUp: false
  // An unresolved send keeps the widget on screen after the module is gone.
  readonly property bool present: deviceUp || sendState === "sending" || sendState === "unknown"

  function syncDevice() {
    if (probe.running) probe.again = true
    else probe.running = true
  }

  // A device unit is "active" while plugged; "activating" (tentative) is not
  // a udev device yet.
  function applyDevice(state) {
    var up = String(state).trim() === "active"
    if (up === deviceUp) return
    deviceUp = up
    worker.running = up
    if (up) error = ""
    else forget()
  }

  // Nothing read from the module outlives it.
  function forget() {
    snapshot = ({ online: false, messages: [], unread: 0 })
    cleaned = null
    error = ""
  }

  function request(value) {
    if (busy) return false
    if (!worker.running) {
      error = deviceUp ? "本地短信引擎未运行" : "模块未插入"
      return false
    }
    busy = true
    pendingAction = value.action
    error = ""
    worker.write(JSON.stringify(value) + "\n")
    return true
  }

  function refresh() { request({ action: "refresh" }) }
  function showMessage(id) {
    detail = null
    return request({ action: "show", id: id })
  }
  function markAllRead() { request({ action: "mark_all_read" }) }
  function acknowledge() { request({ action: "acknowledge" }) }
  function cleanup() {
    cleaned = null
    request({ action: "cleanup", confirmed: true })
  }
  function deleteMessage(id) { request({ action: "delete", id: id, confirmed: true }) }
  function prepare(recipient, body) {
    preview = null
    sendState = ""
    request({ action: "preview", recipient: recipient, body: body })
  }
  function submit() {
    if (busy || !preview) return
    var key = preview.key
    if (request({ action: "send", key: key, confirmed: true })) {
      preview = null
      sendState = "sending"
    }
  }
  function clearSensitive() {
    detail = null
    preview = null
    if (busy && (pendingAction === "preview" || pendingAction === "show")) discardSensitiveReply = true
    if (!busy) request({ action: "cancel" })
  }
  // SMS text reaches wl-copy over stdin, never argv; --sensitive keeps it out
  // of clipboard history.
  function copy(text) {
    if (!text) return
    clipboard.running = false
    clipboard.pending = text
    clipboard.stdinEnabled = true
    clipboard.running = true
  }

  function receive(line) {
    var value
    try { value = JSON.parse(line) } catch (e) { return }
    if (value.response) {
      var discard = discardSensitiveReply
      busy = false
      pendingAction = ""
      discardSensitiveReply = false
      if (discard && (value.type === "preview" || value.type === "detail")) return
    }
    if (value.type === "snapshot") {
      snapshot = value.online ? value : Object.assign({}, snapshot, { online: false, ready: false, error: value.error })
      if (value.response && value.action === "cleanup") cleaned = value.cleaned || null
      // An engine that lost the module may mean it was unplugged.
      if (!value.online || !value.connected) syncDevice()
    } else if (value.type === "detail") {
      detail = value.message
      var messages = (snapshot.messages || []).map(function(message) {
        return message.id === value.message.id ? Object.assign({}, message, { unread: false }) : message
      })
      snapshot = Object.assign({}, snapshot, { unread: value.unread, messages: messages })
    } else if (value.type === "preview") {
      preview = value
    } else if (value.type === "sent") {
      preview = null
      sendState = value.state
    } else if (value.type === "error") {
      error = value.error
      if (value.action === "send") sendState = ""
    }
  }

  Process {
    id: worker
    // The shell string is constant; all private UI input travels over stdin.
    command: ["sh", "-c", "exec \"$HOME/.local/lib/dji4g/.venv/bin/python\" -B -u \"$HOME/.local/lib/dji4g/dji4g_desktop.py\""]
    stdinEnabled: true
    onStarted: root.error = ""
    stdout: SplitParser { onRead: function(line) { root.receive(line) } }
    onExited: function(exitCode) {
      if (root.pendingAction === "send") root.sendState = "unknown"
      root.busy = false
      root.pendingAction = ""
      root.discardSensitiveReply = false
      root.preview = null
      root.detail = null
      root.snapshot = Object.assign({}, root.snapshot, { online: false, ready: false })
      // 75: another engine or the CLI owns the modem; its snapshot already said so.
      root.error = exitCode === 75 ? "" : "本地短信引擎已停止，正在重连；不会重试发送"
      // Stopped on purpose once the module is gone; a late line may have
      // refilled the snapshot since.
      if (!root.deviceUp) root.forget()
    }
  }

  Process {
    id: probe
    property bool again: false
    command: ["systemctl", "--user", "is-active", "dev-dji4g.device"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyDevice(text)
    }
    // A request that arrived mid-probe may be newer than what it reported.
    onExited: if (again) { again = false; Qt.callLater(root.syncDevice) }
  }

  Process {
    id: clipboard
    property string pending: ""
    command: ["wl-copy", "--sensitive"]
    stdinEnabled: true
    onStarted: {
      write(pending)
      pending = ""
      stdinEnabled = false
    }
  }

  Timer {
    interval: 5000
    repeat: true
    running: !root.stopping && root.deviceUp && !worker.running
    onTriggered: worker.running = true
  }
  // The unit's call can be missed (shell busy or restarting, unit not
  // installed), so a slow poll still finds a module that was plugged in.
  Timer {
    interval: 30000
    repeat: true
    running: !root.stopping && !root.deviceUp
    onTriggered: root.syncDevice()
  }

  // dji4g-sms-panel.service calls `sync` when the module is plugged in and
  // again when it is unplugged.
  IpcHandler {
    target: "dji4g.sms.service"

    function sync(): void { root.syncDevice() }
    function device(): string { return root.deviceUp ? "up" : "down" }
  }

  Component.onCompleted: syncDevice()
  Component.onDestruction: { stopping = true; worker.running = false }
}
