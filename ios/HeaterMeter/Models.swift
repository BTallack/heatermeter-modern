//  Models.swift
//  Codable models mirroring the daemon's REST + WebSocket JSON.
//
//  Source of truth: backend/heatermeterd/state.py `to_dict()`, protocol.py
//  `Status.to_dict()`, presets.py, and api.py. The JSON is the contract — keep
//  this in sync when the API gains fields.
//
//  Decoding note: the board reports some config fields as STRINGS even though
//  they're numeric (probe offsets, PID constants, alarm thresholds). The live
//  `status` block is clean Doubles; anything round-tripping board config uses
//  the lenient helpers at the bottom.

import Foundation

// MARK: - Top-level state (/api/status and the WS "state" object)

struct DeviceState: Decodable, Sendable {
    var deviceName: String?
    var version: String?
    var appVersion: String?
    var sessionId: Int?
    var status: Status
    var probeNames: [String]
    var pid: PIDValues?
    var alarms: [String]   // flat [low0,high0,low1,high1,...]

    enum CodingKeys: String, CodingKey {
        case deviceName = "device_name"
        case version
        case appVersion = "app_version"
        case sessionId = "session_id"
        case status
        case probeNames = "probe_names"
        case pid
        case alarms
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        deviceName = try c.decodeIfPresent(String.self, forKey: .deviceName)
        version = try c.decodeIfPresent(String.self, forKey: .version)
        appVersion = try c.decodeIfPresent(String.self, forKey: .appVersion)
        sessionId = try c.decodeIfPresent(Int.self, forKey: .sessionId)
        status = try c.decode(Status.self, forKey: .status)
        probeNames = (try? c.decode([String].self, forKey: .probeNames)) ?? []
        pid = try? c.decode(PIDValues.self, forKey: .pid)
        // alarms may arrive as strings ("203H"/"-40") or numbers; coerce to strings.
        if let s = try? c.decode([String].self, forKey: .alarms) {
            alarms = s
        } else if let raw = try? c.decode([JSONValue].self, forKey: .alarms) {
            alarms = raw.map { v in
                switch v {
                case .string(let s): return s
                case .number(let n): return n == n.rounded() ? String(Int(n)) : String(n)
                default: return ""
                }
            }
        } else {
            alarms = []
        }
    }

    /// The high-alarm target for a probe index (1=food1, 2=food2, 3=ambient),
    /// or nil when unset/disabled. Stored at flat index 2*probe+1, possibly with
    /// a trailing L/H "ringing" marker.
    func target(forProbe probe: Int) -> Double? {
        let idx = probe * 2 + 1
        guard idx < alarms.count else { return nil }
        let v = Double(alarms[idx].trimmingCharacters(in: CharacterSet(charactersIn: "LH")))
        guard let v, v >= 0 else { return nil }
        return v
    }
}

struct PIDValues: Codable, Sendable {
    @LenientDouble var p: Double?
    @LenientDouble var i: Double?
    @LenientDouble var d: Double?
    var units: String?
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
    var pidModeLabel: String?

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

    func probes(names: [String]) -> [ProbeReading] {
        [("pit", pit), ("food1", food1), ("food2", food2), ("ambient", ambient)]
            .enumerated().map { idx, pair in
                ProbeReading(channel: pair.0,
                             name: idx < names.count ? names[idx] : pair.0,
                             temp: pair.1, probeIndex: idx)
            }
    }
}

struct ProbeReading: Identifiable, Sendable {
    let channel: String           // pit / food1 / food2 / ambient
    let name: String
    let temp: Double?
    let probeIndex: Int           // 0..3
    var id: String { channel }
    var connected: Bool { temp != nil }
    var isFood: Bool { probeIndex == 1 || probeIndex == 2 }
}

// MARK: - WebSocket envelope (/api/ws)

struct LiveMessage: Decodable, Sendable {
    var ts: Double?
    var sessionId: Int?
    var state: DeviceState?
    var event: LiveEvent?
    enum CodingKeys: String, CodingKey { case ts, state, event; case sessionId = "session_id" }
}

struct LiveEvent: Decodable, Sendable {
    var type: String
    var message: String?
}

// MARK: - History (/api/history) -> Swift Charts

struct HistoryColumns: Decodable, Sendable {
    var t: [Double]
    var setPoint: [Double?]
    var pit: [Double?]
    var food1: [Double?]
    var food2: [Double?]
    var ambient: [Double?]
    var fanPct: [Double?]
    enum CodingKeys: String, CodingKey {
        case t, pit, food1, food2, ambient
        case setPoint = "set_point"
        case fanPct = "fan_pct"
    }

    /// Flatten one column into chart points, dropping gaps.
    func points(_ key: KeyPath<HistoryColumns, [Double?]>) -> [TempPoint] {
        let col = self[keyPath: key]
        var out: [TempPoint] = []
        for (i, ts) in t.enumerated() where i < col.count {
            if let v = col[i] { out.append(TempPoint(date: Date(timeIntervalSince1970: ts), value: v)) }
        }
        return out
    }
}

struct TempPoint: Identifiable, Sendable {
    let date: Date
    let value: Double
    var id: Double { date.timeIntervalSince1970 }
}

// MARK: - Prediction (/api/predict)

struct Prediction: Decodable, Sendable {
    var etaSeconds: Double?
    var confidence: String?
    enum CodingKeys: String, CodingKey { case etaSeconds = "eta_seconds"; case confidence }
    var readyAt: Date? {
        guard let eta = etaSeconds, eta > 0 else { return nil }
        return Date().addingTimeInterval(eta)
    }
    var hasETA: Bool { (etaSeconds ?? 0) > 0 && confidence != nil && confidence != "none" }
}

// MARK: - Sessions (/api/sessions)

struct CookSession: Codable, Identifiable, Sendable {
    var id: Int
    var name: String?
    var description: String?
    var startedTs: Double
    var endedTs: Double?
    var completedTs: Double?
    var completedReason: String?
    var sampleCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, description
        case startedTs = "started_ts"
        case endedTs = "ended_ts"
        case completedTs = "completed_ts"
        case completedReason = "completed_reason"
        case sampleCount = "sample_count"
    }

    var isActive: Bool { endedTs == nil }
    var startedDate: Date { Date(timeIntervalSince1970: startedTs) }
    var title: String { name ?? "Cook #\(id)" }
}

// MARK: - Presets (/api/presets)

struct Presets: Decodable, Sendable {
    var meat: [MeatPreset]
    var pid: [PidPreset]
    var program: [ProgramPreset]
}

struct MeatPreset: Decodable, Identifiable, Sendable {
    var key: String
    var label: String
    var tempF: Int
    var category: String
    var note: String?
    var id: String { key }
    enum CodingKeys: String, CodingKey { case key, label, category, note; case tempF = "temp_f" }
}

struct PidPreset: Decodable, Identifiable, Sendable {
    var key: String
    var label: String
    var b: Double
    var p: Double
    var i: Double
    var d: Double
    var note: String?
    var id: String { key }
}

struct ProgramPreset: Decodable, Identifiable, Sendable {
    var key: String
    var label: String
    var category: String?
    var note: String?
    var stages: [JSONValue]       // passed straight back to /api/program/start
    var id: String { key }
}

// MARK: - Auto-tune (/api/autotune)

struct AutoTuneStatus: Decodable, Sendable {
    var phase: String
    var done: Bool
    var setpoint: Double?
    var rule: String?
    var cycles: Int?
    var maxCycles: Int?
    var result: AutoTuneResult?
    enum CodingKeys: String, CodingKey {
        case phase, done, setpoint, rule, cycles, result
        case maxCycles = "max_cycles"
    }
    var running: Bool { !done && phase != "idle" }
}

struct AutoTuneResult: Decodable, Sendable {
    var kp: Double?
    var ki: Double?
    var kd: Double?
    var ku: Double?
    var tu: Double?
    var rule: String?
}

// MARK: - Program status (/api/program)

struct ProgramStatus: Decodable, Sendable {
    var running: Bool
    var label: String?
    var stageLabel: String?
    enum CodingKeys: String, CodingKey { case running, label; case stageLabel = "stage_label" }
}

// MARK: - Integrations status (read-only summaries)

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

// MARK: - Lid recovery config (/api/lid-recovery)

struct LidRecovery: Codable, Sendable {
    var enabled: Bool
    var recoverDelta: Double
    var startPct: Int
    var rampSecs: Int
    var minArmedSecs: Int
    enum CodingKeys: String, CodingKey {
        case enabled
        case recoverDelta = "recover_delta"
        case startPct = "start_pct"
        case rampSecs = "ramp_secs"
        case minArmedSecs = "min_armed_secs"
    }
}

// MARK: - Lenient decoding helpers

/// Decodes a value that may be a number OR a numeric string.
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
        var c = encoder.singleValueContainer(); try c.encode(wrappedValue)
    }
}

/// Minimal arbitrary-JSON value, so we can carry through opaque blobs (program
/// stages) and re-serialize them for POST without modeling every field.
enum JSONValue: Codable, Sendable {
    case null, bool(Bool), number(Double), string(String)
    case array([JSONValue]), object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { self = .null }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    /// Foundation object for JSONSerialization-based POST bodies.
    var anyValue: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let b): return b
        case .number(let n): return n
        case .string(let s): return s
        case .array(let a): return a.map(\.anyValue)
        case .object(let o): return o.mapValues(\.anyValue)
        }
    }
}
