import AppKit
import SwiftUI

struct PanelView: View {
    @Bindable var model: PanelModel
    let loginItem: LoginItem

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HeroView(model: model, loginItem: loginItem)
            if !model.notices.isEmpty {
                NoticeList(model: model)
            }
            Divider()
            if model.mode == "inbox" {
                InboxSection(model: model)
            } else {
                ComposeSection(model: model)
            }
        }
        .padding(16)
        .frame(width: 380)
    }
}

private struct HeroView: View {
    let model: PanelModel
    let loginItem: LoginItem

    @State private var spin: Double = 0
    @State private var spinning = false

    var body: some View {
        HStack(spacing: 12) {
            SignalBars(
                level: model.linkUp ? (model.snap.signal ?? 0) : 0,
                searching: model.opened && model.snap.online && model.snap.connected && !model.linkUp
            )
            .foregroundStyle(.primary)
            .opacity(model.linkUp ? 1 : 0.5)

            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("短信").font(.system(size: 17, weight: .semibold))
                    if model.unread > 0 {
                        Text("\(model.unread) 未读")
                            .font(.callout.weight(.medium))
                            .foregroundStyle(Color.accentColor)
                    }
                }
                Text(model.heroMeta)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .opacity(model.metaVisible ? 1 : 0)
                    .animation(.easeOut(duration: model.metaVisible ? 0.26 : 0.18), value: model.metaVisible)
            }

            Spacer(minLength: 8)

            HStack(spacing: 2) {
                IconButton(
                    symbol: model.mode == "compose" ? "arrow.left" : "square.and.pencil",
                    help: model.mode == "compose" ? "返回收件箱 (Esc)" : "写短信 (N)", size: 26
                ) {
                    if model.mode == "compose" { model.leaveCompose() } else { model.startCompose() }
                }
                Button(action: model.refresh) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 13, weight: .medium))
                        .rotationEffect(.degrees(spin))
                        .frame(width: 26, height: 26)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("刷新 (R)")
                Menu {
                    Toggle(loginItem.title, isOn: Binding(get: { loginItem.enabled }, set: { loginItem.set($0) }))
                    if !loginItem.error.isEmpty {
                        Text(loginItem.error)
                    }
                    Button("清理模块短信…", action: model.armCleanup)
                        .disabled(!model.cleanupAvailable)
                    Divider()
                    Button("退出") { NSApp.terminate(nil) }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .frame(width: 26, height: 26)
                .help("更多")
            }
        }
        .onChange(of: model.busy) { _, busy in if busy && model.opened { startSpin() } }
        .onAppear { loginItem.refresh() }
    }

    // Spins once per request, and keeps going for a slow one.
    private func startSpin() {
        guard !spinning else { return }
        spinning = true
        withAnimation(.easeInOut(duration: 0.7)) { spin += 360 }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) {
            spinning = false
            if model.busy && model.opened { startSpin() }
        }
    }
}

private struct NoticeList: View {
    let model: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(model.notices) { notice in
                HStack(alignment: .center, spacing: 8) {
                    Image(systemName: notice.urgent ? "exclamationmark.circle" : "info.circle")
                        .foregroundStyle(notice.urgent ? Color.red : Color.secondary)
                        .frame(width: 18)
                    Text(notice.text)
                        .foregroundStyle(notice.urgent ? Color.red : Color.primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    switch notice.action {
                    case .none:
                        EmptyView()
                    case .acknowledge:
                        IconButton(symbol: "checkmark", help: "已核实，不再提示", action: model.acknowledge)
                    case .cleanup:
                        Button("清理", action: model.armCleanup)
                            .controlSize(.small)
                            .disabled(!model.canCleanup)
                            .help("删除模块里已存到本机的短信")
                    case .confirmCleanup:
                        Button("取消", action: model.cancelCleanup)
                            .controlSize(.small)
                            .help("取消 (Esc)")
                        Button("删除", action: model.confirmCleanup)
                            .controlSize(.small)
                            .buttonStyle(.borderedProminent)
                            .tint(.red)
                            .disabled(!model.canCleanup)
                    }
                }
            }
        }
    }
}

private struct SectionHeader: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.secondary)
    }
}

private struct InboxSection: View {
    let model: PanelModel

    @State private var contentHeight: CGFloat = 0

    private var listCap: CGFloat {
        let available = NSScreen.main?.visibleFrame.height ?? 800
        return max(160, min(420, available - 200))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                SectionHeader(title: "收件箱")
                Spacer()
                if model.unread > 0 {
                    IconButton(symbol: "checkmark.circle", help: "全部标为已读 (A)", action: model.markAllRead)
                }
            }
            .frame(height: 22)

            if model.messages.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "tray")
                        .font(.system(size: 26))
                        .foregroundStyle(.secondary)
                    Text(model.snap.online ? "还没有短信" : "短信引擎运行后显示短信")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 18)
            } else {
                ScrollViewReader { proxy in
                    ScrollView(.vertical) {
                        VStack(spacing: 2) {
                            ForEach(Array(model.messages.enumerated()), id: \.element.id) { index, row in
                                MessageRowView(model: model, index: index, row: row)
                                    .id(row.id)
                                    .transition(.opacity)
                            }
                        }
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { contentHeight = $0 }
                    }
                    .scrollIndicators(.automatic)
                    .frame(height: min(max(contentHeight, 1), listCap))
                    .animation(.easeOut(duration: 0.26), value: model.messages.map(\.id))
                    .onChange(of: model.scrollTick) {
                        guard let id = model.scrollTarget else { return }
                        withAnimation(.easeOut(duration: 0.16)) { proxy.scrollTo(id) }
                    }
                    .onChange(of: model.detail?.id) {
                        guard let id = model.expandedId else { return }
                        DispatchQueue.main.async {
                            withAnimation(.easeOut(duration: 0.16)) { proxy.scrollTo(id) }
                        }
                    }
                }
            }
        }
    }
}

private struct MessageRowView: View {
    let model: PanelModel
    let index: Int
    let row: MessageSummary

    @State private var hovering = false

    private var expanded: Bool { model.expandedId == row.id }
    private var message: MessageDetail? { expanded ? model.detail : nil }
    private var code: String { message.map { SmsFormat.findCode($0.body) } ?? "" }
    private var replyable: Bool { message.map { SmsFormat.isValidRecipient($0.peer) } ?? false }
    private var hasCursor: Bool { model.cursorActive && model.selectedIndex == index }

    private var marker: String {
        if !row.complete { return "ellipsis.message" }
        if row.direction == "outbound" { return "arrow.up.right" }
        return row.unread ? "message.fill" : "message"
    }

    private var fill: Color {
        if hasCursor { return Color.primary.opacity(expanded ? 0.1 : 0.08) }
        return Color.primary.opacity(expanded ? 0.05 : 0)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Only the summary line toggles, so a click next to an action
            // button does not fold the message away.
            HStack(spacing: 10) {
                Image(systemName: marker)
                    .foregroundStyle(row.unread ? Color.accentColor : Color.secondary)
                    .frame(width: 18)
                Text(message?.peer ?? row.peer)
                    .fontWeight(row.unread ? .semibold : .regular)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(expanded ? SmsFormat.fullTime(row.time) : SmsFormat.relativeTime(row.time, now: model.now))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .contentShape(Rectangle())
            .onTapGesture { model.toggleMessage(row.id) }

            if expanded {
                reveal
                    .padding(.leading, 38)
                    .padding(.trailing, 10)
                    .padding(.bottom, 10)
                    .transition(.opacity)
            }
        }
        .background(RoundedRectangle(cornerRadius: 8).fill(fill))
        .contentShape(Rectangle())
        .onHover { inside in
            if inside && !hovering { model.setCursor(index) }
            hovering = inside
        }
        .animation(.easeOut(duration: 0.16), value: expanded)
    }

    @ViewBuilder
    private var reveal: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let message {
                Text(message.body)
                    .lineSpacing(3)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !message.complete {
                    Text("长短信尚未收全")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                HStack(spacing: 2) {
                    if !code.isEmpty {
                        Button { model.copyText("code") } label: {
                            Label(code, systemImage: model.copied == "code" ? "checkmark" : "key")
                                .monospacedDigit()
                                .fontWeight(.semibold)
                        }
                        .buttonStyle(.bordered)
                        .tint(.accentColor)
                        .help("复制验证码 (C)")
                    }
                    Spacer()
                    IconButton(symbol: model.copied == "body" ? "checkmark" : "doc.on.doc", help: "复制全文") {
                        model.copyText("body")
                    }
                    if replyable {
                        IconButton(symbol: "arrowshape.turn.up.left", help: "回复") {
                            model.startCompose(message.peer)
                        }
                    }
                }
            } else if model.revealFailed {
                Button { model.reveal(row.id) } label: {
                    Text("读取失败，点击重试").foregroundStyle(.secondary).underline()
                }
                .buttonStyle(.plain)
            } else {
                Text("正在读取…").foregroundStyle(.secondary)
            }
        }
        .padding(.top, 2)
    }
}

private struct ComposeSection: View {
    @Bindable var model: PanelModel

    @State private var bodyHeight: CGFloat = 88

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionHeader(title: "写短信").frame(height: 22)

            RecipientField(
                text: $model.recipient,
                focused: model.focus == .recipient,
                editable: !model.editingLocked,
                onFocus: { model.focus = .recipient },
                onSubmit: { model.focus = .body }
            )

            BodyEditor(
                text: $model.body,
                height: $bodyHeight,
                focused: model.focus == .body,
                editable: !model.editingLocked,
                onFocus: { model.focus = .body }
            )
            .frame(height: bodyHeight)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(model.focus == .body ? Color.accentColor : Color(nsColor: .separatorColor),
                                  lineWidth: model.focus == .body ? 2 : 1)
            )

            if let pending = model.pending {
                VStack(alignment: .leading, spacing: 6) {
                    Text("发送给 \(model.recipientText)")
                        .fontWeight(.semibold)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text("\(pending.encoding == "gsm7" ? "GSM-7" : "UCS-2") · \(pending.characters) 字 · \(pending.segments) 段 · 可能产生运营商费用")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 4)
                    HoldButton(model: model)
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color(nsColor: .separatorColor)))
            } else {
                HStack(spacing: 10) {
                    Text(model.composeHint)
                        .font(.callout)
                        .foregroundStyle(model.composeProblem ? Color.red : Color.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button("预览", action: model.requestPreview)
                        .disabled(!model.canPreview)
                        .help("预览后再确认发送 (Ctrl+Enter)")
                        .overlay(
                            RoundedRectangle(cornerRadius: 6)
                                .strokeBorder(Color.accentColor, lineWidth: 2)
                                .opacity(model.focus == .preview ? 1 : 0)
                                .padding(-2)
                        )
                }
            }
        }
    }
}
