//  CookLiveActivity.swift
//  The live cook at a glance: pit vs setpoint, food temps with their done-by
//  clocks, and the daemon's one-line alert (pit low, fire dying, levelling off).

import ActivityKit
import SwiftUI
import WidgetKit

private typealias CS = CookActivityAttributes.ContentState

struct CookLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: CookActivityAttributes.self) { context in
            LockScreenCookView(state: context.state)
                .activityBackgroundTint(Color.black.opacity(0.75))
                .activitySystemActionForegroundColor(.orange)
        } dynamicIsland: { context in
            let s = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(CS.deg(s.pit))
                            .font(.system(size: 34, weight: .bold, design: .rounded))
                            .foregroundStyle(.orange)
                        Text("set \(CS.deg(s.setpoint))").font(.caption).foregroundStyle(.secondary)
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    VStack(alignment: .trailing, spacing: 4) {
                        FoodLine(name: s.food1Name ?? "Food 1", temp: s.food1, done: s.predictedDoneFood1)
                        FoodLine(name: s.food2Name ?? "Food 2", temp: s.food2, done: s.predictedDoneFood2)
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    StatusLine(state: s)
                }
            } compactLeading: {
                HStack(spacing: 3) {
                    Image(systemName: "flame.fill").foregroundStyle(.orange)
                    Text(CS.deg(s.pit)).font(.caption.weight(.semibold)).monospacedDigit()
                }
            } compactTrailing: {
                if s.alert != nil {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                } else if let f = s.food1 {
                    Text(CS.deg(f)).font(.caption.weight(.semibold)).monospacedDigit()
                }
            } minimal: {
                Image(systemName: s.alert == nil ? "flame.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(s.alert == nil ? .orange : .red)
            }
            .keylineTint(.orange)
        }
    }
}

private struct FoodLine: View {
    let name: String
    let temp: Double?
    let done: Double?
    var body: some View {
        if let temp {
            HStack(spacing: 4) {
                Text(name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Text(CS.deg(temp)).font(.callout.weight(.semibold)).monospacedDigit()
                if let c = CS.clock(done) {
                    Text("~\(c)").font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

private struct StatusLine: View {
    let state: CS
    var body: some View {
        HStack(spacing: 6) {
            if let a = state.alert {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                Text(a).font(.caption.weight(.semibold)).foregroundStyle(.red)
            } else if state.lidOpen == true {
                Image(systemName: "wind").foregroundStyle(.yellow)
                Text("Lid open").font(.caption.weight(.semibold)).foregroundStyle(.yellow)
            } else {
                let parts = [state.mode, state.fanPct.map { "fan \(Int($0))%" }]
                    .compactMap { $0 }.filter { !$0.isEmpty }
                Text(parts.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }
}

private struct LockScreenCookView: View {
    let state: CS
    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Image(systemName: "flame.fill").foregroundStyle(.orange)
                    Text("Pit").font(.caption).foregroundStyle(.secondary)
                }
                Text(CS.deg(state.pit))
                    .font(.system(size: 40, weight: .bold, design: .rounded))
                    .foregroundStyle(.orange)
                    .monospacedDigit()
                Text("set \(CS.deg(state.setpoint))").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 6) {
                FoodLine(name: state.food1Name ?? "Food 1", temp: state.food1, done: state.predictedDoneFood1)
                FoodLine(name: state.food2Name ?? "Food 2", temp: state.food2, done: state.predictedDoneFood2)
                StatusLine(state: state)
            }
        }
        .padding(14)
        .foregroundStyle(.white)
    }
}
