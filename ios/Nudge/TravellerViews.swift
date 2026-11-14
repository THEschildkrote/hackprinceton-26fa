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
            VStack(spacing: 24) {
                Spacer()
                Text(traveller.status)
                    .font(.largeTitle.bold())
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.white)
                if let d = traveller.distanceToNext, traveller.phase == .walking {
                    Text(fmtMetres(d)).font(.system(size: 64, weight: .heavy)).foregroundStyle(.yellow)
                }
                // a small readout for a sighted helper; the traveller never needs it
                VStack(spacing: 6) {
                    Text("\(traveller.steps) steps · \(traveller.heading.map { "\(Int($0))°" } ?? "no direction yet")")
                    if traveller.compassPoor { Text("Direction is unreliable: keep the phone upright").foregroundStyle(.orange) }
                    if !traveller.piReachable { Text("Pi not reachable").foregroundStyle(.red) }
                    if !traveller.heard.isEmpty { Text("Heard: “\(traveller.heard)”") }
                }
                .font(.callout)
                .foregroundStyle(.gray)
                Spacer()
                Button("Setup") { showSetup = true }
                    .font(.footnote)
                    .foregroundStyle(.gray)
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
                    Button("Done") { Task { await finish() } }
                        .frame(maxWidth: .infinity, minHeight: 64)
                        .font(.title2.bold())
                }
            }
            .navigationTitle("Nudge setup")
        }
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
