#!/usr/bin/env swift
// macbrow_ui.swift – Siri-style pop-down HUD for macbrow.
//
// Protocol (newline-delimited commands on stdin):
//   SHOW                  – animate the HUD down from top-center
//   HIDE                  – animate it back up and out
//   LOADING               – show a pulsing spinner/indicator
//   DONE                  – stop the spinner
//   TEXT:<message>         – set the transcript text
//   RESPONSE:<message>     – set the response text
//   QUIT                  – exit the process

import AppKit
import SwiftUI

// ---------------------------------------------------------------------------
// MARK: – Observable state
// ---------------------------------------------------------------------------

class HUDState: ObservableObject {
    @Published var visible = false
    @Published var loading = false
    @Published var transcript = ""
    @Published var response = ""
}

// ---------------------------------------------------------------------------
// MARK: – SwiftUI view
// ---------------------------------------------------------------------------

struct HUDView: View {
    @ObservedObject var state: HUDState

    var body: some View {
        VStack(spacing: 8) {
            // ── Default / idle listening state ──
            if state.transcript.isEmpty && !state.loading && state.response.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: "waveform")
                        .foregroundColor(.white.opacity(0.75))
                        .font(.system(size: 13, weight: .semibold))
                    Text("Listening…")
                        .font(.system(size: 13, weight: .medium, design: .rounded))
                        .foregroundColor(.white.opacity(0.85))
                }
                .transition(.opacity)
            }

            // ── Transcript (what the user said) ──
            if !state.transcript.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: "mic.fill")
                        .foregroundColor(.white.opacity(0.8))
                        .font(.system(size: 13, weight: .semibold))
                    Text(state.transcript)
                        .font(.system(size: 14, weight: .medium, design: .rounded))
                        .foregroundColor(.white)
                        .lineLimit(3)
                        .multilineTextAlignment(.center)
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            }

            // ── Loading indicator ──
            if state.loading {
                HStack(spacing: 6) {
                    PulsingDots()
                    Text("Thinking…")
                        .font(.system(size: 12, weight: .medium, design: .rounded))
                        .foregroundColor(.white.opacity(0.65))
                }
                .transition(.opacity)
            }

            // ── Response (what the agent said back) ──
            if !state.response.isEmpty {
                Text(state.response)
                    .font(.system(size: 13, weight: .regular, design: .rounded))
                    .foregroundColor(.white.opacity(0.9))
                    .lineLimit(4)
                    .multilineTextAlignment(.center)
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
        .frame(minWidth: 200, maxWidth: 440)
        .background(
            ZStack {
                // Dark blurred glass
                VisualEffectBlur()
                // Gradient tint
                LinearGradient(
                    colors: [
                        Color(red: 0.12, green: 0.12, blue: 0.18).opacity(0.88),
                        Color(red: 0.08, green: 0.08, blue: 0.14).opacity(0.92)
                    ],
                    startPoint: .top,
                    endPoint: .bottom
                )
                // Subtle border glow
                RoundedRectangle(cornerRadius: 22)
                    .strokeBorder(
                        LinearGradient(
                            colors: [.white.opacity(0.28), .white.opacity(0.06)],
                            startPoint: .top,
                            endPoint: .bottom
                        ),
                        lineWidth: 0.75
                    )
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 22))
        .shadow(color: .black.opacity(0.45), radius: 24, y: 8)
        // ── Pop-down / pop-up animation ──
        .offset(y: state.visible ? 0 : -140)
        .opacity(state.visible ? 1 : 0)
        .scaleEffect(state.visible ? 1 : 0.88, anchor: .top)
        .animation(.spring(response: 0.4, dampingFraction: 0.76), value: state.visible)
        .animation(.easeInOut(duration: 0.2), value: state.transcript)
        .animation(.easeInOut(duration: 0.2), value: state.response)
        .animation(.easeInOut(duration: 0.2), value: state.loading)
    }
}

// ---------------------------------------------------------------------------
// MARK: – Pulsing dots animation
// ---------------------------------------------------------------------------

struct PulsingDots: View {
    @State private var active = false

    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<3) { i in
                Circle()
                    .fill(Color.white.opacity(0.7))
                    .frame(width: 6, height: 6)
                    .scaleEffect(active ? 1.0 : 0.4)
                    .opacity(active ? 1.0 : 0.3)
                    .animation(
                        .easeInOut(duration: 0.5)
                            .repeatForever(autoreverses: true)
                            .delay(Double(i) * 0.15),
                        value: active
                    )
            }
        }
        .onAppear { active = true }
    }
}

// ---------------------------------------------------------------------------
// MARK: – NSVisualEffectView wrapper (real macOS blur)
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
// MARK: – Borderless, transparent panel
// ---------------------------------------------------------------------------

class HUDPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

// ---------------------------------------------------------------------------
// MARK: – App setup
// ---------------------------------------------------------------------------

let hudState = HUDState()

// Disable the Dock icon for this helper.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

// Create the hosting view.
let hostingView = NSHostingView(rootView: HUDView(state: hudState))
hostingView.translatesAutoresizingMaskIntoConstraints = false

let panelWidth: CGFloat = 460
let panelHeight: CGFloat = 200

// Build a borderless, transparent panel.
let panel = HUDPanel(
    contentRect: NSRect(x: 0, y: 0, width: panelWidth, height: panelHeight),
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

// Add the SwiftUI view to the panel.
let container = NSView(frame: panel.contentView!.bounds)
container.translatesAutoresizingMaskIntoConstraints = false
panel.contentView!.addSubview(container)
NSLayoutConstraint.activate([
    container.topAnchor.constraint(equalTo: panel.contentView!.topAnchor),
    container.bottomAnchor.constraint(equalTo: panel.contentView!.bottomAnchor),
    container.leadingAnchor.constraint(equalTo: panel.contentView!.leadingAnchor),
    container.trailingAnchor.constraint(equalTo: panel.contentView!.trailingAnchor),
])
container.addSubview(hostingView)
NSLayoutConstraint.activate([
    hostingView.centerXAnchor.constraint(equalTo: container.centerXAnchor),
    hostingView.topAnchor.constraint(equalTo: container.topAnchor),
    hostingView.widthAnchor.constraint(lessThanOrEqualTo: container.widthAnchor),
])

// Position at top-center of the main screen right below menu bar.
if let screen = NSScreen.main {
    let screenVisible = screen.visibleFrame
    let sx = screen.frame.midX - (panelWidth / 2)
    let sy = screenVisible.maxY - panelHeight
    panel.setFrame(NSRect(x: sx, y: sy, width: panelWidth, height: panelHeight), display: true)
}
panel.orderFrontRegardless()

// ---------------------------------------------------------------------------
// MARK: – stdin command reader (background thread → main-thread dispatch)
// ---------------------------------------------------------------------------

DispatchQueue.global(qos: .userInitiated).async {
    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        DispatchQueue.main.async {
            if trimmed == "SHOW" {
                hudState.response = ""
                hudState.visible = true
            } else if trimmed == "HIDE" {
                hudState.visible = false
                // Clear text after animation finishes
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    if !hudState.visible {
                        hudState.transcript = ""
                        hudState.response = ""
                        hudState.loading = false
                    }
                }
            } else if trimmed == "LOADING" {
                hudState.loading = true
            } else if trimmed == "DONE" {
                hudState.loading = false
            } else if trimmed.hasPrefix("TEXT:") {
                hudState.transcript = String(trimmed.dropFirst(5))
            } else if trimmed.hasPrefix("RESPONSE:") {
                hudState.loading = false
                hudState.response = String(trimmed.dropFirst(9))
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
