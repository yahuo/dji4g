import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: sms
  moduleName: "dji4g.sms"
  ipcTarget: "dji4g.sms"
  manageIpc: false

  property var service: null
  property string mode: "inbox"
  property bool cursorActive: false
  property int selectedIndex: 0
  property int expandedId: -1
  property bool wantShow: false
  property bool wantLatest: false
  property bool revealFailed: false
  property bool primed: false
  property string copied: ""
  property real now: Date.now()
  property int phraseIndex: 0
  property int seenUnread: 0

  readonly property var snap: service ? service.snapshot : ({ online: false, messages: [], unread: 0 })
  readonly property bool busy: !service || service.busy
  readonly property var detail: service && service.detail && service.detail.id === expandedId ? service.detail : null
  readonly property var pending: service ? service.preview : null
  readonly property string sendState: service ? service.sendState : ""
  readonly property int unread: snap.unread || 0
  readonly property bool linkUp: !!(snap.online && snap.connected && snap.sim_ready && snap.registered)
  // False while the module is unplugged: the widget hides and cannot be summoned.
  readonly property bool available: !!service && service.present

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string statusText: !service ? "正在启动"
    : !service.deviceUp ? "模块未插入"
    : !snap.online ? (snap.error || service.error !== "" ? "短信引擎未运行" : "正在连接")
    : !snap.connected ? "模块未连接"
    : !snap.sim_ready ? "SIM 未就绪"
    : !snap.registered ? "等待网络注册" : "在线"
  readonly property var facts: linkFacts()
  readonly property string heroMeta: linkUp && facts.length > 0 ? facts[phraseIndex % facts.length] : statusText
  readonly property var notices: currentNotices()
  readonly property real listCap: Math.max(Style.space(160), Math.min(Style.space(420), panel.availableCardHeight - Style.space(200)))

  readonly property string recipientText: recipientField.text.trim()
  readonly property bool recipientValid: /^\+?[0-9]{3,15}$/.test(recipientText)
  readonly property var draft: estimate(bodyEdit.text)
  readonly property bool composeProblem: draft.segments > 10 || (recipientText !== "" && !recipientValid)
  readonly property bool canPreview: !!service && !!snap.online && !busy && sendState !== "sending"
    && recipientValid && draft.segments >= 1 && draft.segments <= 10
  readonly property string composeHint: draft.segments > 10 ? "超过 10 段上限（当前 " + draft.segments + " 段）"
    : recipientText !== "" && !recipientValid ? "号码需为 3–15 位数字，可带 +"
    : draft.units === 0 ? "预览不会发送短信"
    : draft.encoding + " · " + draft.units + "/" + draft.capacity + " · " + draft.segments + " 段"

  // Mirrors dji4g_sms.py so the counter matches what the engine will send.
  readonly property string gsmBasic: "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    + "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
  readonly property string gsmExtension: "\f^{}\\[~]|€"

  // Nerd Font (Material Design Icons) glyphs, named once so the rest reads as text.
  QtObject {
    id: glyph
    readonly property string message: "󰍥"   // message-outline
    readonly property string unread: "󰍩"    // message-text
    readonly property string read: "󰍪"      // message-text-outline
    readonly property string partial: "󰍦"   // message-processing
    readonly property string outbound: "󰁜"  // arrow-top-right
    readonly property string compose: "󰤌"   // square-edit-outline
    readonly property string back: "󰁍"      // arrow-left
    readonly property string refresh: "󰑐"   // refresh
    readonly property string checkAll: "󰄭"  // check-all
    readonly property string check: "󰄬"     // check
    readonly property string copy: "󰆏"      // content-copy
    readonly property string reply: "󰑚"     // reply
    readonly property string key: "󰌆"       // key
    readonly property string send: "󰒊"      // send
    readonly property string alert: "󰗖"     // alert-circle-outline
    readonly property string info: "󰋽"      // information-outline
    readonly property string inbox: "󰚇"     // inbox
  }

  ListModel { id: inbox }

  function lookupService() {
    if (service || !bar || !bar.shell) return
    service = bar.shell.serviceFor("dji4g.sms")
  }

  function linkFacts() {
    var list = []
    if (!linkUp) return list
    var radio = snap.radio || {}
    var parts = []
    if (radio.rat) parts.push(radio.rat)
    if (typeof radio.band === "number") parts.push("Band " + radio.band)
    if (snap.roaming) parts.push("Roaming")
    if (parts.length > 0) list.push(parts.join(" · "))
    parts = []
    if (typeof radio.rsrp === "number") parts.push("RSRP " + radio.rsrp)
    if (typeof radio.sinr === "number") parts.push("SINR " + radio.sinr)
    if (parts.length > 0) list.push(parts.join(" · "))
    var storage = snap.storage || {}
    parts = []
    if (storage.SM) parts.push("SIM " + storage.SM.used + "/" + storage.SM.total)
    if (storage.ME) parts.push("MODEM " + storage.ME.used + "/" + storage.ME.total)
    if (parts.length > 0) list.push(parts.join(" · "))
    return list
  }

  function currentNotices() {
    var list = []
    if (!service) return list
    if (service.error !== "") list.push({ text: service.error, urgent: true, action: "" })
    else if (!snap.online && snap.error) list.push({ text: snap.error, urgent: true, action: "" })
    if (sendState === "sending") list.push({ text: "正在发送，请勿重复操作…", urgent: false, action: "" })
    else if (sendState === "accepted_by_modem") list.push({ text: "模块已接受短信（不代表对方已收到）", urgent: false, action: "" })
    else if (sendState === "failed") list.push({ text: "发送失败，模块未接受短信", urgent: true, action: "" })
    else if (sendState === "unknown") list.push({ text: "发送结果未知，请先核实，勿重复发送", urgent: true, action: "acknowledge" })
    else if ((snap.uncertain_sends || 0) > 0)
      list.push({ text: "有 " + snap.uncertain_sends + " 条发送结果未确认，请先核实，勿重复发送", urgent: true, action: "acknowledge" })
    var storage = snap.storage || {}
    var names = { SM: "SIM 卡", ME: "模块" }
    for (var name in names) {
      var store = storage[name]
      if (!store || store.state === "normal") continue
      list.push({
        text: names[name] + "短信存储" + (store.state === "full" ? "已满，新短信可能收不到" : "即将存满")
          + "（" + store.used + "/" + store.total + "）",
        urgent: store.state === "full", action: ""
      })
    }
    return list
  }

  function indexOfId(id) {
    for (var i = 0; i < inbox.count; i++) if (inbox.get(i).mid === id) return i
    return -1
  }

  // Updated in place so rows keep their identity (and their animations)
  // across the helper's periodic snapshots.
  function syncInbox() {
    var messages = snap.messages || []
    var cursorId = cursorActive && selectedIndex < inbox.count ? inbox.get(selectedIndex).mid : -1
    var ids = {}
    var i
    for (i = 0; i < messages.length; i++) ids[messages[i].id] = true
    for (i = inbox.count - 1; i >= 0; i--) if (!ids[inbox.get(i).mid]) inbox.remove(i)
    for (i = 0; i < messages.length; i++) {
      var message = messages[i]
      var row = {
        mid: message.id, peer: message.peer || "", time: message.time || "",
        direction: message.direction || "", unread: !!message.unread, complete: !!message.complete
      }
      var at = -1
      for (var j = i; j < inbox.count; j++) if (inbox.get(j).mid === message.id) { at = j; break }
      if (at < 0) inbox.insert(i, row)
      else {
        if (at !== i) inbox.move(at, i, 1)
        inbox.set(i, row)
      }
    }
    if (expandedId >= 0 && !ids[expandedId]) collapse()
    var kept = cursorId >= 0 ? indexOfId(cursorId) : -1
    selectedIndex = kept >= 0 ? kept : Math.max(0, Math.min(selectedIndex, inbox.count - 1))
    if (!primed && snap.online) Qt.callLater(function() { sms.primed = true })
  }

  function parseTime(iso) {
    if (!iso) return null
    var date = new Date(String(iso).replace(" ", "T").replace(/\.\d+/, ""))
    return isNaN(date.getTime()) ? null : date
  }

  function relativeTime(iso, nowMs) {
    var date = parseTime(iso)
    if (!date) return ""
    var age = nowMs - date.getTime()
    if (age < 60000) return "刚刚"
    if (age < 3600000) return Math.floor(age / 60000) + " 分钟前"
    var today = new Date(nowMs)
    today.setHours(0, 0, 0, 0)
    var clock = Qt.formatDateTime(date, "HH:mm")
    if (date.getTime() >= today.getTime()) return clock
    if (date.getTime() >= today.getTime() - 86400000) return "昨天 " + clock
    if (date.getFullYear() === today.getFullYear()) return (date.getMonth() + 1) + "月" + date.getDate() + "日"
    return date.getFullYear() + "/" + (date.getMonth() + 1) + "/" + date.getDate()
  }

  function fullTime(iso) {
    var date = parseTime(iso)
    return date ? Qt.formatDateTime(date, "yyyy-MM-dd HH:mm") : ""
  }

  // A code is only offered when the text says it is one; the closest number
  // after that word wins, so order numbers and phone numbers are not picked.
  function findCode(body) {
    if (!body) return ""
    var keyword = /验证码|校验码|动态码|动态密码|确认码|授权码|取件码|提取码|verification|passcode|code|otp/i.exec(body)
    if (!keyword) return ""
    var pattern = /[0-9]{4,8}/g
    var best = ""
    var bestScore = Infinity
    var match
    while ((match = pattern.exec(body)) !== null) {
      var before = match.index > 0 ? body.charAt(match.index - 1) : ""
      var after = body.charAt(match.index + match[0].length)
      if (/[0-9A-Za-z]/.test(before) || /[0-9A-Za-z]/.test(after)) continue
      var score = match.index >= keyword.index ? match.index - keyword.index : keyword.index - match.index + 40
      if (score < bestScore) {
        bestScore = score
        best = match[0]
      }
    }
    return best
  }

  function estimate(text) {
    var gsm = true
    var units = []
    var i
    for (i = 0; i < text.length; i++) {
      var character = text.charAt(i)
      if (gsmExtension.indexOf(character) >= 0) units.push(2)
      else if (gsmBasic.indexOf(character) >= 0) units.push(1)
      else {
        gsm = false
        break
      }
    }
    if (!gsm) {
      units = []
      for (i = 0; i < text.length; i++) {
        var code = text.charCodeAt(i)
        if (code >= 0xD800 && code <= 0xDBFF && i + 1 < text.length) {
          units.push(2)
          i++
        } else units.push(1)
      }
    }
    var total = 0
    for (i = 0; i < units.length; i++) total += units[i]
    var single = gsm ? 160 : 70
    var multi = gsm ? 153 : 67
    var segments = total > 0 ? 1 : 0
    if (total > single) {
      var used = 0
      for (i = 0; i < units.length; i++) {
        if (used > 0 && used + units[i] > multi) {
          segments++
          used = 0
        }
        used += units[i]
      }
    }
    return { encoding: gsm ? "GSM-7" : "UCS-2", units: total, segments: segments, capacity: segments > 1 ? segments * multi : single }
  }

  function refresh() { if (service) service.refresh() }
  function markAllRead() { if (service && unread > 0) service.markAllRead() }
  // Only silences the warning; nothing is ever sent again from here.
  function acknowledge() {
    if (!service) return
    if (service.sendState === "unknown") service.sendState = ""
    service.acknowledge()
  }

  function setCursor(index) {
    cursorActive = true
    selectedIndex = index
  }

  function moveCursor(dy) {
    if (mode !== "inbox" || inbox.count === 0) return
    if (!cursorActive) cursorActive = true
    else selectedIndex = Math.max(0, Math.min(inbox.count - 1, selectedIndex + dy))
    scrollItemIntoView(rows.itemAt(selectedIndex))
  }

  function activateCursor() {
    if (mode !== "inbox" || inbox.count === 0) return
    cursorActive = true
    toggleMessage(inbox.get(selectedIndex).mid)
  }

  function toggleMessage(id) {
    if (expandedId === id) collapse()
    else reveal(id)
  }

  // The body is fetched only for the row the user opened, and dropped again
  // as soon as that row closes.
  function reveal(id) {
    if (!service) return
    expandedId = id
    revealFailed = false
    copied = ""
    wantShow = !service.showMessage(id)
    if (wantShow && service.busy && service.pendingAction === "show") service.discardSensitiveReply = true
  }

  function collapse() {
    expandedId = -1
    wantShow = false
    revealFailed = false
    copied = ""
    if (!service) return
    service.detail = null
    if (service.busy && service.pendingAction === "show") service.discardSensitiveReply = true
  }

  function revealLatest() {
    for (var i = 0; i < inbox.count; i++) {
      if (inbox.get(i).direction === "outbound") continue
      setCursor(i)
      reveal(inbox.get(i).mid)
      scrollItemIntoView(rows.itemAt(i))
      return
    }
  }

  // Runs once the service has settled: Service.receive() releases `busy`
  // before it assigns the reply, so this is always deferred.
  function settle() {
    if (!service || service.busy) return
    if (wantLatest) {
      wantLatest = false
      revealLatest()
    } else if (wantShow && expandedId >= 0) reveal(expandedId)
    else if (expandedId >= 0 && !detail) revealFailed = true
    ensureComposeFocus()
  }

  function openLatest() {
    if (mode === "compose") leaveCompose()
    wantLatest = true
    if (!opened) open()
    else Qt.callLater(settle)
  }

  function copyText(kind) {
    if (!service || !detail) return
    var text = kind === "code" ? findCode(detail.body) : detail.body
    if (!text) return
    service.copy(text)
    copied = kind
    copiedReset.restart()
  }

  function scrollItemIntoView(item) {
    if (!item) return
    Qt.callLater(function() {
      if (!item || !item.parent) return
      var top = item.mapToItem(listColumn, 0, 0).y
      var bottom = top + item.height
      if (top < flick.contentY || item.height > flick.height) flick.contentY = top
      else if (bottom > flick.contentY + flick.height) flick.contentY = bottom - flick.height
    })
  }

  function startCompose(recipient) {
    collapse()
    mode = "compose"
    if (recipient) recipientField.text = recipient
    if (!opened) open()
    else Qt.callLater(focusCurrent)
  }

  function leaveCompose() {
    invalidatePreview()
    mode = "inbox"
    Qt.callLater(focusCurrent)
  }

  // A preview is bound to the exact text it was made from; any edit drops
  // it, including one that is still on its way back from the helper.
  function invalidatePreview() {
    if (!service) return
    service.preview = null
    if (service.busy && service.pendingAction === "preview") service.discardSensitiveReply = true
  }

  function requestPreview() {
    if (canPreview) service.prepare(recipientText, bodyEdit.text)
  }

  function focusCurrent() {
    if (!opened) return
    if (mode !== "compose") keyCatcher.forceActiveFocus()
    else if (pending && holdButton.enabled) holdButton.forceActiveFocus()
    else (recipientField.text === "" ? recipientField : bodyEdit).forceActiveFocus()
  }

  function ensureComposeFocus() {
    if (!opened || mode !== "compose") return
    if (recipientField.activeFocus || bodyEdit.activeFocus || previewButton.activeFocus || holdButton.activeFocus) return
    focusCurrent()
  }

  function composeFocus(index, direction) {
    var chain = [recipientField, bodyEdit, pending ? holdButton : previewButton]
    for (var step = 0; step < chain.length; step++) {
      var item = chain[((index % chain.length) + chain.length) % chain.length]
      if (item.enabled) {
        item.forceActiveFocus()
        return
      }
      index += direction
    }
  }

  function composeKey(event, index) {
    if (event.key === Qt.Key_Escape) {
      if (pending) {
        invalidatePreview()
        bodyEdit.forceActiveFocus()
      } else leaveCompose()
    } else if (event.key === Qt.Key_Backtab || (event.key === Qt.Key_Tab && (event.modifiers & Qt.ShiftModifier))) composeFocus(index - 1, -1)
    else if (event.key === Qt.Key_Tab || (index === 0 && event.key === Qt.Key_Down)) composeFocus(index + 1, 1)
    else return
    event.accepted = true
  }

  Component.onCompleted: {
    lookupService()
    syncInbox()
  }
  onSnapChanged: syncInbox()
  onUnreadChanged: {
    if (unread > seenUnread) pulse.restart()
    seenUnread = unread
  }
  onOpenedChanged: {
    if (opened) {
      now = Date.now()
      cursorActive = false
      selectedIndex = 0
      phraseIndex = 0
      flick.contentY = 0
      refresh()
      Qt.callLater(settle)
      Qt.callLater(focusCurrent)
    } else {
      // An unfinished draft survives a stray outside click; everything that
      // came from the engine does not.
      var keepDraft = mode === "compose" && (recipientField.text !== "" || bodyEdit.text !== "")
      expandedId = -1
      wantShow = false
      wantLatest = false
      revealFailed = false
      copied = ""
      if (!keepDraft) {
        mode = "inbox"
        recipientField.text = ""
        bodyEdit.text = ""
      }
      if (service) {
        if (service.sendState !== "sending") service.sendState = ""
        service.clearSensitive()
      }
    }
  }

  Connections {
    target: sms.service
    function onPresentChanged() { if (!sms.service.present) sms.close() }
    function onBusyChanged() { Qt.callLater(sms.settle) }
    function onDetailChanged() { Qt.callLater(sms.settle) }
    function onPreviewChanged() { Qt.callLater(sms.service.preview ? sms.focusCurrent : sms.ensureComposeFocus) }
    function onSendStateChanged() {
      if (sms.service.sendState !== "accepted_by_modem") return
      recipientField.text = ""
      bodyEdit.text = ""
      if (sms.mode === "compose") sms.leaveCompose()
      Qt.callLater(sms.refresh)
    }
  }

  Timer {
    interval: 300
    repeat: true
    running: !sms.service
    onTriggered: sms.lookupService()
  }
  Timer {
    interval: 30000
    repeat: true
    running: sms.opened
    onTriggered: sms.now = Date.now()
  }
  Timer {
    id: copiedReset
    interval: 1400
    onTriggered: sms.copied = ""
  }
  Timer {
    interval: 3200
    repeat: true
    running: sms.opened && sms.linkUp && sms.facts.length > 1
    onTriggered: phraseSwap.restart()
  }
  SequentialAnimation {
    id: phraseSwap
    PropertyAnimation { target: hero; property: "metaOpacity"; to: 0.0; duration: 180; easing.type: Easing.OutQuad }
    ScriptAction { script: sms.phraseIndex = (sms.phraseIndex + 1) % Math.max(1, sms.facts.length) }
    PropertyAnimation { target: hero; property: "metaOpacity"; to: 1.0; duration: 260; easing.type: Easing.InQuad }
  }
  SequentialAnimation {
    id: pulse
    NumberAnimation { target: badge; property: "scale"; to: 1.9; duration: 140; easing.type: Easing.OutQuad }
    NumberAnimation { target: badge; property: "scale"; to: 1.0; duration: 420; easing.type: Easing.OutBounce }
  }

  IpcHandler {
    target: sms.ipcTarget

    function open(): void { if (sms.available) sms.open() }
    function close(): void { sms.close() }
    function show(): void { if (sms.available) sms.open() }
    function hide(): void { sms.close() }
    function toggle(): void { if (sms.available) sms.toggle() }
    function compose(): void { if (sms.available) sms.startCompose("") }
    function latest(): void { if (sms.available) sms.openLatest() }
  }

  visible: available
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: sms.bar
    text: sms.unread > 0 ? glyph.unread : glyph.message
    dimmed: !sms.linkUp && sms.unread === 0
    tooltipText: sms.unread > 0 ? sms.unread + " 条未读短信" : "短信 · " + sms.statusText
    onPressed: function(code) {
      if (code === Qt.RightButton) sms.startCompose("")
      else sms.toggle()
    }

    Rectangle {
      id: badge
      visible: sms.unread > 0
      width: Style.space(6)
      height: width
      radius: width / 2
      x: Math.round(parent.width / 2 + Style.space(3))
      y: Math.round(parent.height / 2 - Style.space(9))
      color: Color.accent
    }
  }

  component SmsText: Text {
    textFormat: Text.PlainText
    color: sms.foreground
    font.family: sms.fontFamily
    font.pixelSize: Style.font.body
  }

  // Icons differ in advance width; a fixed box keeps them in a column.
  component SmsGlyph: Text {
    textFormat: Text.PlainText
    color: sms.foreground
    font.family: sms.fontFamily
    font.pixelSize: Style.font.icon
    width: Math.ceil(font.pixelSize * 1.2)
    horizontalAlignment: Text.AlignHCenter
  }

  // Four ascending bars; sweeps while the module is still looking for a cell.
  component SignalBars: Item {
    id: bars

    property int level: 0
    property bool searching: false
    property color color: Color.foreground
    property real size: Style.font.display
    property int sweep: 0

    implicitWidth: size
    implicitHeight: size

    Timer {
      interval: 260
      repeat: true
      running: bars.searching
      onTriggered: bars.sweep = (bars.sweep + 1) % 5
      onRunningChanged: if (!running) bars.sweep = 0
    }

    Row {
      id: barRow
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.bottom: parent.bottom
      anchors.bottomMargin: Math.round(bars.size * 0.14)
      height: Math.round(bars.size * 0.72)
      spacing: Math.max(2, Math.round(bars.size / 10))

      Repeater {
        model: 4

        Rectangle {
          required property int index
          width: Math.max(2, Math.round((bars.size - barRow.spacing * 3) / 4))
          height: Math.round(barRow.height * (index + 1) / 4)
          anchors.bottom: parent.bottom
          color: bars.color
          opacity: index < (bars.searching ? bars.sweep : bars.level) ? 1 : 0.22

          Behavior on opacity { NumberAnimation { duration: 180 } }
        }
      }
    }
  }

  // Sending costs money and cannot be undone, so it takes a deliberate hold
  // rather than a click: releasing early cancels.
  component HoldButton: BorderSurface {
    id: hold

    property string label: ""
    property string iconText: ""
    property real progress: 0
    property bool holding: false
    readonly property bool hot: holdMouse.containsMouse && enabled

    signal completed()
    signal otherKey(var event)

    function press() { if (enabled) holding = true }
    function release() { holding = false }
    function isHoldKey(event) {
      return event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space
    }

    activeFocusOnTab: true
    implicitHeight: holdLabel.implicitHeight + Style.spacing.controlPaddingY * 2 + Style.space(6)
    radius: Style.cornerRadius
    opacity: enabled ? 1 : 0.5
    color: Style.controlFill(activeFocus, hot, sms.foreground, Color.accent)
    borderSpec: Border.controlSpec(activeFocus ? "focus" : (hot ? "hover-cursor" : "normal"), sms.foreground, Color.accent)

    onHoldingChanged: {
      if (holding) {
        drain.stop()
        charge.restart()
      } else if (charge.running) {
        charge.stop()
        drain.restart()
      }
    }
    onEnabledChanged: if (!enabled) release()
    onActiveFocusChanged: if (!activeFocus) release()
    onVisibleChanged: if (!visible) release()

    Keys.onPressed: function(event) {
      if (!isHoldKey(event)) {
        otherKey(event)
        return
      }
      // A key that was already down when focus arrived must not start a send.
      if (!event.isAutoRepeat) press()
      event.accepted = true
    }
    Keys.onReleased: function(event) {
      if (!isHoldKey(event)) return
      if (!event.isAutoRepeat) release()
      event.accepted = true
    }

    NumberAnimation {
      id: charge
      target: hold
      property: "progress"
      from: 0
      to: 1
      duration: 900
      onFinished: {
        hold.holding = false
        drain.restart()
        hold.completed()
      }
    }
    NumberAnimation {
      id: drain
      target: hold
      property: "progress"
      to: 0
      duration: 180
      easing.type: Easing.OutCubic
    }

    Rectangle {
      x: hold.borderLeft
      y: hold.borderTop
      width: (hold.width - hold.borderLeft - hold.borderRight) * hold.progress
      height: hold.height - hold.borderTop - hold.borderBottom
      visible: hold.progress > 0
      color: Util.alpha(Color.accent, 0.45)
    }

    Row {
      id: holdLabel
      anchors.centerIn: parent
      spacing: Style.spacing.controlGap

      SmsGlyph {
        visible: hold.iconText !== ""
        text: hold.iconText
        anchors.verticalCenter: parent.verticalCenter
      }
      SmsText {
        text: hold.label
        font.bold: true
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    MouseArea {
      id: holdMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onPressed: {
        hold.forceActiveFocus()
        hold.press()
      }
      onReleased: hold.release()
      onCanceled: hold.release()
      onContainsMouseChanged: if (!containsMouse) hold.release()
    }
  }

  component MessageRow: CursorSurface {
    id: row

    required property int index
    required property int mid
    required property string peer
    required property string time
    required property string direction
    required property bool unread
    required property bool complete

    readonly property bool expanded: sms.expandedId === mid
    readonly property var message: expanded ? sms.detail : null
    readonly property string code: message ? sms.findCode(message.body) : ""
    readonly property bool replyable: !!message && /^\+?[0-9]{3,15}$/.test(message.peer)

    width: listColumn.width
    implicitHeight: summary.implicitHeight + Style.spacing.rowPaddingX + revealBox.height
    hasCursor: sms.cursorActive && sms.selectedIndex === index
    current: expanded
    currentFill: fill
    foreground: sms.foreground
    opacity: 0

    Component.onCompleted: {
      if (sms.primed) arrive.start()
      else opacity = 1
    }
    onHeightChanged: if (expanded) sms.scrollItemIntoView(row)

    NumberAnimation {
      id: arrive
      target: row
      property: "opacity"
      from: 0
      to: 1
      duration: 260
      easing.type: Easing.OutCubic
    }

    // Only the summary line toggles, so a click next to an action button
    // does not fold the message away.
    MouseArea {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      height: row.expanded ? summary.implicitHeight + Style.spacing.rowPaddingX : parent.height
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onContainsMouseChanged: if (containsMouse) sms.setCursor(row.index)
      onClicked: sms.toggleMessage(row.mid)
    }

    RowLayout {
      id: summary
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      anchors.topMargin: Style.spacing.rowPaddingX / 2
      spacing: Style.space(10)

      SmsGlyph {
        id: marker
        Layout.preferredWidth: Math.ceil(font.pixelSize * 1.2)
        text: !row.complete ? glyph.partial
          : row.direction === "outbound" ? glyph.outbound
          : row.unread ? glyph.unread : glyph.read
        color: row.unread ? Color.accent : sms.dim
      }
      SmsText {
        Layout.fillWidth: true
        text: row.message ? row.message.peer : row.peer
        font.bold: row.unread
        elide: Text.ElideRight
      }
      SmsText {
        text: row.expanded ? sms.fullTime(row.time) : sms.relativeTime(row.time, sms.now)
        color: sms.dim
        font.pixelSize: Style.font.caption
      }
    }

    Item {
      id: revealBox
      anchors.left: summary.left
      anchors.right: summary.right
      anchors.top: summary.bottom
      anchors.leftMargin: marker.width + summary.spacing
      height: row.expanded ? revealLoader.implicitHeight + Style.space(8) : 0
      opacity: row.expanded ? 1 : 0
      visible: height > 0
      clip: true

      Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
      Behavior on opacity { NumberAnimation { duration: 160 } }

      Loader {
        id: revealLoader
        y: Style.space(8)
        width: parent.width
        active: row.expanded || revealBox.height > 0

        sourceComponent: Component {
          Column {
            spacing: Style.space(8)

            SmsText {
              width: parent.width
              wrapMode: Text.Wrap
              lineHeight: 1.2
              text: row.message ? row.message.body
                : !row.expanded ? ""
                : sms.revealFailed ? "读取失败，点击重试" : "正在读取…"
              color: row.message ? sms.foreground : sms.dim
            }
            SmsText {
              visible: !!row.message && !row.message.complete
              text: "长短信尚未收全"
              color: sms.dim
              font.pixelSize: Style.font.caption
            }
            Item {
              width: parent.width
              implicitHeight: Math.max(codeChip.implicitHeight, tools.implicitHeight)
              visible: !!row.message

              Button {
                id: codeChip
                visible: row.code !== ""
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: row.code
                iconText: sms.copied === "code" ? glyph.check : glyph.key
                tooltipText: "复制验证码 (C)"
                bordered: true
                selected: true
                foreground: sms.foreground
                fontFamily: sms.fontFamily
                onClicked: sms.copyText("code")
              }
              Row {
                id: tools
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)

                PanelActionButton {
                  iconText: sms.copied === "body" ? glyph.check : glyph.copy
                  tooltipText: "复制全文"
                  foreground: sms.foreground
                  fontFamily: sms.fontFamily
                  onClicked: sms.copyText("body")
                }
                PanelActionButton {
                  visible: row.replyable
                  iconText: glyph.reply
                  tooltipText: "回复"
                  foreground: sms.foreground
                  fontFamily: sms.fontFamily
                  onClicked: sms.startCompose(row.message.peer)
                }
              }
            }
          }
        }
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: sms
    bar: sms.bar
    open: sms.opened
    focusTarget: sms.mode !== "compose" ? keyCatcher : (recipientField.text === "" ? recipientField : bodyEdit)
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      clip: true
      // The editors own the keyboard while writing.
      blocked: sms.mode === "compose" && !keyCatcher.activeFocus

      onMoveRequested: function(dx, dy) {
        if (dy !== 0) sms.moveCursor(dy)
        else if (dx > 0 && sms.cursorActive && sms.selectedIndex < inbox.count && sms.expandedId !== inbox.get(sms.selectedIndex).mid) sms.activateCursor()
        else if (dx < 0) sms.collapse()
      }
      onActivateRequested: sms.activateCursor()
      onCloseRequested: {
        if (sms.mode === "compose") sms.leaveCompose()
        else if (sms.expandedId >= 0) sms.collapse()
        else sms.close()
      }
      onTabRequested: function(direction) { sms.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "n") sms.startCompose("")
        else if (text === "r") sms.refresh()
        else if (text === "a") sms.markAllRead()
        else if (text === "c" && sms.detail) sms.copyText(sms.findCode(sms.detail.body) !== "" ? "code" : "body")
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          id: hero
          width: parent.width
          title: "短信"
          meta: sms.heroMeta
          detail: sms.unread > 0 ? sms.unread + " 未读" : ""
          foreground: sms.foreground
          fontFamily: sms.fontFamily
          iconOpacity: sms.linkUp ? 1.0 : 0.5
          iconComponent: Component {
            SignalBars {
              level: sms.linkUp ? (sms.snap.signal || 0) : 0
              searching: sms.opened && !!sms.snap.online && !!sms.snap.connected && !sms.linkUp
              color: sms.foreground
            }
          }
          trailingControl: Component {
            Row {
              spacing: Style.space(2)

              PanelActionButton {
                size: Style.space(26)
                iconText: sms.mode === "compose" ? glyph.back : glyph.compose
                tooltipText: sms.mode === "compose" ? "返回收件箱 (Esc)" : "写短信 (N)"
                foreground: sms.foreground
                fontFamily: sms.fontFamily
                onClicked: sms.mode === "compose" ? sms.leaveCompose() : sms.startCompose("")
              }
              PanelActionButton {
                id: refreshButton
                size: Style.space(26)
                tooltipText: "刷新 (R)"
                foreground: sms.foreground
                fontFamily: sms.fontFamily
                onClicked: sms.refresh()

                // Spins once per request, and keeps going for a slow one.
                SmsGlyph {
                  id: refreshIcon
                  anchors.centerIn: parent
                  text: glyph.refresh
                }
                RotationAnimation {
                  id: spin
                  target: refreshIcon
                  from: 0
                  to: 360
                  duration: 700
                  easing.type: Easing.InOutQuad
                  onFinished: if (sms.service && sms.service.busy) spin.restart()
                }
                Connections {
                  target: sms.service
                  function onBusyChanged() { if (sms.service.busy && sms.opened && !spin.running) spin.restart() }
                }
              }
            }
          }
        }

        Column {
          id: noticeColumn
          width: parent.width
          spacing: Style.space(6)
          visible: sms.notices.length > 0

          Repeater {
            model: sms.notices

            Item {
              id: notice
              required property var modelData
              width: noticeColumn.width
              implicitHeight: Math.max(noticeIcon.implicitHeight, noticeText.implicitHeight, noticeAction.visible ? noticeAction.implicitHeight : 0)

              SmsGlyph {
                id: noticeIcon
                anchors.left: parent.left
                anchors.top: parent.top
                text: notice.modelData.urgent ? glyph.alert : glyph.info
                color: notice.modelData.urgent ? sms.urgent : sms.dim
              }
              SmsText {
                id: noticeText
                anchors.left: noticeIcon.right
                anchors.leftMargin: Style.space(8)
                anchors.right: noticeAction.visible ? noticeAction.left : parent.right
                anchors.rightMargin: noticeAction.visible ? Style.space(8) : 0
                anchors.verticalCenter: parent.verticalCenter
                text: notice.modelData.text
                color: notice.modelData.urgent ? sms.urgent : sms.foreground
                wrapMode: Text.Wrap
              }
              PanelActionButton {
                id: noticeAction
                visible: notice.modelData.action === "acknowledge"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                iconText: glyph.check
                tooltipText: "已核实，不再提示"
                foreground: sms.foreground
                fontFamily: sms.fontFamily
                onClicked: sms.acknowledge()
              }
            }
          }
        }

        PanelSeparator { foreground: sms.foreground }

        Column {
          width: parent.width
          spacing: Style.space(6)
          visible: sms.mode === "inbox"

          Item {
            width: parent.width
            implicitHeight: Style.space(22)

            PanelSectionHeader {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "收件箱"
              foreground: sms.foreground
              fontFamily: sms.fontFamily
            }
            PanelActionButton {
              visible: sms.unread > 0
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: glyph.checkAll
              tooltipText: "全部标为已读 (A)"
              foreground: sms.foreground
              fontFamily: sms.fontFamily
              onClicked: sms.markAllRead()
            }
          }

          Flickable {
            id: flick
            width: parent.width
            height: Math.min(listColumn.implicitHeight, sms.listCap)
            visible: inbox.count > 0
            contentWidth: width
            contentHeight: listColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            interactive: contentHeight > height
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
              id: listColumn
              width: flick.width
              spacing: Style.space(2)

              Repeater {
                id: rows
                model: inbox

                MessageRow {}
              }
            }
          }

          Column {
            visible: inbox.count === 0
            width: parent.width
            topPadding: Style.space(18)
            bottomPadding: Style.space(18)
            spacing: Style.space(8)

            SmsGlyph {
              anchors.horizontalCenter: parent.horizontalCenter
              text: glyph.inbox
              color: sms.dim
              font.pixelSize: Style.font.display
            }
            SmsText {
              anchors.horizontalCenter: parent.horizontalCenter
              text: sms.snap.online ? "还没有短信" : "短信引擎运行后显示短信"
              color: sms.dim
            }
          }
        }

        Column {
          width: parent.width
          spacing: Style.space(8)
          visible: sms.mode === "compose"

          Item {
            width: parent.width
            implicitHeight: Style.space(22)

            PanelSectionHeader {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "写短信"
              foreground: sms.foreground
              fontFamily: sms.fontFamily
            }
          }

          TextField {
            id: recipientField
            width: parent.width
            placeholderText: "收件号码"
            foreground: sms.foreground
            font.family: sms.fontFamily
            inputMethodHints: Qt.ImhDialableCharactersOnly
            maximumLength: 20
            selectByMouse: true
            readOnly: sms.sendState === "sending"
            onTextChanged: sms.invalidatePreview()
            onAccepted: bodyEdit.forceActiveFocus()
            Keys.onPressed: function(event) { sms.composeKey(event, 0) }
          }

          // The kit has no multi-line input; this borrows TextField's tokens.
          BorderSurface {
            id: bodyBox
            readonly property bool hot: bodyHover.hovered
            readonly property var spec: Border.controlSpec(bodyEdit.activeFocus ? "focus" : (hot ? "hover-cursor" : "normal"), sms.foreground, Color.accent)
            readonly property real insetX: Style.spacing.controlPaddingX + Border.left(spec)
            readonly property real insetY: Style.spacing.inputPaddingY + Border.top(spec)

            width: parent.width
            implicitHeight: Math.max(Style.space(88), Math.min(Style.space(190), bodyEdit.implicitHeight + insetY * 2))
            radius: Style.cornerRadius
            color: Style.controlFill(bodyEdit.activeFocus, hot, sms.foreground, Color.accent)
            borderSpec: spec

            HoverHandler {
              id: bodyHover
              cursorShape: Qt.IBeamCursor
            }

            Flickable {
              id: bodyFlick
              anchors.fill: parent
              anchors.leftMargin: bodyBox.insetX
              anchors.rightMargin: bodyBox.insetX
              anchors.topMargin: bodyBox.insetY
              anchors.bottomMargin: bodyBox.insetY
              contentWidth: width
              contentHeight: bodyEdit.height
              clip: true
              boundsBehavior: Flickable.StopAtBounds
              interactive: contentHeight > height

              function reveal(area) {
                if (contentY > area.y) contentY = area.y
                else if (contentY + height < area.y + area.height) contentY = area.y + area.height - height
              }

              TextEdit {
                id: bodyEdit
                width: bodyFlick.width
                height: Math.max(bodyFlick.height, implicitHeight)
                wrapMode: TextEdit.Wrap
                textFormat: TextEdit.PlainText
                selectByMouse: true
                readOnly: sms.sendState === "sending"
                color: sms.foreground
                selectionColor: Style.selectionFillFor(sms.foreground, Color.accent)
                selectedTextColor: sms.foreground
                font.family: sms.fontFamily
                font.pixelSize: Style.font.body
                onTextChanged: sms.invalidatePreview()
                onCursorRectangleChanged: bodyFlick.reveal(cursorRectangle)
                Keys.onPressed: function(event) {
                  var enter = event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                  if (enter && (event.modifiers & Qt.ControlModifier)) {
                    sms.requestPreview()
                    event.accepted = true
                  } else sms.composeKey(event, 1)
                }
              }
            }

            SmsText {
              x: bodyBox.insetX
              y: bodyBox.insetY
              visible: bodyEdit.text === "" && bodyEdit.preeditText === ""
              text: "短信内容"
              color: Qt.darker(sms.foreground, 1.6)
            }
          }

          Item {
            width: parent.width
            implicitHeight: Math.max(hint.implicitHeight, previewButton.implicitHeight)
            visible: !sms.pending

            SmsText {
              id: hint
              anchors.left: parent.left
              anchors.right: previewButton.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              text: sms.composeHint
              color: sms.composeProblem ? sms.urgent : sms.dim
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }
            Button {
              id: previewButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: "预览"
              tooltipText: "预览后再确认发送 (Ctrl+Enter)"
              bordered: true
              focusable: true
              enabled: sms.canPreview
              opacity: enabled ? 1 : 0.45
              foreground: sms.foreground
              fontFamily: sms.fontFamily
              onClicked: sms.requestPreview()
              Keys.onPressed: function(event) { sms.composeKey(event, 2) }
            }
          }

          BorderSurface {
            width: parent.width
            implicitHeight: confirmColumn.implicitHeight + Style.space(20)
            visible: !!sms.pending
            radius: Style.cornerRadius
            color: Style.normalFillFor(sms.foreground, Color.accent)
            borderSpec: Border.controlSpec("normal", sms.foreground, Color.accent)

            Column {
              id: confirmColumn
              x: Style.space(10)
              y: Style.space(10)
              width: parent.width - Style.space(20)
              spacing: Style.space(6)

              SmsText {
                width: parent.width
                text: "发送给 " + sms.recipientText
                font.bold: true
                elide: Text.ElideMiddle
              }
              SmsText {
                width: parent.width
                text: sms.pending
                  ? (sms.pending.preview.encoding === "gsm7" ? "GSM-7" : "UCS-2") + " · " + sms.pending.preview.characters + " 字 · "
                    + sms.pending.preview.segments + " 段 · 可能产生运营商费用"
                  : ""
                color: sms.dim
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.Wrap
                bottomPadding: Style.space(4)
              }
              HoldButton {
                id: holdButton
                width: parent.width
                enabled: !!sms.snap.ready && !sms.busy
                iconText: glyph.send
                label: !sms.snap.ready ? "模块未就绪，暂时无法发送" : holding ? "继续按住…" : "按住发送"
                onCompleted: sms.service.submit()
                onOtherKey: function(event) { sms.composeKey(event, 2) }
              }
            }
          }
        }
      }
    }
  }
}
