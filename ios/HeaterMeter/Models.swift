//  Models.swift
//  HeaterMeter — Codable models mirroring the daemon's REST + WebSocket JSON.
//
//  Source of truth: backend/heatermeterd/state.py `to_dict()`, protocol.py
//  `Status.to_dict()`, and api.py. Keep this file in sync when the API gains
//  fields — the JSON is the contract between the daemon and the app.
//
//  Decoding note: the board reports some config fields as STRINGS even though
//  they're numeric (probe offsets, PID constants, the LCD/display fields). The
//  live `status` block is clean Doubles, but anything that round-trips board
//  config needs lenient decoding — see `LenientDouble` at the bottom.

import Foundation

// MARK: - Top-level state (/api/status and the WS "state" object)

struct DeviceState: Codable, Sendable {
    var deviceName: String?
    var version: String?          // board firmware, e.g. "20260601-hm1B"
    var appVersion: String?       // daemon app version, e.g. "0.4.1"
    var sessionId: Int?
    var status: Status
    var probeNames: [String]

    enum CodingKeys: String, CodingKey {
        case deviceName = "device_name"
        case version
        case appVersion = "app_version"
        case sessionId = "session_id"
        case status
        case probeNames = "probe_names"
    }
}

// MARK: - Live status ($HMSU)

struct Status: Codable, Sendable {
    var setPoint: Double?
    var pit: Double?
    var food1: Double?
    var food2: Double?
    var ambient: Double?
    var outputPct: Double?
    var fanPct: Double?
    var servoPct: Double?
    var lidCountdown: Int?
    var pidMode: Int?
    var pidModeLabel: String?     // "At temp", "Recovering", "Off", …

    enum CodingKeys: String, CodingKey {
        case setPoint = "set_point"
        case pit, food1, food2, ambient
        case outputPct = "output_pct"
        case fanPct = "fan_pct"
        case servoPct = "servo_pct"
        case lidCountdown = "lid_countdown"
        case pidMode = "pid_mode"
        case pidModeLabel = "pid_mode_label"
    }

    var lidOpen: Bool { (lidCountdown ?? 0) > 0 }

    /// The probe channels in display order, paired with their live temps.
    func probes(names: [String]) -> [ProbeReading] {
        [("pit", pit), ("food1", food1), ("food2", food2), ("ambient", ambient)]
            .enumerated()
            .map { idx, pair in
                ProbeReading(channel: pair.0,
                             name: idx < names.count ? names[idx] : pair.0,
                             temp: pair.1)
            }
    }
}

struct ProbeReading: Identifiable, Sendable {
    let channel: String           // pit / food1 / food2 / ambient
    let name: String
    let temp: Double?
    var id: String { channel }
    var connected: Bool { temp != nil }
}

// MARK: - WebSocket envelope (/api/ws)

/// The daemon pushes either a full `{ts, session_id, state}` snapshot or an
/// `{event: {...}}` side-channel message (alarms, lid_recovery, timeline, …).
struct LiveMessage: Decodable, Sendable {
    var ts: Double?
    var sessionId: Int?
    var state: DeviceState?
    var event: LiveEvent?

    enum CodingKeys: String, CodingKey {
        case ts, state, event
        case sessionId = "session_id"
    }
}

struct LiveEvent: Decodable, Sendable {
    var type: String              // "probe_event", "lid_recovery", "timeline", …
    var message: String?
    // The rest of an event's fields vary by type; decode what a given screen
    // needs by extending this struct or decoding `event` again as a concrete type.
}

// MARK: - Predictions (/api/predict?channel=&target=)

struct Prediction: Decodable, Sendable {
    var etaSeconds: Double?
    var confidence: String?       // "none" | "low" | "medium" | "high"

    enum CodingKeys: String, CodingKey {
        case etaSeconds = "eta_seconds"
        case confidence
    }

    /// Wall-clock "ready at", computed from now + ETA.
    var readyAt: Date? {
        guard let eta = etaSeconds, eta > 0 else { return nil }
        return Date().addingTimeInterval(eta)
    }
}

// MARK: - Sessions (/api/sessions)

struct CookSession: Codable, Identifiable, Sendable {
    var id: Int
    var name: String?
    var startedTs: Double
    var endedTs: Double?
    var completedTs: Double?

    enum CodingKeys: String, CodingKey {
        case id, name
        case startedTs = "started_ts"
        case endedTs = "ended_ts"
        case completedTs = "completed_ts"
    }

    var isActive: Bool { endedTs == nil }
}

// MARK: - Push status (/api/push)

struct PushStatus: Codable, Sendable {
    var enabled: Bool
    var configured: Bool
    var available: Bool
    var sandbox: Bool
    var bundleId: String
    var tokenCount: Int

    enum CodingKeys: String, CodingKey {
        case enabled, configured, available, sandbox
        case bundleId = "bundle_id"
        case tokenCount = "token_count"
    }
}

// MARK: - Lenient numeric decoding (for the string-typed board config fields)

/// Decodes a JSON value that may arrive as a number OR a numeric string (the
/// board reports offsets/PID/display fields as strings). Use on config models:
///
///     struct PIDConfig: Decodable {
///         @LenientDouble var p: Double?
///         @LenientDouble var i: Double?
///         @LenientDouble var d: Double?
///     }
@propertyWrapper
struct LenientDouble: Codable, Sendable {
    var wrappedValue: Double?
    init(wrappedValue: Double?) { self.wrappedValue = wrappedValue }
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { wrappedValue = nil }
        else if let d = try? c.decode(Double.self) { wrappedValue = d }
        else if let s = try? c.decode(String.self) { wrappedValue = Double(s) }
        else { wrappedValue = nil }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(wrappedValue)
    }
}
