import AppKit
import SwiftUI

// AppKit editors, so focus follows the panel model exactly and macOS text
// substitutions (smart quotes, dashes) cannot silently switch an SMS to UCS-2.

@MainActor
private func ownsFocus(_ view: NSView) -> Bool {
    guard let window = view.window else { return false }
    if let field = view as? NSTextField, let editor = field.currentEditor() {
        return window.firstResponder === editor
    }
    return window.firstResponder === view
}

@MainActor
private func syncFocus(_ view: NSView, focused: Bool) {
    DispatchQueue.main.async {
        guard let window = view.window else { return }
        let owns = ownsFocus(view)
        if focused && !owns { window.makeFirstResponder(view) }
        else if !focused && owns { window.makeFirstResponder(nil) }
    }
}

final class FocusTextField: NSTextField {
    var onFocus: (() -> Void)?

    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted { onFocus?() }
        return accepted
    }
}

struct RecipientField: NSViewRepresentable {
    @Binding var text: String
    let focused: Bool
    let editable: Bool
    let onFocus: () -> Void
    let onSubmit: () -> Void

    func makeNSView(context: Context) -> FocusTextField {
        let field = FocusTextField()
        field.placeholderString = "收件号码"
        field.bezelStyle = .roundedBezel
        field.font = .systemFont(ofSize: NSFont.systemFontSize)
        field.usesSingleLineMode = true
        field.lineBreakMode = .byClipping
        field.cell?.isScrollable = true
        field.isAutomaticTextCompletionEnabled = false
        field.delegate = context.coordinator
        return field
    }

    func updateNSView(_ field: FocusTextField, context: Context) {
        context.coordinator.parent = self
        field.onFocus = onFocus
        if field.stringValue != text { field.stringValue = text }
        field.isEditable = editable
        syncFocus(field, focused: focused)
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: RecipientField

        init(parent: RecipientField) { self.parent = parent }

        func controlTextDidChange(_ notification: Notification) {
            guard let field = notification.object as? NSTextField else { return }
            MainActor.assumeIsolated {
                parent.text = field.stringValue
                if field.stringValue != parent.text { field.stringValue = parent.text }
            }
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
            MainActor.assumeIsolated { parent.onSubmit() }
            return true
        }
    }
}

final class FocusTextView: NSTextView {
    var onFocus: (() -> Void)?
    var placeholder = ""

    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted { onFocus?() }
        return accepted
    }

    // Drawn here rather than overlaid, so it hides while an input method is
    // still composing (marked text is already part of the string).
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty else { return }
        let padding = textContainer?.lineFragmentPadding ?? 0
        let origin = NSPoint(x: textContainerInset.width + padding, y: textContainerInset.height)
        (placeholder as NSString).draw(at: origin, withAttributes: [
            .font: font ?? NSFont.systemFont(ofSize: NSFont.systemFontSize),
            .foregroundColor: NSColor.placeholderTextColor,
        ])
    }
}

struct BodyEditor: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    let focused: Bool
    let editable: Bool
    let onFocus: () -> Void

    static let inset = NSSize(width: 4, height: 6)

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true

        let view = FocusTextView(usingTextLayoutManager: false)
        view.isRichText = false
        view.importsGraphics = false
        view.allowsUndo = true
        view.drawsBackground = false
        view.font = .systemFont(ofSize: NSFont.systemFontSize)
        view.textContainerInset = Self.inset
        view.isAutomaticQuoteSubstitutionEnabled = false
        view.isAutomaticDashSubstitutionEnabled = false
        view.isAutomaticTextReplacementEnabled = false
        view.isAutomaticSpellingCorrectionEnabled = false
        view.isAutomaticTextCompletionEnabled = false
        view.isContinuousSpellCheckingEnabled = false
        view.smartInsertDeleteEnabled = false
        view.minSize = .zero
        view.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        view.isVerticallyResizable = true
        view.isHorizontallyResizable = false
        view.autoresizingMask = [.width]
        view.textContainer?.widthTracksTextView = true
        view.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        view.placeholder = "短信内容"
        view.delegate = context.coordinator
        scroll.documentView = view
        context.coordinator.view = view
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let view = scroll.documentView as? FocusTextView else { return }
        view.onFocus = onFocus
        if view.string != text {
            view.string = text
            context.coordinator.measure()
        }
        view.isEditable = editable
        syncFocus(view, focused: focused)
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: BodyEditor
        weak var view: NSTextView?

        init(parent: BodyEditor) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard let view = notification.object as? NSTextView else { return }
            MainActor.assumeIsolated {
                parent.text = view.string
                view.needsDisplay = true
                measure()
            }
        }

        @MainActor
        func measure() {
            guard let view, let manager = view.layoutManager, let container = view.textContainer else { return }
            manager.ensureLayout(for: container)
            let content = manager.usedRect(for: container).height + BodyEditor.inset.height * 2
            let next = max(88, min(190, ceil(content)))
            if abs(next - parent.height) > 0.5 {
                DispatchQueue.main.async { self.parent.height = next }
            }
        }
    }
}
