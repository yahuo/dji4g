import AppKit
import Observation
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private let engine = EngineClient()
    private let usb = UsbMonitor()
    private let loginItem = LoginItem()
    private lazy var model = PanelModel(engine: engine)
    private var statusItem: NSStatusItem?
    private let popover = NSPopover()
    private var notifier: SmsNotifier?
    private var keyMonitor: Any?
    private var lastClose = Date.distantPast
    private var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.imagePosition = .imageLeading
        }
        // Hidden until the module is found; it decides visibility from here on.
        item.isVisible = false
        statusItem = item

        let host = NSHostingController(rootView: PanelView(model: model, loginItem: loginItem))
        host.sizingOptions = .preferredContentSize
        popover.contentViewController = host
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self

        let notifier = SmsNotifier { [weak self] action in self?.handle(action) }
        notifier.requestAuthorization()
        self.notifier = notifier

        model.requestOpen = { [weak self] in self?.openPanel() }
        model.requestClose = { [weak self] in self?.popover.performClose(nil) }
        model.onIncoming = { [weak self] messages in messages.forEach { self?.notifier?.post($0) } }
        model.onShown = { [weak self] id in self?.notifier?.remove(id) }
        model.onAllRead = { [weak self] in self?.notifier?.removeAll() }
        model.onDeleted = { [weak self] id in self?.notifier?.remove(id) }

        usb.onChange = { [weak self] up in self?.deviceChanged(up) }
        usb.start()
        observeStatus()
        installKeyMonitor()
        loginItem.refresh()
    }

    /// The engine runs only while the module is plugged in, as on Omarchy.
    private func deviceChanged(_ up: Bool) {
        model.deviceUp = up
        if up {
            engine.start()
        } else {
            engine.stop()
            engine.forget()
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateNow }
        quitting = true
        if popover.isShown { popover.performClose(nil) }
        engine.stop { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        // Like the Omarchy IPC, nothing summons the panel while it is hidden.
        guard model.present else { return }
        for url in urls where url.scheme == "dji4g" {
            let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            func value(_ name: String) -> String? { query.first { $0.name == name }?.value }
            switch url.host {
            case "compose":
                // Prefills like an sms: link. Any page or app can open this URL,
                // so it never requests a preview by itself: the user previews,
                // then holds to send.
                model.startCompose(value("to") ?? "", body: value("body"))
                openPanel()
            case "latest":
                model.openLatest()
                openPanel()
            default:
                openPanel()
            }
        }
    }

    // MARK: Status item

    private func observeStatus() {
        withObservationTracking {
            updateStatusItem()
        } onChange: { [weak self] in
            Task { @MainActor in self?.observeStatus() }
        }
    }

    private func updateStatusItem() {
        guard let item = statusItem, let button = item.button else { return }
        // Set every time: macOS may restore a remembered visibility.
        item.isVisible = model.present
        if !model.present && popover.isShown {
            DispatchQueue.main.async { [weak self] in self?.popover.performClose(nil) }
        }
        let unread = model.unread
        let symbol = unread > 0 ? "message.badge.filled.fill" : "message"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "短信")
            ?? NSImage(systemSymbolName: "message", accessibilityDescription: "短信")
        image?.isTemplate = true
        button.image = image
        button.title = unread > 0 ? "\(unread)" : ""
        button.toolTip = model.tooltip
        button.appearsDisabled = !model.linkUp && unread == 0
    }

    @objc private func statusClicked(_ sender: Any?) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            model.startCompose()
            openPanel()
            return
        }
        if popover.isShown {
            popover.performClose(nil)
        } else if Date().timeIntervalSince(lastClose) > 0.25 {
            // A transient popover closes on the mouse-down of this very click.
            openPanel()
        }
    }

    private func openPanel() {
        guard model.present, let button = statusItem?.button else { return }
        NSApp.activate()
        if !popover.isShown {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
        popover.contentViewController?.view.window?.makeKey()
    }

    func popoverWillShow(_ notification: Notification) {
        model.didOpen()
    }

    func popoverDidClose(_ notification: Notification) {
        lastClose = Date()
        model.didClose()
    }

    // MARK: Notifications

    private func handle(_ action: NotificationAction) {
        switch action {
        case .copyCode(let code):
            Clipboard.copySensitive(code)
        case .open(let id):
            guard model.present else { return }
            model.openMessage(id)
            openPanel()
        case .reply(let peer, let text):
            guard model.present else { return }
            // Never sends: it only fills the composer and asks for a preview.
            model.startCompose(peer, body: text, preview: true)
            openPanel()
        }
    }

    // MARK: Keyboard

    private func installKeyMonitor() {
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp]) { [weak self] event in
            let consumed = MainActor.assumeIsolated {
                guard let self, self.popover.isShown, event.window === self.popover.contentViewController?.view.window
                else { return false }
                return KeyRouter.handle(event, model: self.model)
            }
            return consumed ? nil : event
        }
    }
}

/// Panel.qml's key handling: the inbox owns plain letters and arrows, while
/// the compose editors keep typing and only the chain keys are intercepted.
@MainActor
enum KeyRouter {
    private static let escape: UInt16 = 53
    private static let tab: UInt16 = 48
    private static let returnKey: UInt16 = 36
    private static let enter: UInt16 = 76
    private static let space: UInt16 = 49
    private static let left: UInt16 = 123
    private static let right: UInt16 = 124
    private static let down: UInt16 = 125
    private static let up: UInt16 = 126

    static func handle(_ event: NSEvent, model: PanelModel) -> Bool {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if modifiers.contains(.command) { return false }
        // An input method that is still composing owns Esc, Enter and arrows,
        // but only while an editor is the focused control.
        let editing = model.mode == "compose" && (model.focus == .recipient || model.focus == .body)
        if editing, let editor = event.window?.firstResponder as? NSTextView, editor.hasMarkedText() { return false }
        return model.mode == "compose" ? compose(event, model: model, modifiers: modifiers)
            : inbox(event, model: model, modifiers: modifiers)
    }

    private static func inbox(_ event: NSEvent, model: PanelModel, modifiers: NSEvent.ModifierFlags) -> Bool {
        guard event.type == .keyDown, modifiers.isDisjoint(with: [.control, .option]) else { return false }
        switch event.keyCode {
        case escape:
            model.escape()
        case up:
            model.moveCursor(-1)
        case down:
            model.moveCursor(1)
        case right:
            if model.cursorActive, model.selectedIndex < model.messages.count,
               model.expandedId != model.messages[model.selectedIndex].id {
                model.activateCursor()
            }
        case left:
            model.collapse()
        case returnKey, enter, space:
            model.activateCursor()
        case tab:
            break
        default:
            switch event.charactersIgnoringModifiers?.lowercased() {
            case "j": model.moveCursor(1)
            case "k": model.moveCursor(-1)
            case "n": model.startCompose()
            case "r": model.refresh()
            case "a": model.markAllRead()
            case "c": model.copyCurrent()
            default: return false
            }
        }
        return true
    }

    private static func compose(_ event: NSEvent, model: PanelModel, modifiers: NSEvent.ModifierFlags) -> Bool {
        let holdKey = [returnKey, enter, space].contains(event.keyCode)
        if event.type == .keyUp {
            guard model.focus == .hold, holdKey else { return false }
            if !event.isARepeat { model.releaseHold() }
            return true
        }
        let index = model.composeIndex()
        if [returnKey, enter].contains(event.keyCode), model.focus == .body, modifiers.contains(.control) {
            model.requestPreview()
            return true
        }
        switch event.keyCode {
        case escape:
            model.escape()
            return true
        case tab:
            if modifiers.contains(.shift) { model.composeFocus(index - 1, -1) } else { model.composeFocus(index + 1, 1) }
            return true
        case down where model.focus == .recipient:
            model.composeFocus(index + 1, 1)
            return true
        default:
            break
        }
        switch model.focus {
        case .preview where holdKey:
            model.requestPreview()
            return true
        case .hold where holdKey:
            // A key that was already down when focus arrived must not start a send.
            if !event.isARepeat { model.pressHold() }
            return true
        case .hold, .preview:
            // As in Panel.qml, other keys on a button are dropped rather than
            // reaching an editor that AppKit may still hold as first responder.
            return true
        default:
            return false
        }
    }
}
