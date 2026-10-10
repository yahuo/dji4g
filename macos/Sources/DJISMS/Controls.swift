import SwiftUI

/// Four ascending bars; sweeps while the module is still looking for a cell.
struct SignalBars: View {
    let level: Int
    let searching: Bool
    var size: CGFloat = 26

    var body: some View {
        if searching {
            TimelineView(.periodic(from: .now, by: 0.26)) { context in
                bars(lit: Int(context.date.timeIntervalSinceReferenceDate / 0.26) % 5)
            }
        } else {
            bars(lit: level)
        }
    }

    private func bars(lit: Int) -> some View {
        let spacing = max(2, (size / 10).rounded())
        let width = max(2, ((size - spacing * 3) / 4).rounded())
        let height = (size * 0.72).rounded()
        return HStack(alignment: .bottom, spacing: spacing) {
            ForEach(0..<4, id: \.self) { index in
                RoundedRectangle(cornerRadius: 1)
                    .frame(width: width, height: (height * CGFloat(index + 1) / 4).rounded())
                    .opacity(index < lit ? 1 : 0.22)
                    .animation(.easeInOut(duration: 0.18), value: lit)
            }
        }
        .padding(.bottom, (size * 0.14).rounded())
        .frame(width: size, height: size, alignment: .bottom)
    }
}

/// A borderless icon button with a tooltip, like the panel kit's action button.
struct IconButton: View {
    let symbol: String
    let help: String
    var size: CGFloat = 24
    let action: () -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .medium))
                .frame(width: size, height: size)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(hovering ? 0.1 : 0)))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(help)
        .onHover { hovering = $0 }
    }
}

/// Sending costs money and cannot be undone, so it takes a deliberate hold
/// rather than a click: releasing early, or leaving the button, cancels.
struct HoldButton: View {
    let model: PanelModel

    @State private var progress: CGFloat = 0
    @State private var pressed = false
    @State private var hovering = false

    private var enabled: Bool { model.canHold }
    private var focused: Bool { model.focus == .hold }
    private var label: String {
        !model.snap.ready ? "模块未就绪，暂时无法发送" : model.holding ? "继续按住…" : "按住发送"
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 7)
                    .fill(Color.primary.opacity(hovering && enabled ? 0.09 : 0.05))
                Rectangle()
                    .fill(Color.accentColor.opacity(0.45))
                    .frame(width: geometry.size.width * progress)
                HStack(spacing: 6) {
                    Image(systemName: "paperplane.fill")
                    Text(label).fontWeight(.semibold)
                }
                .frame(maxWidth: .infinity)
            }
            .clipShape(RoundedRectangle(cornerRadius: 7))
            .overlay(
                RoundedRectangle(cornerRadius: 7)
                    .strokeBorder(focused ? Color.accentColor : Color(nsColor: .separatorColor),
                                  lineWidth: focused ? 2 : 1)
            )
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        let inside = CGRect(origin: .zero, size: geometry.size).contains(value.location)
                        if !pressed {
                            pressed = true
                            if inside { model.pressHold() }
                        } else if !inside {
                            model.releaseHold()
                        }
                    }
                    .onEnded { _ in
                        pressed = false
                        model.releaseHold()
                    }
            )
        }
        .frame(height: 36)
        .opacity(enabled ? 1 : 0.5)
        .allowsHitTesting(enabled)
        .onHover { hovering = $0 }
        .onChange(of: model.holding) { _, holding in
            if holding {
                withAnimation(.linear(duration: 0.9)) { progress = 1 }
            } else {
                withAnimation(.easeOut(duration: 0.18)) { progress = 0 }
            }
        }
        .onChange(of: enabled) { _, enabled in if !enabled { model.releaseHold() } }
        .onChange(of: focused) { _, focused in if !focused { model.releaseHold() } }
        .onDisappear { model.releaseHold() }
        .help("按住约 1 秒发送；松开即取消")
    }
}
