import AppKit

enum Clipboard {
    /// Declares the nspasteboard.org concealed marker so clipboard managers
    /// keep SMS text and codes out of their history.
    static func copySensitive(_ text: String) {
        let pasteboard = NSPasteboard.general
        let concealed = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")
        pasteboard.clearContents()
        pasteboard.declareTypes([.string, concealed], owner: nil)
        pasteboard.setString(text, forType: .string)
        pasteboard.setString("", forType: concealed)
    }
}
