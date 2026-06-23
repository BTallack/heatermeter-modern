//  GraphView.swift
//  Live temperature chart (Swift Charts) over the daemon's /api/history, with a
//  range picker and dashed target lines. Defaults to the current cook.

import SwiftUI
import Charts

private struct SeriesPoint: Identifiable {
    let series: String
    let date: Date
    let value: Double
    var id: String { "\(series)-\(date.timeIntervalSince1970)" }
}

struct GraphView: View {
    @Environment(HeaterMeterClient.self) private var client

    enum Range: Hashable { case cook, minutes(Int) }
    @State private var range: Range = .cook
    @State private var points: [SeriesPoint] = []
    @State private var loading = false
    @State private var refresher: Task<Void, Never>?

    private let colors: [String: Color] = [
        "Pit": .orange, "Setpoint": .gray, "Food 1": .green, "Food 2": .teal,
    ]

    var body: some View {
        VStack(spacing: 12) {
            Picker("Range", selection: $range) {
                Text("This cook").tag(Range.cook)
                Text("30m").tag(Range.minutes(30))
                Text("2h").tag(Range.minutes(120))
                Text("6h").tag(Range.minutes(360))
            }
            .pickerStyle(.segmented)

            if points.isEmpty {
                ContentUnavailableView(loading ? "Loading…" : "No data yet",
                                       systemImage: "chart.xyaxis.line")
                    .frame(maxHeight: .infinity)
            } else {
                chart.frame(maxHeight: .infinity)
            }
        }
        .padding()
        .navigationTitle("Graph")
        .onChange(of: range) { _, _ in Task { await load() } }
        .task { startRefreshing() }
        .onDisappear { refresher?.cancel(); refresher = nil }
    }

    private var chart: some View {
        Chart {
            ForEach(points) { p in
                if p.series == "Setpoint" {
                    LineMark(x: .value("Time", p.date), y: .value("°", p.value),
                             series: .value("Series", p.series))
                        .interpolationMethod(.stepCenter)
                        .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                        .foregroundStyle(by: .value("Series", p.series))
                } else {
                    LineMark(x: .value("Time", p.date), y: .value("°", p.value),
                             series: .value("Series", p.series))
                        .foregroundStyle(by: .value("Series", p.series))
                }
            }
            ForEach(targetLines(), id: \.0) { name, target in
                RuleMark(y: .value("Target", target))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [2, 4]))
                    .foregroundStyle((colors[name] ?? .secondary).opacity(0.5))
            }
        }
        .chartForegroundStyleScale([
            "Pit": Color.orange, "Setpoint": Color.gray,
            "Food 1": Color.green, "Food 2": Color.teal,
        ])
        .chartLegend(position: .bottom)
        .chartYAxisLabel("°F")
    }

    // MARK: data

    private func targetLines() -> [(String, Double)] {
        guard let st = client.state else { return [] }
        var out: [(String, Double)] = []
        if let t = st.target(forProbe: 1) { out.append(("Food 1", t)) }
        if let t = st.target(forProbe: 2) { out.append(("Food 2", t)) }
        return out
    }

    private func startRefreshing() {
        refresher?.cancel()
        refresher = Task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    private func load() async {
        loading = true; defer { loading = false }
        let hist: HistoryColumns?
        switch range {
        case .cook:
            hist = try? await client.history(minutes: 7 * 24 * 60,
                                             sessionId: client.state?.sessionId, limit: 4000)
        case .minutes(let m):
            hist = try? await client.history(minutes: Double(m), limit: 4000)
        }
        guard let h = hist else { return }
        var pts: [SeriesPoint] = []
        func add(_ key: KeyPath<HistoryColumns, [Double?]>, _ name: String) {
            for tp in h.points(key) { pts.append(SeriesPoint(series: name, date: tp.date, value: tp.value)) }
        }
        add(\.pit, "Pit")
        add(\.setPoint, "Setpoint")
        add(\.food1, "Food 1")
        add(\.food2, "Food 2")
        points = pts
    }
}
