import SwiftUI

// setup happens once. after that the screen shows one status line; everything is spoken and the
// phone rides on the traveller's back.

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

    // one line, always the most important thing: no connection beats everything else
    private var line: String {
        let s = traveller.piReachable ? traveller.status : "Pi not reachable"
        return s.hasSuffix(".") ? String(s.dropLast()) : s
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            Text(line)
                .font(.largeTitle.bold())
                .multilineTextAlignment(.center)
                .foregroundStyle(.white)
                .padding(32)
        }
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { Task { await traveller.startLastTrip() } }
        .onLongPressGesture(minimumDuration: 2) { showSetup = true }   // for a sighted helper
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(line)
        .accessibilityAddTraits(.updatesFrequently)
        .accessibilityHint("Double tap to start your last trip.")
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
                Section("Raspberry Pi") {
                    TextField("Pi address", text: $s.server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button {
                        Task { await test() }
                    } label: {
                        HStack {
                            Text("Test")
                            Spacer()
                            if working { ProgressView() } else if !testResult.isEmpty {
                                Text(testResult).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .frame(minHeight: 44)
                }

                Section("Wear") {
                    Picker("Mount", selection: $s.mountRaw) {
                        ForEach(Mount.allCases) { Text($0.label).tag($0.rawValue) }
                    }
                    .pickerStyle(.inline)
                    .labelsHidden()
                }

                Section("Step length") {
                    Stepper(value: $s.stepM, in: 0.3...1.2, step: 0.05) {
                        Text(String(format: "%.2f m", s.stepM)).monospacedDigit()
                    }
                    HStack {
                        TextField("Metres for 10 steps", text: $metres)
                            .keyboardType(.decimalPad)
                        Button("Set") {
                            if let v = Double(metres.replacingOccurrences(of: ",", with: ".")), let m = stepLength(metres: v) {
                                s.stepM = m
                                metres = ""
                            }
                        }
                        .disabled(metres.isEmpty)
                    }
                }

                Section("Caretaker") {
                    TextField("Name", text: $s.name)
                    LabeledContent("Code") {
                        Text(s.code ?? "Not set")
                            .font(.headline.monospacedDigit())
                            .foregroundStyle(s.code == nil ? .secondary : .primary)
                    }
                    .accessibilityElement(children: .combine)
                }

                Section {
                    Button("Done") { Task { await finish() } }
                        .font(.headline)
                        .frame(maxWidth: .infinity, minHeight: 50)
                }
            }
            .navigationTitle("Setup")
        }
    }

    private func test() async {
        working = true
        defer { working = false }
        do {
            try await settings.api.ping()
            testResult = "Connected"
            await register()
        } catch {
            testResult = "Not reachable"
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
