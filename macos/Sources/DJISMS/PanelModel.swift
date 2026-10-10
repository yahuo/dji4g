import AppKit
import Observation

enum ComposeField: Hashable {
    case recipient, body, preview, hold
}

struct Notice: Identifiable, Equatable {
    let id: String
    let text: String
    let urgent: Bool
    let acknowledge: Bool
}

/// The panel's interaction state, ported from Panel.qml: an inbox of
/// accordion rows that open in place, and an inline compose card whose
/// preview must be confirmed with a deliberate hold.
@Observable
@MainActor
final class PanelModel: EngineListener {
    let engine: EngineClient

    var opened = false
    var mode = "inbox"
    var cursorActive = false
    var selectedIndex = 0
    var expandedId: Int?
    var wantShow = false
    var wantLatest = false
    var revealFailed = false
    var copied = ""
    var now = Date()
    var phraseIndex = 0
    var metaVisible = true
    var appError = ""
    /// Whether the module is plugged in; the engine runs only while it is.
    var deviceUp = false

    var recipient = "" {
        didSet {
            if recipient.count > 20 { recipient = String(recipient.prefix(20)) }
            if recipient != oldValue { invalidatePreview() }
        }
    }
    var body = "" {
        didSet { if body != oldValue { invalidatePreview() } }
    }
    var focus: ComposeField?
    /// Bumped to ask the inbox list to scroll a row into view.
    var scrollTarget: Int?
    var scrollTick = 0
    var holding = false

    @ObservationIgnored var requestOpen: (() -> Void)?
    @ObservationIgnored var requestClose: (() -> Void)?
    @ObservationIgnored var onIncoming: (([IncomingMessage]) -> Void)?
    @ObservationIgnored var onShown: ((Int) -> Void)?
    @ObservationIgnored var onAllRead: (() -> Void)?
    @ObservationIgnored var onSnapshot: (() -> Void)?
    @ObservationIgnored private var cursorBeforeSync: Int?
    @ObservationIgnored private var pendingRevealId: Int?
    @ObservationIgnored private var autoPreview = false
    @ObservationIgnored private var copiedReset: DispatchWorkItem?
    @ObservationIgnored private var holdWork: DispatchWorkItem?
    @ObservationIgnored private var clockTimer: Timer?
    @ObservationIgnored private var phraseTimer: Timer?

    init(engine: EngineClient) {
        self.engine = engine
        engine.listener = self
    }

    // MARK: Derived state

    var snap: Snapshot { engine.snapshot }
    var messages: [MessageSummary] { snap.messages }
    var busy: Bool { engine.busy }
    var unread: Int { snap.unread }
    var detail: MessageDetail? {
        guard let detail = engine.detail, detail.id == expandedId else { return nil }
        return detail
    }
    var pending: SendPreview? { engine.preview }
    var sendState: String { engine.sendState }
    var linkUp: Bool { snap.online && snap.connected && snap.simReady && snap.registered }
    /// False while the module is unplugged: the menu bar item hides and the
    /// panel cannot be summoned. An unresolved send keeps it on screen.
    var present: Bool { deviceUp || sendState == "sending" || sendState == "unknown" }

    var statusText: String {
        if !deviceUp { return "模块未插入" }
        if !snap.online { return !snap.error.isEmpty || !engine.error.isEmpty ? "短信引擎未运行" : "正在连接" }
        if !snap.connected { return "模块未连接" }
        if !snap.simReady { return "SIM 未就绪" }
        if !snap.registered { return "等待网络注册" }
        return "在线"
    }

    var facts: [String] {
        guard linkUp else { return [] }
        var list: [String] = []
        var parts: [String] = []
        if !snap.radio.rat.isEmpty { parts.append(snap.radio.rat) }
        if let band = snap.radio.band { parts.append("Band \(band)") }
        if snap.roaming { parts.append("Roaming") }
        if !parts.isEmpty { list.append(parts.joined(separator: " · ")) }
        parts = []
        if let rsrp = snap.radio.rsrp { parts.append("RSRP \(rsrp)") }
        if let sinr = snap.radio.sinr { parts.append("SINR \(sinr)") }
        if !parts.isEmpty { list.append(parts.joined(separator: " · ")) }
        parts = []
        if let sim = snap.storage["SM"] { parts.append("SIM \(sim.used)/\(sim.total)") }
        if let modem = snap.storage["ME"] { parts.append("MODEM \(modem.used)/\(modem.total)") }
        if !parts.isEmpty { list.append(parts.joined(separator: " · ")) }
        return list
    }

    var heroMeta: String {
        let facts = facts
        return linkUp && !facts.isEmpty ? facts[phraseIndex % facts.count] : statusText
    }

    var notices: [Notice] {
        var list: [Notice] = []
        if !appError.isEmpty { list.append(Notice(id: "app", text: appError, urgent: true, acknowledge: false)) }
        if !engine.error.isEmpty {
            list.append(Notice(id: "error", text: engine.error, urgent: true, acknowledge: false))
        } else if !snap.online && !snap.error.isEmpty {
            list.append(Notice(id: "error", text: snap.error, urgent: true, acknowledge: false))
        }
        switch sendState {
        case "sending":
            list.append(Notice(id: "send", text: "正在发送，请勿重复操作…", urgent: false, acknowledge: false))
        case "accepted_by_modem":
            list.append(Notice(id: "send", text: "模块已接受短信（不代表对方已收到）", urgent: false, acknowledge: false))
        case "failed":
            list.append(Notice(id: "send", text: "发送失败，模块未接受短信", urgent: true, acknowledge: false))
        case "unknown":
            list.append(Notice(id: "send", text: "发送结果未知，请先核实，勿重复发送", urgent: true, acknowledge: true))
        default:
            if snap.uncertainSends > 0 {
                list.append(Notice(id: "send", text: "有 \(snap.uncertainSends) 条发送结果未确认，请先核实，勿重复发送",
                                   urgent: true, acknowledge: true))
            }
        }
        for (name, label) in [("SM", "SIM 卡"), ("ME", "模块")] {
            guard let store = snap.storage[name], store.state != "normal", !store.state.isEmpty else { continue }
            let state = store.state == "full" ? "已满，新短信可能收不到" : "即将存满"
            list.append(Notice(id: "storage-\(name)", text: "\(label)短信存储\(state)（\(store.used)/\(store.total)）",
                               urgent: store.state == "full", acknowledge: false))
        }
        return list
    }

    var recipientText: String { recipient.trimmingCharacters(in: .whitespacesAndNewlines) }
    var recipientValid: Bool { SmsFormat.isValidRecipient(recipientText) }
    var draft: SmsFormat.Estimate { SmsFormat.estimate(body) }
    var composeProblem: Bool { draft.segments > 10 || (!recipientText.isEmpty && !recipientValid) }
    var canPreview: Bool {
        snap.online && !busy && sendState != "sending" && recipientValid
            && draft.segments >= 1 && draft.segments <= 10
    }
    var composeHint: String {
        let draft = draft
        if draft.segments > 10 { return "超过 10 段上限（当前 \(draft.segments) 段）" }
        if !recipientText.isEmpty && !recipientValid { return "号码需为 3–15 位数字，可带 +" }
        if draft.units == 0 { return "预览不会发送短信" }
        return "\(draft.encoding) · \(draft.units)/\(draft.capacity) · \(draft.segments) 段"
    }
    var canHold: Bool { snap.ready && !busy }
    var editingLocked: Bool { sendState == "sending" }
    var tooltip: String { unread > 0 ? "\(unread) 条未读短信" : "短信 · \(statusText)" }

    // MARK: Actions

    func refresh() { engine.refresh() }
    func markAllRead() { if unread > 0 { engine.markAllRead() } }

    /// Only silences the warning; nothing is ever sent again from here.
    func acknowledge() {
        if engine.sendState == "unknown" { engine.sendState = "" }
        engine.acknowledge()
    }

    func setCursor(_ index: Int) {
        cursorActive = true
        selectedIndex = index
    }

    func moveCursor(_ dy: Int) {
        guard mode == "inbox", !messages.isEmpty else { return }
        if !cursorActive { cursorActive = true }
        else { selectedIndex = max(0, min(messages.count - 1, selectedIndex + dy)) }
        scroll(to: messages[selectedIndex].id)
    }

    func activateCursor() {
        guard mode == "inbox", !messages.isEmpty else { return }
        cursorActive = true
        toggleMessage(messages[min(selectedIndex, messages.count - 1)].id)
    }

    func toggleMessage(_ id: Int) {
        if expandedId == id { collapse() } else { reveal(id) }
    }

    /// The body is fetched only for the row the user opened, and dropped again
    /// as soon as that row closes.
    func reveal(_ id: Int) {
        expandedId = id
        revealFailed = false
        copied = ""
        wantShow = !engine.showMessage(id)
        if wantShow && engine.busy && engine.pendingAction == "show" { engine.discardSensitiveReply = true }
    }

    func scroll(to id: Int) {
        scrollTarget = id
        scrollTick += 1
    }

    func collapse() {
        expandedId = nil
        wantShow = false
        revealFailed = false
        copied = ""
        engine.detail = nil
        if engine.busy && engine.pendingAction == "show" { engine.discardSensitiveReply = true }
    }

    private func revealLatest() {
        guard let index = messages.firstIndex(where: { $0.direction != "outbound" }) else { return }
        setCursor(index)
        reveal(messages[index].id)
        scroll(to: messages[index].id)
    }

    private func revealPending(_ id: Int) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        setCursor(index)
        reveal(id)
        scroll(to: id)
    }

    /// Runs once the engine has settled: a reply drops `busy` before its
    /// content lands, so this always runs after the whole reply is applied.
    func settle() {
        guard !engine.busy else { return }
        if let id = pendingRevealId {
            pendingRevealId = nil
            revealPending(id)
        } else if wantLatest {
            wantLatest = false
            revealLatest()
        } else if wantShow, let id = expandedId {
            reveal(id)
        } else if expandedId != nil && detail == nil {
            revealFailed = true
        }
        if mode == "compose" && autoPreview && pending == nil && canPreview {
            autoPreview = false
            requestPreview()
        }
        ensureComposeFocus()
    }

    func openLatest() {
        if mode == "compose" { leaveCompose() }
        wantLatest = true
        if !opened { requestOpen?() } else { DispatchQueue.main.async { self.settle() } }
    }

    func openMessage(_ id: Int) {
        if mode == "compose" { leaveCompose() }
        pendingRevealId = id
        if !opened { requestOpen?() } else { DispatchQueue.main.async { self.settle() } }
    }

    func copyText(_ kind: String) {
        guard let detail else { return }
        let text = kind == "code" ? SmsFormat.findCode(detail.body) : detail.body
        guard !text.isEmpty else { return }
        Clipboard.copySensitive(text)
        copied = kind
        copiedReset?.cancel()
        let reset = DispatchWorkItem { [weak self] in self?.copied = "" }
        copiedReset = reset
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.4, execute: reset)
    }

    func copyCurrent() {
        guard let detail else { return }
        copyText(SmsFormat.findCode(detail.body).isEmpty ? "body" : "code")
    }

    func startCompose(_ recipient: String = "", body: String? = nil, preview: Bool = false) {
        collapse()
        mode = "compose"
        if !recipient.isEmpty { self.recipient = recipient }
        if let body { self.body = body }
        autoPreview = preview
        if !opened { requestOpen?() } else { focusCurrent(); settle() }
    }

    func leaveCompose() {
        invalidatePreview()
        autoPreview = false
        mode = "inbox"
        focusCurrent()
    }

    /// A preview is bound to the exact text it was made from; any edit drops
    /// it, including one that is still on its way back from the engine.
    func invalidatePreview() {
        releaseHold()
        engine.preview = nil
        if engine.busy && engine.pendingAction == "preview" { engine.discardSensitiveReply = true }
    }

    func requestPreview() {
        if canPreview { engine.prepare(recipient: recipientText, body: body) }
    }

    func submit() { engine.submit() }

    /// Sending costs money and cannot be undone, so it takes a deliberate
    /// 900 ms hold rather than a click: releasing early cancels.
    func pressHold() {
        guard canHold, pending != nil, !holding else { return }
        focus = .hold
        holding = true
        holdWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.holding else { return }
            self.holding = false
            if self.canHold && self.pending != nil { self.submit() }
        }
        holdWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9, execute: work)
    }

    func releaseHold() {
        holdWork?.cancel()
        holdWork = nil
        if holding { holding = false }
    }

    func focusCurrent() {
        guard opened else { return }
        if mode != "compose" { focus = nil }
        else if pending != nil && canHold { focus = .hold }
        else { focus = recipient.isEmpty ? .recipient : .body }
    }

    func ensureComposeFocus() {
        guard opened, mode == "compose", focus == nil else { return }
        focusCurrent()
    }

    func composeFocus(_ index: Int, _ direction: Int) {
        let chain: [ComposeField] = [.recipient, .body, pending != nil ? .hold : .preview]
        var index = index
        for _ in chain.indices {
            let field = chain[((index % chain.count) + chain.count) % chain.count]
            let enabled: Bool
            switch field {
            case .hold: enabled = canHold
            case .preview: enabled = canPreview
            default: enabled = true
            }
            if enabled {
                focus = field
                return
            }
            index += direction
        }
    }

    func composeIndex() -> Int {
        switch focus {
        case .recipient: return 0
        case .body: return 1
        default: return 2
        }
    }

    /// Esc inside the panel: unwind one level at a time.
    func escape() {
        if mode == "compose" {
            if pending != nil {
                invalidatePreview()
                focus = .body
            } else {
                leaveCompose()
            }
        } else if expandedId != nil {
            collapse()
        } else {
            requestClose?()
        }
    }

    // MARK: Snapshot sync

    func snapshotChanged() {
        let ids = Set(messages.map(\.id))
        if let expandedId, !ids.contains(expandedId) { collapse() }
        if selectedIndex >= messages.count { selectedIndex = max(0, messages.count - 1) }
    }

    func cursorId() -> Int? {
        guard cursorActive, selectedIndex < messages.count else { return nil }
        return messages[selectedIndex].id
    }

    func keepCursor(on id: Int?) {
        if let id, let index = messages.firstIndex(where: { $0.id == id }) { selectedIndex = index }
        snapshotChanged()
    }

    // MARK: Open / close

    func didOpen() {
        opened = true
        now = Date()
        cursorActive = false
        selectedIndex = 0
        phraseIndex = 0
        metaVisible = true
        if let first = messages.first?.id { scroll(to: first) }
        refresh()
        DispatchQueue.main.async {
            self.settle()
            self.focusCurrent()
        }
        clockTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.now = Date() }
        }
        phraseTimer = Timer.scheduledTimer(withTimeInterval: 3.2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.swapPhrase() }
        }
    }

    private func swapPhrase() {
        guard opened, linkUp, facts.count > 1 else { return }
        metaVisible = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
            self.phraseIndex = (self.phraseIndex + 1) % max(1, self.facts.count)
            self.metaVisible = true
        }
    }

    func didClose() {
        opened = false
        clockTimer?.invalidate()
        phraseTimer?.invalidate()
        // An unfinished draft survives a stray outside click; everything that
        // came from the engine does not.
        let keepDraft = mode == "compose" && (!recipient.isEmpty || !body.isEmpty)
        expandedId = nil
        wantShow = false
        wantLatest = false
        pendingRevealId = nil
        autoPreview = false
        revealFailed = false
        copied = ""
        releaseHold()
        focus = nil
        if !keepDraft {
            mode = "inbox"
            recipient = ""
            body = ""
        }
        if engine.sendState != "sending" { engine.sendState = "" }
        engine.clearSensitive()
    }

    // MARK: EngineListener

    func engineSettled() {
        DispatchQueue.main.async { self.settle() }
    }

    func enginePreviewChanged() {
        DispatchQueue.main.async {
            if self.engine.preview != nil { self.focusCurrent() } else { self.ensureComposeFocus() }
        }
    }

    func engineSendStateChanged() {
        guard engine.sendState == "accepted_by_modem" else { return }
        recipient = ""
        body = ""
        if mode == "compose" { leaveCompose() }
        DispatchQueue.main.async { self.refresh() }
    }

    func engineReceived(_ messages: [IncomingMessage]) { onIncoming?(messages) }
    func engineShowed(_ id: Int) { onShown?(id) }
    func engineMarkedAllRead() { onAllRead?() }
    func engineSnapshotWillChange() { cursorBeforeSync = cursorId() }
    func engineSnapshotDidChange() {
        keepCursor(on: cursorBeforeSync)
        cursorBeforeSync = nil
        onSnapshot?()
    }
}
