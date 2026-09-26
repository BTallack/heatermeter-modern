//  CookActivityAttributes.swift
//  Shared between the app and the widget extension: the Live Activity's static
//  attributes and its content state. The content state mirrors the daemon's
//  apns.liveactivity_content_state() exactly (camelCase keys, predicted-done as
//  epoch seconds), so a push and a local update decode identically.

import ActivityKit
import Foundation

struct CookActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var pit: Double?
        var setpoint: Double?
        var food1: Double?
        var food2: Double?
        var food1Name: String?
        var food2Name: String?
        var fanPct: Double?
        var lidOpen: Bool?
        var mode: String?
        var predictedDoneFood1: Double?     // epoch seconds
        var predictedDoneFood2: Double?
        var alert: String?
    }

    var host: String
    var startedAt: Date
}

extension CookActivityAttributes.ContentState {
    static func deg(_ v: Double?) -> String { v.map { "\(Int($0.rounded()))°" } ?? "—" }
    static func clock(_ epoch: Double?) -> String? {
        guard let epoch else { return nil }
        return Date(timeIntervalSince1970: epoch).formatted(date: .omitted, time: .shortened)
    }
}
