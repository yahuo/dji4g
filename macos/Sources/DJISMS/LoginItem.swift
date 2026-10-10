import Observation
import ServiceManagement

@Observable
@MainActor
final class LoginItem {
    private(set) var status: SMAppService.Status = .notRegistered
    var error = ""

    var enabled: Bool { status == .enabled }
    var title: String { status == .requiresApproval ? "登录时启动（需在系统设置中批准）" : "登录时启动" }

    func refresh() {
        status = SMAppService.mainApp.status
    }

    func set(_ enabled: Bool) {
        error = ""
        do {
            if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            self.error = enabled ? "无法设置登录时启动" : "无法取消登录时启动"
        }
        refresh()
        if status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
    }
}
