import Foundation

// Ports of the Omarchy panel's text helpers (deploy/omarchy/dji4g.sms/Panel.qml).
enum SmsFormat {
    static let recipientPattern = try! NSRegularExpression(pattern: "^\\+?[0-9]{3,15}$")

    static func isValidRecipient(_ text: String) -> Bool {
        let range = NSRange(text.startIndex..., in: text)
        return recipientPattern.firstMatch(in: text, range: range) != nil
    }

    // MARK: Time

    private static let fraction = try! NSRegularExpression(pattern: "\\.\\d+")
    private static let zoned = try! NSRegularExpression(pattern: "(Z|[+-]\\d{2}:?\\d{2})$")

    /// Same inputs as Panel.qml's parseTime: ISO text with an optional zone
    /// (none means local time) and an optional fractional second.
    static func parseTime(_ iso: String) -> Date? {
        guard !iso.isEmpty else { return nil }
        var text = iso.replacingOccurrences(of: " ", with: "T")
        text = fraction.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "")
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        if zoned.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil {
            formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ssXXXXX"
        } else if text.count == 10 {
            // A bare date is UTC midnight in JavaScript's Date.
            formatter.dateFormat = "yyyy-MM-dd"
            formatter.timeZone = TimeZone(identifier: "UTC")
        } else {
            formatter.dateFormat = text.count == 16 ? "yyyy-MM-dd'T'HH:mm" : "yyyy-MM-dd'T'HH:mm:ss"
            formatter.timeZone = .current
        }
        return formatter.date(from: text)
    }

    private static func format(_ date: Date, _ pattern: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = pattern
        return formatter.string(from: date)
    }

    static func relativeTime(_ iso: String, now: Date) -> String {
        guard let date = parseTime(iso) else { return "" }
        let age = now.timeIntervalSince(date)
        if age < 60 { return "刚刚" }
        if age < 3600 { return "\(Int(age / 60)) 分钟前" }
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: now)
        let clock = format(date, "HH:mm")
        if date >= today { return clock }
        if date >= today.addingTimeInterval(-86400) { return "昨天 " + clock }
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        if parts.year == calendar.component(.year, from: today) {
            return "\(parts.month ?? 0)月\(parts.day ?? 0)日"
        }
        return "\(parts.year ?? 0)/\(parts.month ?? 0)/\(parts.day ?? 0)"
    }

    static func fullTime(_ iso: String) -> String {
        parseTime(iso).map { format($0, "yyyy-MM-dd HH:mm") } ?? ""
    }

    // MARK: Verification code

    private static let codeKeyword = try! NSRegularExpression(
        pattern: "验证码|校验码|动态码|动态密码|确认码|授权码|取件码|提取码|verification|passcode|code|otp",
        options: [.caseInsensitive])
    private static let digits = try! NSRegularExpression(pattern: "[0-9]{4,8}")

    /// A code is only offered when the text says it is one; the closest number
    /// after that word wins, so order numbers and phone numbers are not picked.
    static func findCode(_ body: String) -> String {
        guard !body.isEmpty else { return "" }
        let text = body as NSString
        let all = NSRange(location: 0, length: text.length)
        guard let keyword = codeKeyword.firstMatch(in: body, range: all) else { return "" }
        func isWordUnit(_ index: Int) -> Bool {
            guard index >= 0, index < text.length else { return false }
            let unit = text.character(at: index)
            return (48...57).contains(unit) || (65...90).contains(unit) || (97...122).contains(unit)
        }
        var best = ""
        var bestScore = Int.max
        for match in digits.matches(in: body, range: all) {
            let start = match.range.location
            let end = start + match.range.length
            if isWordUnit(start - 1) || isWordUnit(end) { continue }
            let at = keyword.range.location
            let score = start >= at ? start - at : at - start + 40
            if score < bestScore {
                bestScore = score
                best = text.substring(with: match.range)
            }
        }
        return best
    }

    // MARK: Segment estimate

    // Mirrors dji4g_sms.py so the counter matches what the engine will send.
    private static let gsmBasic = Set(
        ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
            + "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà").utf16)
    private static let gsmExtension = Set("\u{0C}^{}\\[~]|€".utf16)

    struct Estimate {
        let encoding: String
        let units: Int
        let segments: Int
        let capacity: Int
    }

    static func estimate(_ text: String) -> Estimate {
        let codeUnits = Array(text.utf16)
        var units: [Int] = []
        var gsm = true
        for unit in codeUnits {
            if gsmExtension.contains(unit) { units.append(2) }
            else if gsmBasic.contains(unit) { units.append(1) }
            else { gsm = false; break }
        }
        if !gsm {
            units = []
            var index = 0
            while index < codeUnits.count {
                let unit = codeUnits[index]
                if (0xD800...0xDBFF).contains(unit) && index + 1 < codeUnits.count {
                    units.append(2)
                    index += 2
                } else {
                    units.append(1)
                    index += 1
                }
            }
        }
        let total = units.reduce(0, +)
        let single = gsm ? 160 : 70
        let multi = gsm ? 153 : 67
        var segments = total > 0 ? 1 : 0
        if total > single {
            var used = 0
            for unit in units {
                if used > 0 && used + unit > multi {
                    segments += 1
                    used = 0
                }
                used += unit
            }
        }
        return Estimate(encoding: gsm ? "GSM-7" : "UCS-2", units: total, segments: segments,
                        capacity: segments > 1 ? segments * multi : single)
    }
}
