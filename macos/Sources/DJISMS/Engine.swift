import Foundation
import Observation

@MainActor
protocol EngineListener: AnyObject {
    /// A reply finished (busy dropped) or the shown message changed.
    func engineSettled()
    func enginePreviewChanged()
    func engineSendStateChanged()
    func engineReceived(_ messages: [IncomingMessage])
    func engineShowed(_ id: Int)
    func engineMarkedAllRead()
    func engineDeleted(_ id: Int)
    func engineSnapshotWillChange()
    func engineSnapshotDidChange()
}

/// Owns the Python engine process and mirrors the Omarchy Service.qml state
/// machine: one request in flight, sensitive replies dropped once the panel
/// has moved on, and a send that is never retried.
@Observable
@MainActor
final class EngineClient {
    private(set) var snapshot = Snapshot() {
        willSet { listener?.engineSnapshotWillChange() }
        didSet { listener?.engineSnapshotDidChange() }
    }
    var detail: MessageDetail?
    var preview: SendPreview?
    var sendState = ""
    /// Counts from the last confirmed storage cleanup, until the panel closes.
    var cleaned: CleanupResult?
    var error = ""
    private(set) var busy = false
    private(set) var pendingAction = ""
    var discardSensitiveReply = false
    private(set) var running = false

    @ObservationIgnored weak var listener: EngineListener?
    @ObservationIgnored private var process: Process?
    @ObservationIgnored private var input: FileHandle?
    @ObservationIgnored private var buffer = Data()
    @ObservationIgnored private var restartTimer: Timer?
    @ObservationIgnored private var stopping = false
    @ObservationIgnored private var relaunchOnExit = false
    @ObservationIgnored private var exitWaiters: [() -> Void] = []

    // MARK: Requests

    @discardableResult
    func request(_ value: JSONObject) -> Bool {
        if busy { return false }
        guard running, let input else {
            // Stopped on purpose only while the module is unplugged (or on quit).
            error = stopping ? "模块未插入" : "本地短信引擎未运行"
            return false
        }
        guard var line = try? JSONSerialization.data(withJSONObject: value) else { return false }
        line.append(0x0A)
        busy = true
        pendingAction = value["action"] as? String ?? ""
        error = ""
        do {
            try input.write(contentsOf: line)
        } catch {
            // The exit handler resets state once the process is reaped.
            return true
        }
        return true
    }

    func refresh() { request(["action": "refresh"]) }
    func showMessage(_ id: Int) -> Bool {
        detail = nil
        return request(["action": "show", "id": id])
    }
    func markAllRead() { request(["action": "mark_all_read"]) }
    func acknowledge() { request(["action": "acknowledge"]) }
    func cleanup() {
        cleaned = nil
        request(["action": "cleanup", "confirmed": true])
    }
    func deleteMessage(_ id: Int) { request(["action": "delete", "id": id, "confirmed": true]) }

    func prepare(recipient: String, body: String) {
        preview = nil
        sendState = ""
        request(["action": "preview", "recipient": recipient, "body": body])
    }

    func submit() {
        guard !busy, let key = preview?.key else { return }
        if request(["action": "send", "key": key, "confirmed": true]) {
            preview = nil
            sendState = "sending"
            listener?.engineSendStateChanged()
        }
    }

    func clearSensitive() {
        detail = nil
        preview = nil
        if busy && (pendingAction == "preview" || pendingAction == "show") { discardSensitiveReply = true }
        if !busy { request(["action": "cancel"]) }
    }

    // MARK: Replies

    private func receive(_ line: Data) {
        guard let value = (try? JSONSerialization.jsonObject(with: line)) as? JSONObject else { return }
        let type = value["type"] as? String ?? ""
        let isResponse = (value["response"] as? Bool) == true
        if isResponse {
            let discard = discardSensitiveReply
            busy = false
            pendingAction = ""
            discardSensitiveReply = false
            if discard && (type == "preview" || type == "detail") {
                listener?.engineSettled()
                return
            }
        }
        switch type {
        case "snapshot":
            let next = Snapshot(value)
            if next.online {
                snapshot = next
            } else {
                var stale = snapshot
                stale.online = false
                stale.ready = false
                stale.error = next.error
                snapshot = stale
            }
        case "detail":
            if let message = (value["message"] as? JSONObject).flatMap(MessageDetail.init) {
                detail = message
                var next = snapshot
                next.messages = next.messages.map { row in
                    var row = row
                    if row.id == message.id { row.unread = false }
                    return row
                }
                if let unread = value["unread"] as? Int { next.unread = unread }
                snapshot = next
                listener?.engineShowed(message.id)
            }
        case "preview":
            preview = SendPreview(value)
            listener?.enginePreviewChanged()
        case "sent":
            preview = nil
            sendState = value["state"] as? String ?? "unknown"
            listener?.engineSendStateChanged()
        case "error":
            error = value["error"] as? String ?? "操作失败"
            if (value["action"] as? String) == "send" {
                sendState = ""
                listener?.engineSendStateChanged()
            }
        case "incoming":
            let messages = (value["messages"] as? [JSONObject] ?? []).compactMap(IncomingMessage.init)
            if !messages.isEmpty { listener?.engineReceived(messages) }
        default:
            break
        }
        if isResponse && (value["action"] as? String) == "mark_all_read" && type == "snapshot" {
            listener?.engineMarkedAllRead()
        }
        if isResponse && (value["action"] as? String) == "cleanup" && type == "snapshot" {
            cleaned = CleanupResult(value["cleaned"] as? JSONObject)
        }
        if isResponse && (value["action"] as? String) == "delete" && type == "snapshot",
           let id = value["deleted"] as? Int {
            listener?.engineDeleted(id)
        }
        if isResponse || type == "detail" { listener?.engineSettled() }
    }

    private func consume(_ data: Data) {
        // Output that trails a deliberate stop is dropped: nothing read from
        // the module outlives it.
        if stopping && process == nil { return }
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<newline]
            buffer.removeSubrange(buffer.startIndex...newline)
            if !line.isEmpty { receive(Data(line)) }
        }
        // A runaway line without a newline is dropped rather than buffered forever.
        if buffer.count > 4 * 1024 * 1024 { buffer.removeAll() }
    }

    // MARK: Process

    static func engineRoot() -> URL? {
        if let configured = UserDefaults.standard.string(forKey: "EngineRoot"), !configured.isEmpty {
            return URL(fileURLWithPath: (configured as NSString).expandingTildeInPath, isDirectory: true)
        }
        guard let root = Bundle.main.object(forInfoDictionaryKey: "DJI4GEngineRoot") as? String,
              !root.isEmpty else { return nil }
        return URL(fileURLWithPath: root, isDirectory: true)
    }

    func start() {
        // Started again before a stopped engine has exited: replace it as
        // soon as it is gone instead of reporting a crash.
        let exiting = stopping && process != nil
        stopping = false
        if exiting { relaunchOnExit = true } else { launch() }
    }

    /// Clears everything read from the module, keeping only an unresolved
    /// send's state so that it is still reported.
    func forget() {
        snapshot = Snapshot()
        detail = nil
        preview = nil
        cleaned = nil
        error = ""
    }

    private func launch() {
        restartTimer?.invalidate()
        restartTimer = nil
        guard process == nil, !stopping else { return }
        let manager = FileManager.default
        guard let root = Self.engineRoot() else {
            fail("未配置短信引擎目录")
            return
        }
        let python = root.appendingPathComponent(".venv/bin/python")
        let script = root.appendingPathComponent("dji4g_desktop.py")
        guard manager.isExecutableFile(atPath: python.path), manager.fileExists(atPath: script.path) else {
            fail("找不到短信引擎（\(root.path)），请检查 .venv 与 dji4g_desktop.py")
            return
        }

        let task = Process()
        task.executableURL = python
        task.arguments = ["-B", "-E", "-s", "-u", script.path, "--emit-incoming"]
        task.currentDirectoryURL = root
        let stdin = Pipe()
        let stdout = Pipe()
        task.standardInput = stdin
        task.standardOutput = stdout
        task.standardError = FileHandle.nullDevice

        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            // The main queue is FIFO, so lines are handled in arrival order.
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.consume(data) }
            }
        }
        task.terminationHandler = { [weak self] finished in
            let pid = finished.processIdentifier
            let status = finished.terminationReason == .exit ? finished.terminationStatus : -1
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.exited(pid: pid, status: status) }
            }
        }

        do {
            try task.run()
        } catch {
            stdout.fileHandleForReading.readabilityHandler = nil
            fail("短信引擎无法启动")
            return
        }
        process = task
        input = stdin.fileHandleForWriting
        buffer.removeAll()
        running = true
        error = ""
    }

    private func fail(_ message: String) {
        running = false
        var stale = snapshot
        stale.online = false
        stale.ready = false
        stale.error = message
        snapshot = stale
        error = message
        scheduleRestart()
    }

    private func scheduleRestart() {
        guard !stopping else { return }
        restartTimer?.invalidate()
        restartTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.launch() }
        }
    }

    /// Exit status of an engine that found the modem or its data owned by
    /// another engine or the CLI (dji4g_desktop.ENGINE_BUSY_EXIT).
    static let busyExit: Int32 = 75

    private func exited(pid: Int32, status: Int32) {
        guard let process, process.processIdentifier == pid else { return }
        self.process = nil
        try? input?.close()
        input = nil
        running = false
        if pendingAction == "send" {
            sendState = "unknown"
            listener?.engineSendStateChanged()
        }
        busy = false
        pendingAction = ""
        discardSensitiveReply = false
        preview = nil
        detail = nil
        var stale = snapshot
        stale.online = false
        stale.ready = false
        snapshot = stale
        let waiters = exitWaiters
        exitWaiters = []
        waiters.forEach { $0() }
        if stopping {
            forget()
            listener?.engineSettled()
            return
        }
        if relaunchOnExit {
            relaunchOnExit = false
            listener?.engineSettled()
            launch()
            return
        }
        // The busy engine's last snapshot already carries the reason.
        error = status == Self.busyExit ? "" : "本地短信引擎已停止，正在重连；不会重试发送"
        listener?.engineSettled()
        scheduleRestart()
    }

    /// Closes stdin so the engine stops its modem worker itself; terminates it
    /// only if it has not exited within `grace` seconds.
    func stop(grace: TimeInterval = 3, completion: @escaping () -> Void = {}) {
        stopping = true
        relaunchOnExit = false
        restartTimer?.invalidate()
        restartTimer = nil
        guard let process, process.isRunning else {
            completion()
            return
        }
        exitWaiters.append(completion)
        try? input?.close()
        input = nil
        let pid = process.processIdentifier
        DispatchQueue.main.asyncAfter(deadline: .now() + grace) { [weak self] in
            MainActor.assumeIsolated {
                guard let self, let process = self.process, process.processIdentifier == pid else { return }
                process.terminate()
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    MainActor.assumeIsolated {
                        let waiters = self.exitWaiters
                        self.exitWaiters = []
                        waiters.forEach { $0() }
                    }
                }
            }
        }
    }
}
