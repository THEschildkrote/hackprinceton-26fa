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
        .animation(.smooth, value: store.paired)
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
            VStack(spacing: 28) {
                Spacer()
                Image(systemName: "figure.walk.motion")
                    .font(.system(size: 64, weight: .semibold))
                    .foregroundStyle(.tint)
                    .symbolEffect(.pulse)
                VStack(spacing: 8) {
                    Text("Follow a traveller").font(.largeTitle.bold())
                    Text("Enter the 6-digit code shown in their Nudge setup.")
                        .multilineTextAlignment(.center).foregroundStyle(.secondary)
                }
                TextField("000000", text: $code)
                    .font(.system(size: 40, weight: .bold, design: .rounded))
                    .multilineTextAlignment(.center)
                    .keyboardType(.numberPad)
                    .padding()
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18))
                    .onChange(of: code) { _, v in code = String(v.filter(\.isNumber).prefix(6)) }
                if !error.isEmpty { Text(error).foregroundStyle(.red).font(.callout) }
                Button {
                    Task { await pair() }
                } label: {
                    Label(working ? "Pairing…" : "Follow", systemImage: "link")
                        .font(.headline).frame(maxWidth: .infinity, minHeight: 54)
                }
                .buttonStyle(.borderedProminent)
                .disabled(code.count != 6 || working)
                Spacer()
            }
            .padding(24)
            .toolbar {
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityLabel("Settings")
            }
            .sheet(isPresented: $showSettings) { CareSettingsView().environment(store) }
        }
    }

    private func pair() async {
        working = true
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
            ScrollView {
                VStack(spacing: 16) {
                    if let a = store.alert { AlertBanner(message: a) }
                    StatusCard()
                    MapCard()
                    TimelineCard()
                }
                .padding()
                .animation(.smooth, value: store.alert)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle(store.displayName)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showTrip = true } label: { Label("Send trip", systemImage: "paperplane.fill") }
                }
            }
            .sheet(isPresented: $showTrip) { SendTripView().environment(store) }
            .sheet(isPresented: $showSettings) { CareSettingsView().environment(store) }
        }
    }
}

struct AlertBanner: View {
    @Environment(CareStore.self) private var store
    let message: CareMessage

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Needs attention", systemImage: "exclamationmark.triangle.fill").font(.headline)
            Text(message.text).font(.body)
            HStack {
                if let url = URL(string: "tel:\(store.callNumber.filter { $0.isNumber || $0 == "+" })"),
                   !store.callNumber.isEmpty {
                    Link(destination: url) {
                        Label("Call", systemImage: "phone.fill").frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.borderedProminent).tint(.white).foregroundStyle(.red)
                }
                Button("Dismiss") { store.dismissAlert() }
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .buttonStyle(.bordered).tint(.white)
            }
        }
        .foregroundStyle(.white)
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.red.gradient, in: RoundedRectangle(cornerRadius: 20))
        .transition(.move(edge: .top).combined(with: .opacity))
        .accessibilityElement(children: .contain)
    }
}

// state -> words, symbol, colour
private func look(_ state: String?) -> (String, String, Color) {
    switch state {
    case "walking": return ("Walking", "figure.walk", .blue)
    case "arrived": return ("Arrived", "checkmark.circle.fill", .green)
    case "help": return ("Needs help", "exclamationmark.triangle.fill", .red)
    case "unmapped": return ("Off the map", "questionmark.circle.fill", .orange)
    case "off_route", "rerouting": return ("Finding a new way", "arrow.triangle.turn.up.right.diamond.fill", .orange)
    default: return ("Not on a trip", "moon.zzz.fill", .gray)
    }
}

struct StatusCard: View {
    @Environment(CareStore.self) private var store

    var body: some View {
        let s = store.status
        let (word, symbol, colour) = look(s?.state)
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Image(systemName: symbol).font(.title).foregroundStyle(colour)
                    .symbolEffect(.bounce, value: s?.state)
                VStack(alignment: .leading) {
                    Text(word).font(.title2.bold())
                    if let goal = s?.goal, s?.state != nil, s?.state != "idle" {
                        Text("to \(goal)").foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if !store.reachable {
                    Label("Offline", systemImage: "wifi.slash").font(.caption).foregroundStyle(.orange)
                }
            }
            HStack(spacing: 12) {
                Metric(title: "Where", value: [s?.place, s?.floor].compactMap { $0 }.joined(separator: " · "), symbol: "mappin.and.ellipse")
                Metric(title: "Left", value: s?.remaining_m.map(fmtMetres) ?? "-", symbol: "ruler")
                Metric(title: "ETA", value: s?.eta_min.map { "\(Int($0)) min" } ?? "-", symbol: "clock")
            }
            if let ts = s?.last_update_ts {
                Text("Updated \(Date(timeIntervalSince1970: ts), style: .relative) ago")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let sent = store.tripSentTo {
                Label("Trip sent to \(sent): waiting for \(store.displayName) to start", systemImage: "paperplane")
                    .font(.callout).foregroundStyle(.tint)
            }
        }
        .padding()
        .background(.background, in: RoundedRectangle(cornerRadius: 20))
        .accessibilityElement(children: .combine)
    }
}

struct Metric: View {
    let title: String
    let value: String
    let symbol: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(title, systemImage: symbol).font(.caption).foregroundStyle(.secondary)
            Text(value.isEmpty ? "-" : value).font(.headline).lineLimit(2).minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
    }
}

struct MapCard: View {
    @Environment(CareStore.self) private var store

    var body: some View {
        let s = store.status
        let sid = s?.sid ?? s?.position?.sid
        VStack(alignment: .leading, spacing: 10) {
            Label("Live map", systemImage: "map").font(.headline)
            if let sid, let plan = store.plans[sid] {
                let floor = s?.position?.floor ?? plan.floors.first?.id ?? ""
                FloorPlanView(plan: plan, floor: floor, route: s?.route?.points ?? [], position: s?.position)
                    .frame(height: 320)
                    .animation(.smooth(duration: 0.6), value: s?.position)
                if let label = plan.floors.first(where: { $0.id == floor })?.label {
                    Text(label).font(.caption).foregroundStyle(.secondary)
                }
            } else {
                ContentUnavailableView("No live trip", systemImage: "map",
                                       description: Text("The map appears when \(store.displayName) starts walking."))
                    .frame(height: 220)
            }
        }
        .padding()
        .background(.background, in: RoundedRectangle(cornerRadius: 20))
    }
}

struct TimelineItem: Identifiable {
    let id: String
    let text: String
    let ts: Double?
    let urgent: Bool
}

struct TimelineCard: View {
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
        VStack(alignment: .leading, spacing: 12) {
            Label("Timeline", systemImage: "list.bullet.rectangle").font(.headline)
            if items.isEmpty {
                Text("Nothing yet.").foregroundStyle(.secondary)
            }
            ForEach(items) { item in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: symbol(for: item.text, urgent: item.urgent))
                        .foregroundStyle(item.urgent ? .red : .accentColor)
                        .frame(width: 24)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.text)
                        if let ts = item.ts {
                            Text(Date(timeIntervalSince1970: ts), style: .time).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 20))
        .animation(.smooth, value: store.messages)
    }

    private func symbol(for text: String, urgent: Bool) -> String {
        let t = text.lowercased()
        if urgent { return "exclamationmark.triangle.fill" }
        if t.contains("arrived") { return "checkmark.circle.fill" }
        if t.contains("floor") { return "stairs" }
        if t.contains("route") || t.contains("way") { return "arrow.triangle.turn.up.right.diamond" }
        if t.contains("started") { return "figure.walk" }
        return "circle.fill"
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
                        HStack {
                            Label(b.label, systemImage: "building.2.fill")
                            Spacer()
                            Button("Change") { building = nil; places = []; destination = nil }
                        }
                    } else {
                        TextField("Search an address or building", text: $query)
                            .textInputAutocapitalization(.never)
                        ForEach(buildings) { b in
                            Button { Task { await choose(b) } } label: {
                                Label(b.label, systemImage: "building.2")
                            }
                        }
                    }
                }
                if building != nil {
                    Section("Destination") {
                        if places.isEmpty { ProgressView() }
                        Picker("Go to", selection: $destination) {
                            Text("Choose…").tag(String?.none)
                            ForEach(places, id: \.self) { Text($0).tag(String?.some($0)) }
                        }
                        .pickerStyle(.navigationLink)
                    }
                    Section("Travels") {
                        Picker("Travels", selection: $profile) {
                            Label("With a cane", systemImage: "figure.walk").tag("blind")
                            Label("In a wheelchair", systemImage: "figure.roll").tag("wheelchair")
                        }
                        .pickerStyle(.inline)
                        .labelsHidden()
                    }
                }
                if !error.isEmpty { Text(error).foregroundStyle(.red) }
            }
            .navigationTitle("Send a trip")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(sending ? "Sending…" : "Send") { Task { await send() } }
                        .disabled(building == nil || destination == nil || sending)
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

    var body: some View {
        @Bindable var s = store
        NavigationStack {
            Form {
                Section {
                    TextField("http://172.20.10.2:8765", text: $s.server)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button("Test connection") {
                        Task {
                            do { try await store.api.ping(); test = "Connected." } catch { test = error.localizedDescription }
                        }
                    }
                    if !test.isEmpty { Text(test).font(.callout) }
                } header: { Text("Nudge server") } footer: {
                    Text("The Raspberry Pi running Nudge. On the traveller's iPhone hotspot it's usually 172.20.10.2–14, port 8765.")
                }
                Section("Emergency call") {
                    TextField("Phone number to call", text: $s.callNumber).keyboardType(.phonePad)
                }
                if store.paired {
                    Section {
                        Button("Stop following \(store.displayName)", role: .destructive) { store.unpair(); dismiss() }
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}
