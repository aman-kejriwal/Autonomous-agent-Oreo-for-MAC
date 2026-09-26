#!/usr/bin/env swift
// oreo_ui.swift – Liquid-glass voice HUD for oreo.
//
// A glass island drops from the menu bar with a living orb inside, the screen edges glow in the
// orb's colours, and replies surface word by word. Colour and motion follow the conversation:
//   listening – calm blue breathing        hearing  – bright cyan, reacts like a voice meter
//   thinking  – iridescent fast swirl      speaking – warm magenta/orange pulse
//
// Protocol (newline-delimited commands on stdin):
//   SHOW                  – drop the HUD down from the menu bar
//   HIDE                  – lift it back up and out
//   LOADING               – show the thinking state
//   DONE                  – stop the thinking state
//   TEXT:<message>        – set the transcript text
//   RESPONSE:<message>    – set the response text (revealed word by word)
//   STATE:<mode>          – listening | hearing | thinking | speaking (overrides inferred states)
//   QUIT                  – exit the process
//
// Output (stdout): HOTKEY when the activation shortcut is pressed, HOTKEY_FAILED if it can't be
// registered (another app owns it or the spec is invalid).
//
// Environment: OREO_HUD_GLOW=0 turns off the screen-edge glow. OREO_HOTKEY sets the
// activation shortcut, e.g. "option+space" (default), "control+option+m"; "off" disables it.
//
// Build:  swiftc -O -target arm64-apple-macos14.0 oreo_ui.swift -o oreo_ui

import AppKit
import Carbon.HIToolbox
import SwiftUI

// ---------------------------------------------------------------------------
// MARK: – Modes and their look
// ---------------------------------------------------------------------------

enum Mode: String {
    case listening, hearing, thinking, speaking
}

/// Everything the orb, glass and edge glow draw from; blended between modes.
struct Look {
    var colors: [SIMD3<Double>]  // exactly 4
    var speed: Double  // swirl rate
    var energy: Double  // how hard the "voice" pulses
    var glow: Double  // edge-glow / inner-light strength

    static func of(_ mode: Mode) -> Look {
        switch mode {
        case .listening:
            return Look(colors: [hex(0x64D2FF), hex(0x0A84FF), hex(0x5E5CE6), hex(0x40C8E0)],
                        speed: 0.45, energy: 0.18, glow: 0.45)
        case .hearing:
            return Look(colors: [hex(0x63E6E2), hex(0x40C8E0), hex(0x0A84FF), hex(0x7D7AFF)],
                        speed: 1.0, energy: 0.85, glow: 0.85)
        case .thinking:
            return Look(colors: [hex(0xBF5AF2), hex(0xFF6482), hex(0x0A84FF), hex(0xFF9F0A)],
                        speed: 2.1, energy: 0.35, glow: 0.7)
        case .speaking:
            return Look(colors: [hex(0xFF375F), hex(0xFF9F0A), hex(0xBF5AF2), hex(0xFF6BD6)],
                        speed: 1.1, energy: 1.0, glow: 0.95)
        }
    }

    func mixed(with other: Look, _ k: Double) -> Look {
        Look(colors: zip(colors, other.colors).map { $0 + ($1 - $0) * k },
             speed: speed + (other.speed - speed) * k,
             energy: energy + (other.energy - energy) * k,
             glow: glow + (other.glow - glow) * k)
    }

    func color(_ i: Int, _ opacity: Double = 1) -> Color {
        let c = colors[i % colors.count]
        return Color(red: c.x, green: c.y, blue: c.z).opacity(opacity)
    }
}

func hex(_ v: Int) -> SIMD3<Double> {
    SIMD3(Double((v >> 16) & 0xFF) / 255, Double((v >> 8) & 0xFF) / 255, Double(v & 0xFF) / 255)
}

func clamp01(_ x: Double) -> Double { min(max(x, 0), 1) }
func easeOut(_ x: Double) -> Double { 1 - pow(1 - clamp01(x), 3) }

/// A fake-but-organic voice level (0…~1.2): layered sines that read like syllables.
func voiceLevel(_ t: Double, energy: Double) -> Double {
    let a = sin(t * 9.1) * 0.5 + 0.5
    let b = sin(t * 5.3 + 1.7) * 0.5 + 0.5
    let c = sin(t * 13.7 + 0.4) * 0.5 + 0.5
    return 0.12 + energy * pow(a * b * 0.7 + c * 0.3, 1.3)
}

/// Integrates swirl speed over time so speed changes never make the motion jump.
final class Phase {
    private var value = 0.0
    private var last = 0.0

    func advance(to t: Double, speed: Double) -> Double {
        if last == 0 { last = t }
        value += min(max(t - last, 0), 0.1) * speed
        last = t
        return value
    }
}

// ---------------------------------------------------------------------------
// MARK: – Observable state
// ---------------------------------------------------------------------------

class HUDState: ObservableObject {
    @Published var visible = false
    @Published var loading = false
    @Published var transcript = ""
    @Published var response = ""
    @Published var responseStart = 0.0
    @Published var mode = Mode.listening
    @Published var topInset: CGFloat = 32
    @Published var cornerRadius: CGFloat = 0

    let glowEnabled = ProcessInfo.processInfo.environment["OREO_HUD_GLOW"] != "0"
    let phase = Phase()

    private var stateDriven = false  // once STATE: arrives, the agent owns the mode
    private var fromLook = Look.of(.listening)
    private var modeChanged = 0.0

    func look(at t: Double) -> Look {
        fromLook.mixed(with: .of(mode), easeOut((t - modeChanged) / 0.8))
    }

    func setMode(_ m: Mode, explicit: Bool) {
        if explicit { stateDriven = true } else if stateDriven { return }
        guard m != mode else { return }
        let now = Date().timeIntervalSinceReferenceDate
        fromLook = look(at: now)
        modeChanged = now
        mode = m
    }
}

// ---------------------------------------------------------------------------
// MARK: – Root view: edge glow + island
// ---------------------------------------------------------------------------

struct RootView: View {
    @ObservedObject var state: HUDState

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60, paused: !state.visible)) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            let look = state.look(at: t)
            let phase = state.phase.advance(to: t, speed: look.speed)
            let level = voiceLevel(t, energy: look.energy)

            ZStack(alignment: .top) {
                if state.glowEnabled {
                    EdgeGlow(look: look, phase: phase, level: level, radius: state.cornerRadius)
                        .opacity(state.visible ? 1 : 0)
                        .animation(.easeInOut(duration: state.visible ? 0.5 : 0.8), value: state.visible)
                }

                if state.visible {
                    Island(state: state, look: look, phase: phase, level: level, t: t)
                        .padding(.top, state.topInset + 10)
                        .transition(.asymmetric(
                            insertion: .modifier(active: Drop(progress: 0), identity: Drop(progress: 1)),
                            removal: .modifier(active: Drop(progress: 0), identity: Drop(progress: 1))
                                .animation(.smooth(duration: 0.4))))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .animation(.spring(response: 0.55, dampingFraction: 0.68), value: state.visible)
        .environment(\.colorScheme, .dark)
    }
}

/// Pop-down: grows out of the menu bar like a droplet.
struct Drop: ViewModifier {
    var progress: Double

    func body(content: Content) -> some View {
        content
            .scaleEffect(x: 0.35 + 0.65 * progress, y: 0.2 + 0.8 * progress, anchor: .top)
            .offset(y: -36 * (1 - progress))
            .blur(radius: 14 * (1 - progress))
            .opacity(progress)
    }
}

// ---------------------------------------------------------------------------
// MARK: – Island
// ---------------------------------------------------------------------------

struct Island: View {
    @ObservedObject var state: HUDState
    let look: Look
    let phase: Double
    let level: Double
    let t: Double

    private var idle: Bool { state.transcript.isEmpty && state.response.isEmpty && !state.loading }
    private var thinking: Bool { (state.loading || state.mode == .thinking) && state.response.isEmpty }

    var body: some View {
        let radius: CGFloat = 30
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        let expanded = !idle

        CapWidth(max: 560) {
            HStack(alignment: .center, spacing: 14) {
                Orb(look: look, phase: phase, level: level)
                    .frame(width: expanded ? 50 : 34, height: expanded ? 50 : 34)

                VStack(alignment: .leading, spacing: 7) {
                    if idle {
                        Shimmer(text: label, t: t, font: .system(size: 15, weight: .semibold, design: .rounded))
                            .transition(.blurFade)
                    }
                    if !state.transcript.isEmpty {
                        Text(state.transcript)
                            .font(.system(size: 16, weight: .semibold, design: .rounded))
                            .foregroundStyle(.white)
                            .lineLimit(3)
                            .fixedSize(horizontal: false, vertical: true)
                            .contentTransition(.interpolate)
                            .transition(.blurFade)
                    }
                    if thinking {
                        Shimmer(text: "Thinking", t: t, font: .system(size: 13, weight: .medium, design: .rounded))
                            .transition(.blurFade)
                    }
                    if !state.response.isEmpty {
                        WordReveal(text: state.response, start: state.responseStart, t: t)
                            .transition(.blurFade)
                    }
                }
                                .padding(.trailing, 8)
            }
            .padding(.leading, expanded ? 14 : 11)
            .padding(.trailing, 16)
            .padding(.vertical, expanded ? 14 : 11)
            .background {
                // The orb lights the glass from the inside.
                ZStack {
                    RadialGradient(colors: [look.color(0, 0.42 * look.glow), .clear],
                                   center: UnitPoint(x: 0.02, y: 0.5), startRadius: 0, endRadius: 190)
                    RadialGradient(colors: [look.color(2, 0.22 * look.glow), .clear],
                                   center: UnitPoint(x: 0.95, y: 1.1), startRadius: 0, endRadius: 220)
                }
                .blendMode(.plusLighter)
                .clipShape(shape)
            }
            .glassSurface(shape)
            .overlay { TravelingRim(look: look, phase: phase, shape: shape) }
            .shadow(color: look.color(1, 0.35 * look.glow), radius: 26, y: 10)
            .shadow(color: .black.opacity(0.35), radius: 18, y: 8)
        }
        .animation(.spring(response: 0.5, dampingFraction: 0.78), value: state.transcript)
        .animation(.spring(response: 0.5, dampingFraction: 0.78), value: state.response)
        .animation(.spring(response: 0.5, dampingFraction: 0.78), value: state.loading)
        .animation(.spring(response: 0.5, dampingFraction: 0.78), value: state.mode)
    }

    private var label: String {
        switch state.mode {
        case .listening: return "Listening"
        case .hearing: return "Go ahead, I'm listening"
        case .thinking: return "Thinking"
        case .speaking: return "Speaking"
        }
    }
}

extension AnyTransition {
    static var blurFade: AnyTransition {
        .modifier(active: BlurFade(p: 0), identity: BlurFade(p: 1))
    }
}

struct BlurFade: ViewModifier {
    var p: Double
    func body(content: Content) -> some View {
        content.opacity(p).blur(radius: 8 * (1 - p)).offset(y: 6 * (1 - p))
    }
}

extension View {
    /// Real Liquid Glass on macOS 26+, a dark vibrancy panel before that.
    @ViewBuilder
    func glassSurface(_ shape: RoundedRectangle) -> some View {
        if #available(macOS 26.0, *) {
            self.glassEffect(.regular.tint(Color.black.opacity(0.32)), in: shape)
        } else {
            self.background(ZStack { VisualEffectBlur(); Color.black.opacity(0.3) }.clipShape(shape))
                .overlay(shape.strokeBorder(.white.opacity(0.18), lineWidth: 0.75))
        }
    }
}

/// Two sparks of the orb's colours running around the glass edge.
struct TravelingRim: View {
    let look: Look
    let phase: Double
    let shape: RoundedRectangle

    var body: some View {
        let gradient = AngularGradient(
            stops: [
                .init(color: .clear, location: 0),
                .init(color: look.color(0, 0.95), location: 0.09),
                .init(color: .clear, location: 0.22),
                .init(color: .clear, location: 0.5),
                .init(color: look.color(3, 0.8), location: 0.61),
                .init(color: .clear, location: 0.74),
                .init(color: .clear, location: 1),
            ],
            center: .center, angle: .radians(phase * 0.9))
        ZStack {
            shape.strokeBorder(gradient, lineWidth: 1.4)
            shape.strokeBorder(gradient, lineWidth: 5).blur(radius: 6).opacity(0.7 * look.glow)
        }
        .blendMode(.plusLighter)
        .allowsHitTesting(false)
    }
}

// ---------------------------------------------------------------------------
// MARK: – Orb
// ---------------------------------------------------------------------------

struct Orb: View {
    let look: Look
    let phase: Double
    let level: Double

    var body: some View {
        Canvas { ctx, size in
            let rect = CGRect(origin: .zero, size: size)
            let r = size.width / 2
            let center = CGPoint(x: r, y: r)
            ctx.clip(to: Path(ellipseIn: rect))

            // Deep base so the colours have something to glow against.
            let deep = look.colors[2] * 0.28
            ctx.fill(Path(ellipseIn: rect), with: .color(Color(red: deep.x, green: deep.y, blue: deep.z)))

            // Four drifting colour blobs, additive, heavily blurred → liquid.
            ctx.drawLayer { layer in
                layer.addFilter(.blur(radius: r * 0.24))
                layer.blendMode = .plusLighter
                for i in 0..<4 {
                    let fi = Double(i)
                    let dir = i.isMultiple(of: 2) ? 1.0 : -1.0
                    let angle = phase * (0.8 + 0.25 * fi) * dir + fi * .pi / 2
                    let dist = r * (0.48 + 0.16 * sin(phase * 0.7 + fi * 1.3)) * (0.9 + 0.25 * level)
                    let br = r * (0.58 + 0.14 * sin(phase * 1.1 + fi)) * (0.9 + 0.25 * level)
                    let p = CGPoint(x: center.x + cos(angle) * dist, y: center.y + sin(angle) * dist)
                    layer.fill(Path(ellipseIn: CGRect(x: p.x - br, y: p.y - br, width: br * 2, height: br * 2)),
                               with: .color(look.color(i, 0.8)))
                }
            }

            // Bright core that swells with the voice.
            let core = r * (0.1 + 0.18 * level)
            ctx.drawLayer { layer in
                layer.addFilter(.blur(radius: r * 0.25))
                layer.blendMode = .plusLighter
                layer.fill(Path(ellipseIn: CGRect(x: center.x - core, y: center.y - core, width: core * 2, height: core * 2)),
                           with: .color(.white.opacity(0.3)))
            }

            // Glass-marble specular highlight and rim.
            ctx.fill(Path(ellipseIn: rect), with: .radialGradient(
                Gradient(colors: [.white.opacity(0.4), .white.opacity(0.0)]),
                center: CGPoint(x: r * 0.6, y: r * 0.38), startRadius: 0, endRadius: r * 0.45))
            ctx.stroke(Path(ellipseIn: rect.insetBy(dx: 0.5, dy: 0.5)), with: .linearGradient(
                Gradient(colors: [.white.opacity(0.6), .white.opacity(0.05), .white.opacity(0.25)]),
                startPoint: .zero, endPoint: CGPoint(x: size.width, y: size.height)), lineWidth: 1)
        }
        .scaleEffect(1 + 0.08 * level)
        .background {
            Circle().fill(look.color(0, 0.9)).blur(radius: 14).opacity(0.55 * look.glow)
                .scaleEffect(1.1 + 0.25 * level)
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: – Screen-edge glow
// ---------------------------------------------------------------------------

struct EdgeGlow: View {
    let look: Look
    let phase: Double
    let level: Double
    let radius: CGFloat

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        let colors = look.colors.indices.map { look.color($0) } + [look.color(0)]
        let gradient = AngularGradient(colors: colors, center: .center, angle: .radians(phase * 0.5))
        let breathe = 0.7 + 0.3 * level

        ZStack(alignment: .top) {
            shape.strokeBorder(gradient, lineWidth: 3)
            shape.strokeBorder(gradient, lineWidth: 14).blur(radius: 10)
            shape.strokeBorder(gradient, lineWidth: 40 + 34 * level).blur(radius: 38)
            // Light spilling down from the menu bar behind the island.
            Ellipse()
                .fill(RadialGradient(colors: [look.color(0), look.color(2, 0.5), .clear],
                                     center: .center, startRadius: 0, endRadius: 380))
                .frame(width: 900, height: 240 + 60 * level)
                .offset(y: -110)
                .blur(radius: 40)
                .opacity(0.55)
        }
        .opacity(look.glow * breathe)
        .drawingGroup()
        .ignoresSafeArea()
        .allowsHitTesting(false)
    }
}

// ---------------------------------------------------------------------------
// MARK: – Text effects
// ---------------------------------------------------------------------------

/// Text with a light sweep passing through it.
struct Shimmer: View {
    let text: String
    let t: Double
    let font: Font

    var body: some View {
        let p = (t * 0.6).truncatingRemainder(dividingBy: 1.4) - 0.2
        Text(text)
            .font(font)
            .foregroundStyle(LinearGradient(
                stops: [
                    .init(color: .white.opacity(0.62), location: 0),
                    .init(color: .white.opacity(0.62), location: clamp01(p - 0.18)),
                    .init(color: .white, location: clamp01(p)),
                    .init(color: .white.opacity(0.62), location: clamp01(p + 0.18)),
                    .init(color: .white.opacity(0.62), location: 1),
                ],
                startPoint: .leading, endPoint: .trailing))
    }
}

/// Reply text whose words rise out of a blur one after another.
struct WordReveal: View {
    let text: String
    let start: Double
    let t: Double

    var body: some View {
        let words = Array(text.split(separator: " ").prefix(70).map(String.init))
        Flow(spacing: 4.5, lineSpacing: 3) {
            ForEach(Array(words.enumerated()), id: \.offset) { i, word in
                let p = easeOut((t - start - Double(i) * 0.045) / 0.38)
                Text(word)
                    .font(.system(size: 14, weight: .regular, design: .rounded))
                    .foregroundStyle(.white.opacity(0.88))
                    .opacity(p)
                    .blur(radius: 6 * (1 - p))
                    .offset(y: 7 * (1 - p))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: – Layout helpers
// ---------------------------------------------------------------------------

/// Proposes at most `max` width and hugs whatever the child needs (so the island fits its text).
struct CapWidth: Layout {
    var max: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        subviews.first?.sizeThatFits(ProposedViewSize(width: min(proposal.width ?? max, max), height: nil)) ?? .zero
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: bounds.origin, proposal: ProposedViewSize(bounds.size))
    }
}

/// Wraps children onto lines like text.
struct Flow: Layout {
    var spacing: CGFloat
    var lineSpacing: CGFloat

    private func rows(_ width: CGFloat, _ subviews: Subviews) -> [(items: [(Int, CGSize)], width: CGFloat, height: CGFloat)] {
        var rows: [(items: [(Int, CGSize)], width: CGFloat, height: CGFloat)] = []
        var items: [(Int, CGSize)] = []
        var x: CGFloat = 0
        var h: CGFloat = 0
        for (i, sv) in subviews.enumerated() {
            let s = sv.sizeThatFits(.unspecified)
            if !items.isEmpty && x + spacing + s.width > width {
                rows.append((items, x, h))
                items = []
                x = 0
                h = 0
            }
            x += (items.isEmpty ? 0 : spacing) + s.width
            h = Swift.max(h, s.height)
            items.append((i, s))
        }
        if !items.isEmpty { rows.append((items, x, h)) }
        return rows
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rs = rows(proposal.width ?? .infinity, subviews)
        let w = rs.map(\.width).max() ?? 0
        let h = rs.map(\.height).reduce(0, +) + lineSpacing * CGFloat(Swift.max(rs.count - 1, 0))
        return CGSize(width: w, height: h)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in rows(bounds.width, subviews) {
            var x = bounds.minX
            for (i, s) in row.items {
                subviews[i].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
                x += s.width + spacing
            }
            y += row.height + lineSpacing
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: – NSVisualEffectView wrapper (fallback before macOS 26)
// ---------------------------------------------------------------------------

struct VisualEffectBlur: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.blendingMode = .behindWindow
        v.material = .hudWindow
        v.state = .active
        v.isEmphasized = true
        return v
    }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

// ---------------------------------------------------------------------------
// MARK: – Borderless, transparent, click-through full-screen panel
// ---------------------------------------------------------------------------

class HUDPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

let hudState = HUDState()

// Disable the Dock icon for this helper.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let panel = HUDPanel(
    contentRect: NSRect(x: 0, y: 0, width: 800, height: 600),
    styleMask: [.nonactivatingPanel, .borderless],
    backing: .buffered,
    defer: false
)
panel.isOpaque = false
panel.backgroundColor = .clear
panel.hasShadow = false
panel.level = .statusBar
panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
panel.hidesOnDeactivate = false
panel.isMovableByWindowBackground = false
panel.ignoresMouseEvents = true

let hostingView = NSHostingView(rootView: RootView(state: hudState))
hostingView.sizingOptions = []
hostingView.autoresizingMask = [.width, .height]
panel.contentView = hostingView

/// Cover the active screen; the island sits just under its menu bar / notch.
func placeOnActiveScreen() {
    guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
    panel.setFrame(screen.frame, display: true)
    hudState.topInset = max(screen.frame.maxY - screen.visibleFrame.maxY, screen.safeAreaInsets.top)
    // Built-in notched displays have rounded corners; external ones are square.
    hudState.cornerRadius = screen.safeAreaInsets.top > 0 ? 14 : 0
}

placeOnActiveScreen()
panel.orderFrontRegardless()

NotificationCenter.default.addObserver(
    forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
) { _ in placeOnActiveScreen() }

// ---------------------------------------------------------------------------
// MARK: – Global activation shortcut (Carbon hotkey: works from any app, needs no permission)
// ---------------------------------------------------------------------------

func emit(_ line: String) {
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
}

let functionKeys: [Int] = [kVK_F1, kVK_F2, kVK_F3, kVK_F4, kVK_F5, kVK_F6, kVK_F7, kVK_F8, kVK_F9, kVK_F10, kVK_F11, kVK_F12]

let keyCodes: [String: Int] = {
    var m: [String: Int] = [
        "space": kVK_Space, "return": kVK_Return, "enter": kVK_Return, "tab": kVK_Tab,
        "escape": kVK_Escape, "esc": kVK_Escape, "`": kVK_ANSI_Grave, "grave": kVK_ANSI_Grave,
        "/": kVK_ANSI_Slash, "slash": kVK_ANSI_Slash, ".": kVK_ANSI_Period, ",": kVK_ANSI_Comma,
        ";": kVK_ANSI_Semicolon, "'": kVK_ANSI_Quote, "\\": kVK_ANSI_Backslash,
    ]
    let letters: [Int] = [
        kVK_ANSI_A, kVK_ANSI_B, kVK_ANSI_C, kVK_ANSI_D, kVK_ANSI_E, kVK_ANSI_F, kVK_ANSI_G, kVK_ANSI_H,
        kVK_ANSI_I, kVK_ANSI_J, kVK_ANSI_K, kVK_ANSI_L, kVK_ANSI_M, kVK_ANSI_N, kVK_ANSI_O, kVK_ANSI_P,
        kVK_ANSI_Q, kVK_ANSI_R, kVK_ANSI_S, kVK_ANSI_T, kVK_ANSI_U, kVK_ANSI_V, kVK_ANSI_W, kVK_ANSI_X,
        kVK_ANSI_Y, kVK_ANSI_Z,
    ]
    for (i, c) in "abcdefghijklmnopqrstuvwxyz".enumerated() { m[String(c)] = letters[i] }
    let digits: [Int] = [
        kVK_ANSI_0, kVK_ANSI_1, kVK_ANSI_2, kVK_ANSI_3, kVK_ANSI_4, kVK_ANSI_5, kVK_ANSI_6, kVK_ANSI_7,
        kVK_ANSI_8, kVK_ANSI_9,
    ]
    for (i, k) in digits.enumerated() { m[String(i)] = k }
    for (i, k) in functionKeys.enumerated() { m["f\(i + 1)"] = k }
    return m
}()

/// "option+space" → (key code, Carbon modifier mask); nil when the spec can't be read.
func parseHotkey(_ spec: String) -> (UInt32, UInt32)? {
    var mods: UInt32 = 0
    var key: Int?
    for part in spec.lowercased().split(separator: "+").map({ $0.trimmingCharacters(in: .whitespaces) }) {
        switch part {
        case "cmd", "command", "⌘": mods |= UInt32(cmdKey)
        case "opt", "option", "alt", "⌥": mods |= UInt32(optionKey)
        case "ctrl", "control", "⌃": mods |= UInt32(controlKey)
        case "shift", "⇧": mods |= UInt32(shiftKey)
        default:
            guard key == nil, let k = keyCodes[part] else { return nil }
            key = k
        }
    }
    // A bare key would fire while typing; only function keys may go without a modifier.
    guard let key, mods != 0 || functionKeys.contains(key) else { return nil }
    return (UInt32(key), mods)
}

var hotKeyRef: EventHotKeyRef?

func registerHotkey() {
    let spec = ProcessInfo.processInfo.environment["OREO_HOTKEY"] ?? "option+space"
    if spec.lowercased() == "off" || spec.isEmpty { return }
    guard let (code, mods) = parseHotkey(spec) else {
        FileHandle.standardError.write("oreo_ui: can't read OREO_HOTKEY=\(spec)\n".data(using: .utf8)!)
        emit("HOTKEY_FAILED")
        return
    }
    var pressed = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
        emit("HOTKEY")
        return noErr
    }, 1, &pressed, nil, nil)
    let id = EventHotKeyID(signature: OSType(0x4D42_5257), id: 1)  // 'MBRW'
    let status = RegisterEventHotKey(code, mods, id, GetApplicationEventTarget(), 0, &hotKeyRef)
    if status != noErr {
        FileHandle.standardError.write("oreo_ui: shortcut \(spec) is taken (\(status))\n".data(using: .utf8)!)
        emit("HOTKEY_FAILED")
    }
}

registerHotkey()

// ---------------------------------------------------------------------------
// MARK: – stdin command reader (background thread → main-thread dispatch)
// ---------------------------------------------------------------------------

DispatchQueue.global(qos: .userInitiated).async {
    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        DispatchQueue.main.async {
            if trimmed == "SHOW" {
                if !hudState.visible { placeOnActiveScreen() }
                hudState.response = ""
                hudState.visible = true
            } else if trimmed == "HIDE" {
                hudState.visible = false
                // Clear text after the lift-out finishes
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                    if !hudState.visible {
                        hudState.transcript = ""
                        hudState.response = ""
                        hudState.loading = false
                        hudState.setMode(.listening, explicit: false)
                    }
                }
            } else if trimmed == "LOADING" {
                hudState.loading = true
                hudState.setMode(.thinking, explicit: false)
            } else if trimmed == "DONE" {
                hudState.loading = false
                hudState.setMode(.listening, explicit: false)
            } else if trimmed.hasPrefix("TEXT:") {
                hudState.transcript = String(trimmed.dropFirst(5))
                hudState.response = ""
                hudState.setMode(.hearing, explicit: false)
            } else if trimmed.hasPrefix("RESPONSE:") {
                hudState.loading = false
                hudState.responseStart = Date().timeIntervalSinceReferenceDate
                hudState.response = String(trimmed.dropFirst(9))
                hudState.setMode(.speaking, explicit: false)
            } else if trimmed.hasPrefix("STATE:"), let m = Mode(rawValue: String(trimmed.dropFirst(6))) {
                hudState.setMode(m, explicit: true)
            } else if trimmed == "QUIT" {
                NSApp.terminate(nil)
            }
        }
    }
    // stdin closed → exit
    DispatchQueue.main.async { NSApp.terminate(nil) }
}

// Run the AppKit event loop.
NSApp.run()
