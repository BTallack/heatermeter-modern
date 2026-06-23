//  CookView.swift
//  Cook lifecycle: finish the current cook, start a program from a preset, and
//  browse past cooks with per-session stats (rename / delete).

import SwiftUI

struct CookView: View {
    @Environment(HeaterMeterClient.self) private var client

    @State private var sessions: [CookSession] = []
    @State private var programs: [ProgramPreset] = []
    @State private var activeProgram: ProgramStatus?
    @State private var detail: CookSession?
    @State private var toast: String?

    var body: some View {
        List {
            Section("Current cook") {
                if let p = activeProgram, p.running {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.label ?? "Program running").font(.subheadline.weight(.semibold))
                        if let s = p.stageLabel { Text(s).font(.caption).foregroundStyle(.secondary) }
                    }
                    Button("Stop program", role: .destructive) {
                        run { try await client.stopProgram(); await reload() }
                    }
                }
                Button("Finish cook") { run { try await client.finishCook(); await reload() } }
            }

            Section("Start a program") {
                ForEach(programCategories(), id: \.self) { cat in
                    DisclosureGroup(cat) {
                        ForEach(programs.filter { ($0.category ?? "Other") == cat }) { prog in
                            Button {
                                run { try await client.startProgram(stages: prog.stages, name: prog.label); await reload() }
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(prog.label)
                                    if let n = prog.note { Text(n).font(.caption).foregroundStyle(.secondary) }
                                }
                            }
                        }
                    }
                }
            }

            Section("Past cooks") {
                if sessions.isEmpty {
                    Text("No cooks yet").foregroundStyle(.secondary)
                }
                ForEach(sessions) { s in
                    Button { detail = s } label: { sessionRow(s) }
                }
            }
        }
        .navigationTitle("Cook")
        .overlay(alignment: .top) { if let toast { ToastPill(toast) } }
        .refreshable { await reload() }
        .task { await reload() }
        .sheet(item: $detail) { s in
            SessionDetail(client: client, session: s, onChange: { Task { await reload() } })
        }
    }

    private func sessionRow(_ s: CookSession) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(s.title).foregroundStyle(.primary)
                Text(s.startedDate.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if s.isActive {
                Text("LIVE").font(.caption2.bold()).foregroundStyle(.orange)
            } else if s.completedTs != nil {
                Image(systemName: "checkmark.seal.fill").foregroundStyle(.green)
            }
        }
    }

    private func programCategories() -> [String] {
        Array(NSOrderedSet(array: programs.map { $0.category ?? "Other" }) as? [String] ?? [])
    }

    private func reload() async {
        sessions = (try? await client.sessions()) ?? sessions
        if programs.isEmpty { programs = (try? await client.presets().program) ?? [] }
        activeProgram = try? await client.programStatus()
    }

    private func run(_ op: @escaping () async throws -> Void) {
        Task {
            do { try await op() }
            catch {
                withAnimation { toast = error.localizedDescription }
                try? await Task.sleep(nanoseconds: 2_500_000_000)
                withAnimation { toast = nil }
            }
        }
    }
}

// MARK: - Session detail (stats + rename + delete)

private struct SessionDetail: View {
    let client: HeaterMeterClient
    let session: CookSession
    var onChange: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var stats: Stats?
    @State private var loading = true
    @State private var name = ""
    @State private var confirmDelete = false

    struct Stats {
        var durationMin: Double
        var pitAvg: Double, pitMin: Double, pitMax: Double
        var food1Max: Double?, food2Max: Double?
        var fanAvg: Double
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Name") {
                    TextField("Cook name", text: $name)
                    Button("Save name") {
                        Task { try? await client.renameSession(id: session.id, name: name); onChange() }
                    }.disabled(name.isEmpty || name == session.name)
                }
                Section("Stats") {
                    if loading { ProgressView() }
                    else if let s = stats {
                        row("Duration", String(format: "%.0f min", s.durationMin))
                        row("Pit avg", "\(Int(s.pitAvg))°  (\(Int(s.pitMin))–\(Int(s.pitMax))°)")
                        if let f = s.food1Max { row("Food 1 max", "\(Int(f))°") }
                        if let f = s.food2Max { row("Food 2 max", "\(Int(f))°") }
                        row("Fan avg", "\(Int(s.fanAvg))%")
                        if let r = session.completedReason { row("Completed", r) }
                    } else {
                        Text("No samples").foregroundStyle(.secondary)
                    }
                }
                Section {
                    Button("Delete cook", role: .destructive) { confirmDelete = true }
                }
            }
            .navigationTitle(session.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .onAppear { name = session.name ?? "" }
            .task { await loadStats() }
            .confirmationDialog("Delete this cook?", isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("Delete", role: .destructive) {
                    Task { try? await client.deleteSession(id: session.id); onChange(); dismiss() }
                }
            }
        }
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack { Text(k); Spacer(); Text(v).foregroundStyle(.secondary).monospacedDigit() }
    }

    private func loadStats() async {
        loading = true; defer { loading = false }
        guard let h = try? await client.history(minutes: 30 * 24 * 60, sessionId: session.id, limit: 8000),
              !h.t.isEmpty else { return }
        let pit = h.pit.compactMap { $0 }
        guard !pit.isEmpty else { return }
        let fan = h.fanPct.compactMap { $0 }
        stats = Stats(
            durationMin: (h.t.last! - h.t.first!) / 60,
            pitAvg: pit.reduce(0, +) / Double(pit.count),
            pitMin: pit.min() ?? 0, pitMax: pit.max() ?? 0,
            food1Max: h.food1.compactMap { $0 }.max(),
            food2Max: h.food2.compactMap { $0 }.max(),
            fanAvg: fan.isEmpty ? 0 : fan.reduce(0, +) / Double(fan.count))
    }
}

struct ToastPill: View {
    let text: String
    init(_ t: String) { text = t }
    var body: some View {
        Text(text).font(.footnote).padding(.horizontal, 14).padding(.vertical, 8)
            .background(.thinMaterial, in: Capsule()).padding(.top, 4)
    }
}
