import Foundation

// Values decoded from the engine's JSON lines. Every field is optional on the
// wire, so decoding never fails on a partial or older reply.

typealias JSONObject = [String: Any]

private func int(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
    return number.intValue
}

private func bool(_ value: Any?) -> Bool {
    (value as? NSNumber).map { $0.boolValue } ?? false
}

private func string(_ value: Any?) -> String {
    value as? String ?? ""
}

struct MessageSummary: Identifiable, Equatable {
    let id: Int
    let peer: String  // masked by the engine
    let time: String
    let direction: String
    var unread: Bool
    let complete: Bool

    init?(_ object: JSONObject) {
        guard let id = int(object["id"]) else { return nil }
        self.id = id
        peer = string(object["peer"])
        time = string(object["time"])
        direction = string(object["direction"])
        unread = bool(object["unread"])
        complete = bool(object["complete"])
    }
}

struct Radio: Equatable {
    var rat = ""
    var band: Int?
    var rsrp: Int?
    var sinr: Int?
}

struct Storage: Equatable {
    let used: Int
    let total: Int
    let state: String
}

struct Snapshot: Equatable {
    var online = false
    var ready = false
    var connected = false
    var simReady = false
    var registered = false
    var signal: Int?
    var roaming = false
    var radio = Radio()
    var storage: [String: Storage] = [:]
    var unread = 0
    var uncertainSends = 0
    var messages: [MessageSummary] = []
    var error = ""

    init() {}

    init(_ object: JSONObject) {
        online = bool(object["online"])
        ready = bool(object["ready"])
        connected = bool(object["connected"])
        simReady = bool(object["sim_ready"])
        registered = bool(object["registered"])
        signal = int(object["signal"])
        roaming = bool(object["roaming"])
        if let radio = object["radio"] as? JSONObject {
            self.radio = Radio(rat: string(radio["rat"]), band: int(radio["band"]),
                               rsrp: int(radio["rsrp"]), sinr: int(radio["sinr"]))
        }
        if let stores = object["storage"] as? JSONObject {
            for name in ["SM", "ME"] {
                guard let store = stores[name] as? JSONObject,
                      let used = int(store["used"]), let total = int(store["total"]) else { continue }
                storage[name] = Storage(used: used, total: total, state: string(store["state"]))
            }
        }
        unread = int(object["unread"]) ?? 0
        uncertainSends = int(object["uncertain_sends"]) ?? 0
        messages = (object["messages"] as? [JSONObject] ?? []).compactMap(MessageSummary.init)
        error = string(object["error"])
    }
}

struct MessageDetail: Equatable {
    let id: Int
    let peer: String  // full number
    let body: String
    let complete: Bool
    let time: String

    init?(_ object: JSONObject) {
        guard let id = int(object["id"]) else { return nil }
        self.id = id
        peer = string(object["peer"])
        body = string(object["body"])
        complete = bool(object["complete"])
        time = string(object["time"])
    }
}

struct SendPreview: Equatable {
    let key: String
    let encoding: String
    let characters: Int
    let segments: Int

    init?(_ object: JSONObject) {
        guard let key = object["key"] as? String, !key.isEmpty,
              let preview = object["preview"] as? JSONObject else { return nil }
        self.key = key
        encoding = string(preview["encoding"])
        characters = int(preview["characters"]) ?? 0
        segments = int(preview["segments"]) ?? 0
    }
}

struct CleanupResult: Equatable {
    let deleted: Int
    let kept: Int  // left on the module because they could not be stored

    init?(_ object: JSONObject?) {
        guard let object, let deleted = int(object["deleted"]) else { return nil }
        self.deleted = deleted
        kept = int(object["kept"]) ?? 0
    }
}

struct IncomingMessage {
    let id: Int
    let peer: String
    let body: String
    let time: String
    let complete: Bool

    init?(_ object: JSONObject) {
        guard let id = int(object["id"]) else { return nil }
        self.id = id
        peer = string(object["peer"])
        body = string(object["body"])
        time = string(object["time"])
        complete = bool(object["complete"])
    }
}
