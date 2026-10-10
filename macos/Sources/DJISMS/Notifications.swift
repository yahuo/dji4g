import Foundation
import UserNotifications

enum NotificationAction: Sendable {
    case open(id: Int)
    case copyCode(String)
    case reply(peer: String, text: String)
}

/// Posts one banner per received SMS with the full sender and text (the user
/// chose Messages-style notifications), plus copy-code and reply actions.
final class SmsNotifier: NSObject, UNUserNotificationCenterDelegate, @unchecked Sendable {
    static let codeReplyCategory = "sms.code.reply"
    static let codeCategory = "sms.code"
    static let replyCategory = "sms.reply"
    static let copyAction = "copy-code"
    static let replyAction = "reply"

    private let center = UNUserNotificationCenter.current()
    private let handler: @MainActor @Sendable (NotificationAction) -> Void

    init(handler: @escaping @MainActor @Sendable (NotificationAction) -> Void) {
        self.handler = handler
        super.init()
        center.delegate = self
        let reply = UNTextInputNotificationAction(
            identifier: Self.replyAction, title: "回复", options: [],
            textInputButtonTitle: "预览", textInputPlaceholder: "短信内容")
        let copy = UNNotificationAction(identifier: Self.copyAction, title: "复制验证码", options: [])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: Self.codeReplyCategory, actions: [copy, reply], intentIdentifiers: []),
            UNNotificationCategory(identifier: Self.codeCategory, actions: [copy], intentIdentifiers: []),
            UNNotificationCategory(identifier: Self.replyCategory, actions: [reply], intentIdentifiers: []),
        ])
    }

    func requestAuthorization() {
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func identifier(_ id: Int) -> String { "sms-\(id)" }

    func post(_ message: IncomingMessage) {
        let content = UNMutableNotificationContent()
        content.title = message.peer.isEmpty ? "新短信" : message.peer
        content.body = message.complete ? message.body : message.body + "\n（长短信尚未收全）"
        content.sound = .default
        content.threadIdentifier = message.peer
        // Reply is offered only to a number the panel could send to, as in
        // the inbox row; alphanumeric senders cannot receive an SMS.
        let code = !SmsFormat.findCode(message.body).isEmpty
        let replyable = SmsFormat.isValidRecipient(message.peer)
        content.categoryIdentifier = code
            ? (replyable ? Self.codeReplyCategory : Self.codeCategory)
            : (replyable ? Self.replyCategory : "")
        content.userInfo = ["id": message.id, "peer": message.peer]
        center.add(UNNotificationRequest(identifier: Self.identifier(message.id), content: content, trigger: nil))
    }

    func remove(_ id: Int) {
        center.removeDeliveredNotifications(withIdentifiers: [Self.identifier(id)])
    }

    func removeAll() {
        center.removeAllDeliveredNotifications()
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse
    ) async {
        let content = response.notification.request.content
        let id = (content.userInfo["id"] as? NSNumber)?.intValue
        let peer = content.userInfo["peer"] as? String ?? ""
        let action: NotificationAction?
        switch response.actionIdentifier {
        case Self.copyAction:
            let code = SmsFormat.findCode(content.body)
            action = code.isEmpty ? nil : .copyCode(code)
        case Self.replyAction:
            let text = (response as? UNTextInputNotificationResponse)?.userText ?? ""
            action = .reply(peer: peer, text: text)
        case UNNotificationDefaultActionIdentifier:
            action = id.map { .open(id: $0) }
        default:
            action = nil
        }
        if let action { await handler(action) }
    }
}
