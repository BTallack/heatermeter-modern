//  SettingsView.swift
//  Controls: probe names/offsets, PID (presets + auto-tune), smart lid recovery,
//  temperature unit, integration status, and connection.

import SwiftUI

struct SettingsView: View {
    @Environment(HeaterMeterClient.self) private var client
    var onDisconnect: () -> Void

    @State private var toast: String?

    var body: some View {
        Form {
            ProbesSection(client: client)
            PIDSection(client: client)
            LidRecoverySection(client: client)
            UnitsSection(client: client)
            IntegrationsSection(client: client)
            AboutSection(client: client, onDisconnect: onDisconnect)
        }
        .navigationTitle("Settings")
        .overlay(alignment: .top) { if let toast { ToastPill(toast) } }
    }
}

// MARK: Probes

private struct ProbesSection: View {
    let client: HeaterMeterClient
    @State private var names = ["", "", "", ""]
    @State private var offsets = ["", "", "", ""]
    @State private var loaded = false

    var body: some View {
        Section("Probes") {
            ForEach(0..<4, id: \.self) { i in
                HStack {
                    TextField(defaultName(i), text: $names[i])
                    TextField("offset", text: $offsets[i])
                        .keyboardType(.numbersAndPunctuation)
                        .multilineTextAlignment(.trailing).frame(width: 70)
                }
            }
            Button("Save probes") { save() }
        }
        .onAppear {
            guard !loaded, let n = client.state?.probeNames else { return }
            for i in 0..<min(4, n.count) { names[i] = n[i] }
            loaded = true
        }
    }

    private func defaultName(_ i: Int) -> String { ["Pit", "Food 1", "Food 2", "Ambient"][i] }

    private func save() {
        Task {
            for i in 0..<4 where !names[i].isEmpty {
                try? await client.setProbeName(index: i, name: names[i])
            }
            try? await client.setOffsets(offsets.map { Double($0) })  // blank -> keep
        }
    }
}

// MARK: PID + auto-tune

private struct PIDSection: View {
    let client: HeaterMeterClient
    @State private var presets: [PidPreset] = []
    @State private var presetKey = ""
    @State private var b = "", p = "", i = "", d = ""
    @State private var tuneSetpoint = "275"
    @State private var tuneRule = "tyreus_luyben"
    @State private var tune: AutoTuneStatus?
    @State private var poller: Task<Void, Never>?
    @State private var loaded = false

    private let rules = ["tyreus_luyben", "ziegler_nichols", "pessen", "some_overshoot", "no_overshoot"]

    var body: some View {
        Section("PID tuning") {
            Picker("Preset", selection: $presetKey) {
                Text("Custom").tag("")
                ForEach(presets) { Text($0.label).tag($0.key) }
            }
            .onChange(of: presetKey) { _, key in
                if let pr = presets.first(where: { $0.key == key }) {
                    b = trim(pr.b); p = trim(pr.p); i = trim(pr.i); d = trim(pr.d)
                }
            }
            HStack { field("B", $b); field("P", $p); field("I", $i); field("D", $d) }
            Button("Save PID") {
                Task { try? await client.setPID(b: Double(b), p: Double(p), i: Double(i), d: Double(d)) }
            }
        }
        Section("Auto-tune") {
            if let t = tune, t.running {
                HStack { Text("Running… \(t.phase)"); Spacer(); ProgressView() }
                if let c = t.cycles, let m = t.maxCycles { Text("cycle \(c)/\(m)").font(.caption).foregroundStyle(.secondary) }
                Button("Cancel", role: .destructive) { Task { try? await client.cancelAutotune(); await refresh() } }
            } else {
                HStack {
                    Text("Setpoint"); Spacer()
                    TextField("275", text: $tuneSetpoint).keyboardType(.numberPad)
                        .multilineTextAlignment(.trailing).frame(width: 70)
                }
                Picker("Rule", selection: $tuneRule) { ForEach(rules, id: \.self) { Text($0).tag($0) } }
                Button("Start auto-tune") {
                    Task {
                        guard let sp = Double(tuneSetpoint) else { return }
                        tune = try? await client.startAutotune(setpoint: sp, rule: tuneRule)
                        startPolling()
                    }
                }
                if let r = tune?.result, let kp = r.kp, let ki = r.ki, let kd = r.kd, tune?.done == true {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Last result").font(.caption.weight(.semibold))
                        Text(String(format: "P %.2f  I %.3f  D %.2f", kp, ki, kd))
                            .font(.caption).monospacedDigit().foregroundStyle(.secondary)
                    }
                    Button("Load into fields above") {
                        p = trim(r.kp ?? 0); i = trim(r.ki ?? 0); d = trim(r.kd ?? 0); presetKey = ""
                    }
                }
            }
        }
        .task {
            if !loaded {
                presets = (try? await client.presets().pid) ?? []
                if let pid = client.state?.pid {
                    p = pid.p.map(trim) ?? ""; i = pid.i.map(trim) ?? ""; d = pid.d.map(trim) ?? ""
                }
                loaded = true
            }
            await refresh()
        }
        .onDisappear { poller?.cancel() }
    }

    private func field(_ label: String, _ binding: Binding<String>) -> some View {
        VStack(spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            TextField(label, text: binding).keyboardType(.numbersAndPunctuation)
                .multilineTextAlignment(.center).textFieldStyle(.roundedBorder)
        }
    }
    private func trim(_ v: Double) -> String {
        v == v.rounded() ? String(Int(v)) : String(format: "%.3f", v)
    }
    private func refresh() async { tune = try? await client.autotuneStatus() }
    private func startPolling() {
        poller?.cancel()
        poller = Task {
            while !Task.isCancelled {
                await refresh()
                if tune?.running != true { break }
                try? await Task.sleep(nanoseconds: 4_000_000_000)
            }
        }
    }
}

// MARK: Smart lid recovery

private struct LidRecoverySection: View {
    let client: HeaterMeterClient
    @State private var cfg: LidRecovery?

    var body: some View {
        Section("Smart lid recovery") {
            if var c = cfg {
                Toggle("Resume fan early on recovery", isOn: Binding(
                    get: { c.enabled }, set: { c.enabled = $0; cfg = c }))
                stepperRow("Recovery rise °", value: Binding(
                    get: { c.recoverDelta }, set: { c.recoverDelta = $0; cfg = c }), range: 1...20, step: 1)
                stepperRow("Start fan %", value: Binding(
                    get: { Double(c.startPct) }, set: { c.startPct = Int($0); cfg = c }), range: 0...100, step: 5)
                stepperRow("Ramp to full (s)", value: Binding(
                    get: { Double(c.rampSecs) }, set: { c.rampSecs = Int($0); cfg = c }), range: 0...300, step: 10)
                Button("Save smart recovery") {
                    Task { cfg = try? await client.saveLidRecovery(c) }
                }
            } else {
                ProgressView()
            }
        }
        .task { if cfg == nil { cfg = try? await client.getLidRecovery() } }
    }

    private func stepperRow(_ label: String, value: Binding<Double>, range: ClosedRange<Double>, step: Double) -> some View {
        Stepper(value: value, in: range, step: step) {
            HStack { Text(label); Spacer(); Text("\(Int(value.wrappedValue))").foregroundStyle(.secondary) }
        }
    }
}

// MARK: Units

private struct UnitsSection: View {
    let client: HeaterMeterClient
    @State private var unit = "F"

    var body: some View {
        Section("Temperature unit") {
            Picker("Unit", selection: $unit) { Text("Fahrenheit").tag("F"); Text("Celsius").tag("C") }
                .pickerStyle(.segmented)
                .onChange(of: unit) { _, u in Task { try? await client.setUnits(u) } }
        }
        .onAppear { unit = client.state?.pid?.units ?? "F" }
    }
}

// MARK: Integrations (status only; configured from the web UI)

private struct IntegrationsSection: View {
    let client: HeaterMeterClient
    @State private var push: PushStatus?

    var body: some View {
        Section {
            if let push {
                LabeledContent("Push (APNs)", value: push.enabled ? "On" : "Off")
                if push.enabled {
                    LabeledContent("Devices", value: "\(push.tokenCount)")
                }
            }
            Text("MQTT / Home Assistant and notifications are configured from the web interface.")
                .font(.caption).foregroundStyle(.secondary)
        } header: { Text("Integrations") }
        .task { push = try? await client.pushStatus() }
    }
}

// MARK: About

private struct AboutSection: View {
    let client: HeaterMeterClient
    var onDisconnect: () -> Void
    @State private var confirm = false

    var body: some View {
        Section("About") {
            LabeledContent("App version", value: client.state?.appVersion ?? "—")
            LabeledContent("Controller", value: client.state?.version ?? "—")
            LabeledContent("Host", value: client.baseURL.host ?? "—")
            Button("Disconnect", role: .destructive) { confirm = true }
        }
        .confirmationDialog("Disconnect from this HeaterMeter?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Disconnect", role: .destructive, action: onDisconnect)
        }
    }
}
