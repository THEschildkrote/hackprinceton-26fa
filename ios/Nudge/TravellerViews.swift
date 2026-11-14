import SwiftUI

// setup happens once (a helper or the traveller with voiceover). after that the screen is one dark,
// full-screen control: everything is spoken, the phone goes on the traveller's back.

struct TravellerRoot: View {
    @Environment(TravellerSettings.self) private var settings
    @State private var traveller: Traveller?
    @State private var showSetup = false

    var body: some View {
        Group {
            if settings.setupDone, let traveller {
                MainView(traveller: traveller, showSetup: $showSetup)
            } else {
                SetupView { showSetup = false }
            }
        }
        .preferredColorScheme(.dark)
        .sheet(isPresented: $showSetup) { SetupView { showSetup = false }.environment(settings) }
        .task(id: settings.setupDone) {
            guard settings.setupDone, traveller == nil else { return }
            let t = Traveller(settings: settings)
            traveller = t
            await t.begin()
        }
    }
}

struct MainView: View {
    let traveller: Traveller
    @Binding var showSetup: Bool

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VStack(spacing: 28) {
                Wordmark()
                Spacer()
                // one big visual that says what state we are in at a glance. decorative: the whole
                // screen is a single VoiceOver element that reads the status, so this adds nothing there
                Beacon(phase: traveller.phase, reachable: traveller.piReachable,
                       distance: traveller.phase == .walking ? traveller.distanceToNext : nil)
                    .accessibilityHidden(true)
                Text(traveller.status)
                    .font(.largeTitle.bold())
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.white)
                // a small readout for a sighted helper; the traveller never needs it
                VStack(spacing: 8) {
                    Text("\(traveller.steps) steps, \(traveller.heading.map { "heading \(Int($0))°" } ?? "no direction yet")")
                        .foregroundStyle(.white.opacity(0.7))
                    if traveller.compassPoor {
                        Label("Direction is unreliable: keep the phone upright", systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(Theme.yellow)
                    }
                    if !traveller.piReachable {
                        // red text is only 3.7:1 on black, so a warning is white on a red pill instead
                        Text("Pi not reachable").font(.callout.weight(.semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 14).padding(.vertical, 6)
                            .background(Theme.red, in: Capsule())
                    }
                    if !traveller.heard.isEmpty { Text("Heard: \(traveller.heard)").foregroundStyle(.white.opacity(0.7)) }
                }
                .font(.callout)
                Spacer()
                Button("Setup") { showSetup = true }
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.white.opacity(0.7))
                    .frame(minWidth: 88, minHeight: 44)
                    .accessibilityHint("Server, step length and how the phone is worn")
            }
            .padding()
        }
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { Task { await traveller.startLastTrip() } }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(traveller.status)
        .accessibilityHint("Double tap to start your last trip. Or say where am I, repeat, how far, or I need help.")
        .accessibilityAction(named: "Start last trip") { Task { await traveller.startLastTrip() } }
        .accessibilityAction(named: "Stop trip") { traveller.stopTrip() }
        .accessibilityAction(named: "Setup") { showSetup = true }
    }
}

private struct Wordmark: View {
    var body: some View {
        HStack(spacing: 10) {
            Text("Nudge").font(.title3.weight(.heavy)).foregroundStyle(Theme.yellow)
            HStack(spacing: 4) {
                Capsule().fill(Theme.red)
                Capsule().fill(.white)
                Capsule().fill(Theme.sky)
            }
            .frame(width: 40, height: 6)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityHidden(true)
    }
}

// the screen is almost empty on purpose, so the one thing on it has to carry the state:
// a ring that breathes while we wait or walk, goes solid white on arrival, and turns red when the
// Pi is lost. the colour is never the only signal; the status line under it says the same in words.
private struct Beacon: View {
    let phase: Traveller.Phase
    let reachable: Bool
    let distance: Double?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var colour: Color {
        if !reachable { return Theme.red }     // 3.7:1 on black: fine for a graphic, never for text
        switch phase {
        case .starting, .calibrating, .walking: return Theme.yellow
        case .arrived: return .white
        case .waiting: return Theme.sky
        case .stopped: return .white.opacity(0.5)
        }
    }

    private var symbol: String {
        switch phase {
        case .waiting: return "figure.walk.motion"
        case .starting: return "location.fill"
        case .calibrating: return "scope"
        case .walking: return "figure.walk"
        case .arrived: return "checkmark"
        case .stopped: return "pause.fill"
        }
    }

    private var breathes: Bool { reachable && !reduceMotion && (phase == .waiting || phase == .walking) }

    var body: some View {
        ZStack {
            if breathes {
                Circle().stroke(colour.opacity(0.45), lineWidth: 4)
                    .phaseAnimator([false, true]) { v, on in
                        v.scaleEffect(on ? 1.22 : 1).opacity(on ? 0 : 1)
                    } animation: { _ in .easeOut(duration: 2.0) }
            }
            Circle().stroke(colour, lineWidth: 12)
            if let distance {
                Text(fmtMetres(distance))
                    .font(.system(size: 56, weight: .heavy, design: .rounded))
                    .foregroundStyle(Theme.yellow)
                    .minimumScaleFactor(0.5).lineLimit(1)
                    .padding(.horizontal, 28)
            } else {
                Image(systemName: symbol)
                    .font(.system(size: 72, weight: .semibold))
                    .foregroundStyle(colour)
            }
        }
        .frame(width: 240, height: 240)
        .animation(.smooth, value: phase)
    }
}

struct SetupView: View {
    @Environment(TravellerSettings.self) private var settings
    var done: () -> Void
    @State private var testResult = ""
    @State private var metres = ""
    @State private var working = false

    var body: some View {
        @Bindable var s = settings
        NavigationStack {
            Form {
                Section {
                    TextField("http://172.20.10.2:8765", text: $s.server)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityLabel("Pi address")
                    Button(working ? "Testing…" : "Test connection") { Task { await test() } }
                        .frame(minHeight: 44)
                    if !testResult.isEmpty { Text(testResult).accessibilityAddTraits(.updatesFrequently) }
                } header: { Text("Raspberry Pi") } footer: {
                    Text("On iPhone hotspot the Pi is usually 172.20.10.2–14, port 8765.")
                }

                Section("How the phone is worn") {
                    Picker("Mount", selection: $s.mountRaw) {
                        ForEach(Mount.allCases) { Text($0.label).tag($0.rawValue) }
                    }
                    .pickerStyle(.inline)
                    Text("Wear it upright, top edge up, in a back pocket or a backpack pouch.").font(.footnote)
                }

                Section {
                    Stepper("Step length \(String(format: "%.2f", s.stepM)) m", value: $s.stepM, in: 0.3...1.2, step: 0.05)
                    HStack {
                        TextField("Metres for 10 steps", text: $metres).keyboardType(.decimalPad)
                        Button("Set") {
                            if let v = Double(metres.replacingOccurrences(of: ",", with: ".")), let m = stepLength(metres: v) {
                                s.stepM = m
                                metres = ""
                            }
                        }
                    }
                } header: { Text("Walking") } footer: {
                    Text("Walk 10 normal steps, measure how far you went, and enter it.")
                }

                Section("Caretaker") {
                    TextField("Your first name (optional)", text: $s.name)
                    if let code = s.code {
                        Text("Your caretaker enters code \(code) in Nudge Care, or texts follow \(code) to the Nudge iMessage line.")
                            .font(.headline)
                    } else {
                        Text("A code appears here once the Pi is reachable.")
                    }
                }

                Section {
                    // still a real Button. yellow with navy text is 10.5:1, and it is the one thing
                    // on this screen that has to be found and pressed.
                    Button("Done") { Task { await finish() } }
                        .buttonStyle(PrimaryButtonStyle())
                        .listRowBackground(Color.clear)
                        .listRowInsets(EdgeInsets())
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.navy)
            .navigationTitle("Nudge setup")
            .navigationBarTitleDisplayMode(.inline)
            .brandBar()
        }
        .tint(Theme.yellow)
        .preferredColorScheme(.dark)
    }

    private func test() async {
        working = true
        defer { working = false }
        do {
            try await settings.api.ping()
            testResult = "Connected to the Pi."
            await register()
        } catch {
            testResult = error.localizedDescription
        }
        UIAccessibility.post(notification: .announcement, argument: testResult)
    }

    private func register() async {
        if let r = try? await settings.api.register(traveller: settings.traveller, name: settings.name),
           let code = r.code {
            settings.code = code
        }
    }

    private func finish() async {
        if settings.code == nil { await register() }
        settings.setupDone = true
        done()
    }
}
