//  DashboardView.swift
//  A compact dashboard sketch: pit hero + setpoint stepper + probe tiles, fed
//  live from HeaterMeterClient. Mirrors the web Dashboard.svelte. This is a
//  starting point — Graph (Swift Charts), Cook, and Settings screens follow the
//  same pattern against the same client.

import SwiftUI

struct DashboardView: View {
    @State private var client: HeaterMeterClient
    @State private var pendingSetpoint: Double = 225

    init(client: HeaterMeterClient) {
        _client = State(initialValue: client)
    }

    private var status: Status? { client.state?.status }
    private var names: [String] { client.state?.probeNames ?? [] }

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                pitHero
                probeGrid
                if let mode = status?.pidModeLabel {
                    Text(mode).font(.footnote).foregroundStyle(.secondary)
                }
            }
            .padding()
        }
        .navigationTitle("HeaterMeter")
        .task {
            if let s = try? await client.fetchStatus() {
                pendingSetpoint = s.status.setPoint ?? 225
            }
            client.connect()           // live updates from here on
        }
        .onChange(of: client.state?.status.setPoint) { _, sp in
            if let sp { pendingSetpoint = sp }
        }
    }

    // MARK: Pit hero

    private var pitHero: some View {
        VStack(spacing: 12) {
            Text(status?.pit.map { "\(Int($0))°" } ?? "—")
                .font(.system(size: 72, weight: .bold, design: .rounded))
                .foregroundStyle(.orange)
                .contentTransition(.numericText())

            HStack(spacing: 20) {
                readout("Set", status?.setPoint)
                readout("Fan", status?.fanPct, suffix: "%")
                if status?.lidOpen == true {
                    Label("Lid open", systemImage: "wind").foregroundStyle(.yellow)
                }
            }
            .font(.subheadline)

            HStack {
                Stepper("Setpoint \(Int(pendingSetpoint))°",
                        value: $pendingSetpoint, in: 100...450, step: 5)
                Button("Set") {
                    Task { try? await client.setSetpoint(pendingSetpoint) }
                }
                .buttonStyle(.borderedProminent)
                Button("Off", role: .destructive) {
                    Task { try? await client.turnOff() }
                }
            }
            .padding(.top, 4)
        }
        .padding()
        .frame(maxWidth: .infinity)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    private func readout(_ label: String, _ value: Double?,
                         suffix: String = "°") -> some View {
        VStack {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value.map { "\(Int($0))\(suffix)" } ?? "—").monospacedDigit()
        }
    }

    // MARK: Probe tiles

    private var probeGrid: some View {
        let probes = (status?.probes(names: names) ?? []).filter {
            $0.channel != "pit"     // pit is the hero above
        }
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 100))], spacing: 12) {
            ForEach(probes) { p in
                VStack(spacing: 4) {
                    Text(p.name).font(.caption).foregroundStyle(.secondary)
                        .lineLimit(1)
                    Text(p.temp.map { "\(Int($0))°" } ?? "—")
                        .font(.title2.weight(.semibold)).monospacedDigit()
                        .foregroundStyle(p.connected ? .primary : .tertiary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
            }
        }
    }
}
