import SwiftUI

struct CareRoot: View {
    @Environment(CareStore.self) private var store

    var body: some View {
        Group {
            if store.paired {
                HomeView()
            } else {
                PairView()
            }
        }
        .task { store.start() }
    }
}

// MARK: pairing

struct PairView: View {
    @Environment(CareStore.self) private var store
    @State private var code = ""
    @State private var error = ""
    @State private var working = false
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("6-digit code", text: $code)
                        .keyboardType(.numberPad)
                        .font(.title2.monospacedDigit())
                        .onChange(of: code) { _, v in code = String(v.filter(\.isNumber).prefix(6)) }
                } header: {
                    Text("Traveller code")
                } footer: {
                    if !error.isEmpty { Text(error).foregroundStyle(Theme.red) }
                }
                Section {
                    Button {
                        Task { await pair() }
                    } label: {
                        HStack {
                            Text("Follow")
                            if working { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(code.count != 6 || working)
                }
            }
            .navigationTitle("Follow")
            .toolbar {
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityLabel("Settings")
            }
            .sheet(isPresented: $showSettings) { CareSettingsView().environment(store) }
        }
    }

    private func pair() async {
        working = true
        error = ""
        defer { working = false }
        do {
            try await store.pair(code: code)
        } catch APIError.http(404, _) {
            error = "No traveller with that code."
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: home

struct HomeView: View {
    @Environment(CareStore.self) private var store
    @State private var showTrip = false
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            List {
                if let a = store.alert { HelpSection(message: a) }
                StatusSection()
                MapSection()
                ActivitySection()
            }
            .listStyle(.insetGrouped)
            .animation(.default, value: store.alert)
            .navigationTitle(store.displayName)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Send Trip") { showTrip = true }
                }
            }
            .sheet(isPresented: $showTrip) { SendTripView().environment(store) }
            .sheet(isPresented: $showSettings) { CareSettingsView().environment(store) }
        }
    }
}

// the only loud thing in the app
struct HelpSection: View {
    @Environment(CareStore.self) private var store
    let message: CareMessage

    private var callURL: URL? {
        let n = store.callNumber.filter { $0.isNumber || $0 == "+" }
        return n.isEmpty ? nil : URL(string: "tel:\(n)")
    }

    var body: some View {
        Section {
            VStack(alignment: .leading, spacing: 12) {
                Label("Needs help", systemImage: "exclamationmark.triangle.fill").font(.headline)
                Text(message.text)
                HStack(spacing: 12) {
                    if let url = callURL {
                        Link(destination: url) {
                            Text("Call").frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.white)
                        .foregroundStyle(Theme.red)
                    }
                    Button { store.dismissAlert() } label: {
                        Text("Dismiss").frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.bordered)
                    .tint(.white)
                }
            }
            .foregroundStyle(.white)
            .padding(.vertical, 4)
            .listRowBackground(Theme.red)
            .accessibilityElement(children: .contain)
        }
    }
}

// state -> words and a status icon. the word carries the meaning; the icon and colour only back it up.
private func stateLook(_ state: String?) -> (word: String, symbol: String, color: Color) {
    switch state {
    case "walking": return ("Walking", "figure.walk", Theme.blue)
    case "arrived": return ("Arrived", "checkmark.circle.fill", Theme.navy)
    case "help": return ("Needs help", "exclamationmark.triangle.fill", Theme.red)
    case "unmapped": return ("Off the map", "questionmark.circle.fill", Theme.navy)
    case "off_route", "rerouting": return ("Rerouting", "arrow.triangle.branch", Theme.navy)
    default: return ("Not on a trip", "pause.circle.fill", .secondary)
    }
}

struct StatusSection: View {
    @Environment(CareStore.self) private var store

    var body: some View {
        let s = store.status
        let look = stateLook(s?.state)
        let onTrip = s?.state != nil && s?.state != "idle"
        Section {
            VStack(alignment: .leading, spacing: 4) {
                Label {
                    Text(look.word).font(.title2.bold())
                } icon: {
                    Image(systemName: look.symbol).foregroundStyle(look.color)
                }
                if onTrip, let goal = s?.goal {
                    Text("to \(goal)").foregroundStyle(.secondary)
                }
                let place = [s?.place, s?.floor].compactMap { $0 }.joined(separator: ", ")
                if !place.isEmpty {
                    Text(place).font(.subheadline).foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 4)
            .accessibilityElement(children: .combine)

            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 2) {
                GridRow {
                    Text("Left"); Text("ETA"); Text("Updated")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                GridRow {
                    Text(s?.remaining_m.map(fmtMetres) ?? "-")
                    Text(s?.eta_min.map { "\(Int($0)) min" } ?? "-")
                    if let ts = s?.last_update_ts {
                        Text(Date(timeIntervalSince1970: ts), format: .dateTime.hour().minute())
                    } else {
                        Text("-")
                    }
                }
                .font(.headline)
                .monospacedDigit()
            }
            .accessibilityElement(children: .combine)

            if !store.reachable {
                Label("Offline", systemImage: "wifi.slash").foregroundStyle(Theme.red)
            }
            if let sent = store.tripSentTo {
                Text("Trip sent: \(sent)").foregroundStyle(.secondary)
            }
        }
    }
}

struct MapSection: View {
    @Environment(CareStore.self) private var store

    var body: some View {
        let s = store.status
        let sid = s?.sid ?? s?.position?.sid
        if let sid, let plan = store.plans[sid] {
            let floor = s?.position?.floor ?? plan.floors.first?.id ?? ""
            Section(plan.floors.first(where: { $0.id == floor })?.label ?? "Map") {
                FloorPlanView(plan: plan, floor: floor, route: s?.route?.points ?? [], position: s?.position)
                    .frame(height: 300)
                    .animation(.easeInOut(duration: 0.5), value: s?.position)
            }
        } else {
            Section("Map") {
                ContentUnavailableView("No trip", systemImage: "map")
                    .frame(height: 160)
            }
        }
    }
}

struct TimelineItem: Identifiable {
    let id: String
    let text: String
    let ts: Double?
    let urgent: Bool
}

struct ActivitySection: View {
    @Environment(CareStore.self) private var store

    // live feed first; before any arrives, the recent events from the status
    private var items: [TimelineItem] {
        if !store.messages.isEmpty {
            return store.messages.map { TimelineItem(id: $0.id, text: $0.text, ts: $0.ts, urgent: $0.urgent ?? false) }
        }
        return (store.status?.recent ?? []).reversed().compactMap { e in
            guard let t = e.text, !t.isEmpty else { return nil }
            return TimelineItem(id: e.id, text: t, ts: e.ts,
                                urgent: e.type == "help" || e.type == "stuck" || e.type == "unmapped")
        }
    }

    var body: some View {
        Section("Activity") {
            if items.isEmpty {
                Text("No activity").foregroundStyle(.secondary)
            }
            ForEach(items) { item in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    if let ts = item.ts {
                        Text(Date(timeIntervalSince1970: ts), format: .dateTime.hour().minute())
                            .font(.subheadline)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                    if item.urgent {
                        Label(item.text, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(Theme.red)
                    } else {
                        Text(item.text)
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }
}

// MARK: send a trip

struct SendTripView: View {
    @Environment(CareStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var buildings: [Building] = []
    @State private var building: Building?
    @State private var places: [String] = []
    @State private var destination: String?
    @State private var profile = "blind"
    @State private var error = ""
    @State private var sending = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Building") {
                    if let b = building {
                        LabeledContent(b.label) {
                            Button("Change") { building = nil; places = []; destination = nil }
                        }
                    } else {
                        TextField("Search", text: $query)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        ForEach(buildings) { b in
                            Button(b.label) { Task { await choose(b) } }
                                .foregroundStyle(.primary)
                        }
                    }
                }
                if building != nil {
                    Section {
                        if places.isEmpty {
                            ProgressView()
                        } else {
                            Picker("Destination", selection: $destination) {
                                Text("None").tag(String?.none)
                                ForEach(places, id: \.self) { Text($0).tag(String?.some($0)) }
                            }
                            .pickerStyle(.navigationLink)
                        }
                    }
                    Section("Mobility") {
                        Picker("Mobility", selection: $profile) {
                            Text("Cane").tag("blind")
                            Text("Wheelchair").tag("wheelchair")
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                    }
                }
                if !error.isEmpty {
                    Section { Text(error).foregroundStyle(Theme.red) }
                }
            }
            .navigationTitle("Send Trip")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if sending {
                        ProgressView()
                    } else {
                        Button("Send") { Task { await send() } }
                            .disabled(building == nil || destination == nil)
                    }
                }
            }
            .task(id: query) { await search() }
        }
    }

    private func search() async {
        try? await Task.sleep(nanoseconds: 250_000_000)   // debounce typing
        if let r = try? await store.api.buildings(query) { buildings = r.results }
    }

    private func choose(_ b: Building) async {
        building = b
        places = []
        if let r = try? await store.api.places(b.sid) { places = PlaceOrder.names(r.places) }
    }

    private func send() async {
        guard let b = building, let to = destination else { return }
        sending = true
        error = ""
        defer { sending = false }
        do {
            try await store.sendTrip(sid: b.sid, to: to, profile: profile)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: settings

struct CareSettingsView: View {
    @Environment(CareStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var test = ""
    @State private var testing = false

    var body: some View {
        @Bindable var s = store
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("Pi address", text: $s.server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button {
                        Task { await runTest() }
                    } label: {
                        HStack {
                            Text("Test")
                            Spacer()
                            if testing { ProgressView() } else if !test.isEmpty {
                                Text(test).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                Section("Emergency call") {
                    TextField("Phone number", text: $s.callNumber)
                        .keyboardType(.phonePad)
                }
                if store.paired {
                    Section {
                        Button("Stop Following \(store.displayName)", role: .destructive) {
                            store.unpair()
                            dismiss()
                        }
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }

    private func runTest() async {
        testing = true
        defer { testing = false }
        do {
            try await store.api.ping()
            test = "Connected"
        } catch {
            test = "Not reachable"
        }
    }
}
