import Foundation
import IOKit
import IOKit.usb

/// Follows the Baiwang module (2ca3:4006) the way the Omarchy panel follows
/// dev-dji4g.device: `onChange` reports whether it is plugged in, once at
/// `start()` and again whenever that changes.
@MainActor
final class UsbMonitor {
    static let vendor = 0x2CA3
    static let product = 0x4006

    var onChange: ((Bool) -> Void)?
    private(set) var present = false
    private var count = 0
    private var port: IONotificationPortRef?
    private var added: io_iterator_t = 0
    private var removed: io_iterator_t = 0

    func start() {
        guard port == nil, let port = IONotificationPortCreate(kIOMainPortDefault) else { return }
        self.port = port
        // Common modes, so a plug event is not held back while a menu tracks.
        CFRunLoopAddSource(CFRunLoopGetMain(), IONotificationPortGetRunLoopSource(port).takeUnretainedValue(),
                           .commonModes)
        let context = Unmanaged.passUnretained(self).toOpaque()
        // Removal first: a module that leaves while arrivals are being armed
        // is then still reported. Draining an iterator arms it, and the
        // arrival drain also counts a module that was plugged in before launch.
        IOServiceAddMatchingNotification(port, kIOTerminatedNotification, Self.matching(), { context, iterator in
            MainActor.assumeIsolated { UsbMonitor.from(context)?.drain(iterator, by: -1) }
        }, context, &removed)
        _ = Self.count(removed)
        IOServiceAddMatchingNotification(port, kIOFirstMatchNotification, Self.matching(), { context, iterator in
            MainActor.assumeIsolated { UsbMonitor.from(context)?.drain(iterator, by: 1) }
        }, context, &added)
        drain(added, by: 1, initial: true)
    }

    private func drain(_ iterator: io_iterator_t, by sign: Int, initial: Bool = false) {
        count = max(0, count + sign * Self.count(iterator))
        let now = count > 0
        if now == present && !initial { return }
        present = now
        onChange?(now)
    }

    private static func count(_ iterator: io_iterator_t) -> Int {
        var n = 0
        while case let service = IOIteratorNext(iterator), service != 0 {
            IOObjectRelease(service)
            n += 1
        }
        return n
    }

    private static func matching() -> CFDictionary {
        let dict = IOServiceMatching("IOUSBHostDevice") as NSMutableDictionary
        dict["idVendor"] = vendor
        dict["idProduct"] = product
        return dict
    }

    private nonisolated static func from(_ context: UnsafeMutableRawPointer?) -> UsbMonitor? {
        context.map { Unmanaged<UsbMonitor>.fromOpaque($0).takeUnretainedValue() }
    }
}
