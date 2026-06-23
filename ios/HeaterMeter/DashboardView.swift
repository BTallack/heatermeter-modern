//  DashboardView.swift
//  Pit hero + setpoint control, probe tiles with targets/ETAs, manual fan + lid
//  controls, and a probe target/rename editor. Live via the client's WS state.

import SwiftUI

struct DashboardView: View {
    @Environment(HeaterMeterClient.self) private var client

    @State private var pendingSetpoint: Double = 225
    @State private var meat: [MeatPreset] = []
    @State private var etas: [String: Prediction] = [:]
    @State private var manualOpen = false
    @State private var editProbe: ProbeReading?
    @State private var toast: String?

    private var status: Status? { client.state?.status }
    private var names: [String] { client.state?.probeNames ?? [] }

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if client.state == nil {
                    ProgressView("Connecting…").padding(.top, 60)
                } else {
                    pitHero
                    controlRow
                    probeGrid
                    if let m = status?.pidModeLabel {
                        Text(m).font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            .padding()
        }
        .navigationTitle("HeaterMeter")
        .overlay(alignment: .top) { toastView }
        .task {
            if meat.isEmpty { meat = (try? await client.presets().meat) ?? [] }
            if let sp = (try? await client.fetchStatus())?.status.setPoint { pendingSetpoint = sp }
            await refreshETAs()
        }
        .onChange(of: status?.setPoint) { _, sp in if let sp { pendingSetpoint = sp } }
        .onChange(of: status?.food1) { _, _ in Task { await refreshETAs() } }
        .sheet(isPresented: $manualOpen) { ManualSheet(client: client) }
        .sheet(item: $editProbe) { p in
            ProbeEditor(client: client, probe: p, meat: meat,
                        currentTarget: client.state?.target(forProbe: p.probeIndex))
        }
    }

    // MARK: Hero

    private var pitHero: some View {
        VStack(spacing: 12) {
            Text(status?.pit.map { "\(Int($0))°" } ?? "—")
                .font(.system(size: 76, weight: .bold, design: .rounded))
                .foregroundStyle(.orange)
                .contentTransition(.numericText())
            HStack(spacing: 22) {
                readout("Set", status?.setPoint)
                readout("Fan", status?.fanPct, suffix: "%")
                if status?.lidOpen == true {
                    Label("Lid", systemImage: "wind").foregroundStyle(.yellow)
                }
            }
            .font(.subheadline)

            HStack {
                Stepper("\(Int(pendingSetpoint))°", value: $pendingSetpoint, in: 100...450, step: 5)
                    .labelsHidden()
                Text("\(Int(pendingSetpoint))°").monospacedDigit().frame(width: 56)
                Button("Set") { run { try await client.setSetpoint(pendingSetpoint) } }
                    .buttonStyle(.borderedProminent).tint(.orange)
                Button("Off", role: .destructive) { run { try await client.turnOff() } }
            }
        }
        .padding()
        .frame(maxWidth: .infinity)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 18))
    }

    private var controlRow: some View {
        HStack(spacing: 12) {
            Button { manualOpen = true } label: {
                Label("Fan override", systemImage: "fanblades").frame(maxWidth: .infinity)
            }
            if status?.lidOpen == true {
                Button { run { try await client.cancelLid() } } label: {
                    Label("Cancel lid", systemImage: "xmark.circle").frame(maxWidth: .infinity)
                }
            } else {
                Button { run { try await client.openLid() } } label: {
                    Label("Lid open", systemImage: "wind").frame(maxWidth: .infinity)
                }
            }
        }
        .buttonStyle(.bordered)
        .font(.footnote)
    }

    private func readout(_ label: String, _ value: Double?, suffix: String = "°") -> some View {
        VStack {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value.map { "\(Int($0))\(suffix)" } ?? "—").monospacedDigit()
        }
    }

    // MARK: Probe tiles

    private var probeGrid: some View {
        let probes = (status?.probes(names: names) ?? []).filter { $0.channel != "pit" }
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 110))], spacing: 12) {
            ForEach(probes) { p in
                Button { editProbe = p } label: { probeTile(p) }
                    .buttonStyle(.plain)
            }
        }
    }

    private func probeTile(_ p: ProbeReading) -> some View {
        let target = client.state?.target(forProbe: p.probeIndex)
        let eta = etas[p.channel]
        return VStack(spacing: 4) {
            Text(p.name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Text(p.temp.map { "\(Int($0))°" } ?? "—")
                .font(.title2.weight(.semibold)).monospacedDigit()
                .foregroundStyle(p.connected ? .primary : .tertiary)
            if let target {
                let done = (p.temp ?? 0) >= target
                Text(done ? "→ \(Int(target))° · done"
                          : "→ \(Int(target))°" + readyText(eta))
                    .font(.caption2)
                    .foregroundStyle(done ? .green : .secondary)
            } else {
                Text("tap to set").font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 12)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
    }

    private func readyText(_ eta: Prediction?) -> String {
        guard let at = eta?.readyAt, eta?.hasETA == true else { return "" }
        return " · " + at.formatted(date: .omitted, time: .shortened)
    }

    private func refreshETAs() async {
        guard let st = client.state else { return }
        for p in st.status.probes(names: names) where p.isFood {
            if let target = st.target(forProbe: p.probeIndex), (p.temp ?? 0) < target {
                etas[p.channel] = try? await client.predict(channel: p.channel, target: target)
            } else {
                etas[p.channel] = nil
            }
        }
    }

    // MARK: helpers

    @ViewBuilder private var toastView: some View {
        if let toast {
            Text(toast).font(.footnote).padding(.horizontal, 14).padding(.vertical, 8)
                .background(.thinMaterial, in: Capsule()).padding(.top, 4)
                .transition(.move(edge: .top).combined(with: .opacity))
        }
    }

    private func run(_ op: @escaping () async throws -> Void) {
        Task {
            do { try await op() }
            catch { await flash(error.localizedDescription) }
        }
    }
    private func flash(_ msg: String) async {
        withAnimation { toast = msg }
        try? await Task.sleep(nanoseconds: 2_800_000_000)
        withAnimation { toast = nil }
    }
}

// MARK: - Manual fan sheet

private struct ManualSheet: View {
    let client: HeaterMeterClient
    @Environment(\.dismiss) private var dismiss
    @State private var pct: Double = 30

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack {
                        Slider(value: $pct, in: 0...100, step: 1)
                        Text("\(Int(pct))%").monospacedDigit().frame(width: 48)
                    }
                } footer: {
                    Text("Drives the fan directly, bypassing PID. Set a pit temperature again to return to automatic control.")
                }
            }
            .navigationTitle("Fan override")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Apply") {
                        Task { try? await client.setManualOutput(Int(pct)); dismiss() }
                    }
                }
            }
        }
        .presentationDetents([.height(220)])
    }
}

// MARK: - Probe target / rename editor

private struct ProbeEditor: View {
    let client: HeaterMeterClient
    let probe: ProbeReading
    let meat: [MeatPreset]
    let currentTarget: Double?

    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var target = ""
    @State private var presetKey = ""

    private var cats: [String] { Array(NSOrderedSet(array: meat.map(\.category)) as? [String] ?? []) }

    var body: some View {
        NavigationStack {
            Form {
                Section("Preset") {
                    Picker("Preset", selection: $presetKey) {
                        Text("Choose…").tag("")
                        ForEach(cats, id: \.self) { c in
                            Section(c) {
                                ForEach(meat.filter { $0.category == c }) { m in
                                    Text("\(m.label) (\(m.tempF)°)").tag(m.key)
                                }
                            }
                        }
                    }
                    .onChange(of: presetKey) { _, key in
                        if let m = meat.first(where: { $0.key == key }) {
                            target = String(m.tempF); name = m.label
                        }
                    }
                }
                Section("Probe") {
                    TextField("Name", text: $name)
                    TextField("Target °", text: $target).keyboardType(.numberPad)
                }
                Section {
                    Button("Save", action: save)
                    if currentTarget != nil {
                        Button("Clear target", role: .destructive) {
                            Task { try? await client.setProbeTarget(probeIndex: probe.probeIndex, target: nil); dismiss() }
                        }
                    }
                }
            }
            .navigationTitle(probe.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .onAppear {
                name = probe.name
                if let t = currentTarget { target = String(Int(t)) }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func save() {
        Task {
            if name != probe.name, !name.isEmpty {
                try? await client.setProbeName(index: probe.probeIndex, name: name)
            }
            if let t = Double(target) {
                try? await client.setProbeTarget(probeIndex: probe.probeIndex, target: t)
            }
            dismiss()
        }
    }
}
