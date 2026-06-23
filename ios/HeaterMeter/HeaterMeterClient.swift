//  HeaterMeterClient.swift
//  The one object the SwiftUI views talk to: REST control + data, plus a live
//  WebSocket feed of board state. `@Observable` so views re-render as `state`
//  updates. Targets iOS 17+ (Observation, async/await, URLSessionWebSocketTask).

import Foundation
import Observation

@MainActor
@Observable
final class HeaterMeterClient {

    var baseURL: URL
    var authToken: String?

    private(set) var state: DeviceState?
    private(set) var connected = false
    private(set) var lastError: String?

    private var liveLoop: Task<Void, Never>?
    private var wsTask: URLSessionWebSocketTask?
    private let session = URLSession(configuration: .default)

    init(baseURL: URL, authToken: String? = nil) {
        self.baseURL = baseURL
        self.authToken = authToken
    }

    // MARK: - Live status

    func fetchStatus() async throws -> DeviceState { try await get("status") }

    // MARK: - Pit control (Dashboard)

    func setSetpoint(_ fahrenheit: Double) async throws {
        try await post("setpoint", ["value": fahrenheit, "unit": "F"])
    }
    func turnOff() async throws { try await post("command", ["path": "/set?sp=O"]) }
    func setManualOutput(_ percent: Int) async throws { try await post("manual", ["percent": percent]) }
    func openLid() async throws { try await post("lid/open") }
    func cancelLid() async throws { try await post("lid/cancel") }

    // MARK: - Probes / targets

    func setProbeName(index: Int, name: String) async throws {
        try await post("probe-name", ["index": index, "name": name])
    }
    func setOffsets(_ offsets: [Double?]) async throws {
        try await post("offsets", ["offsets": offsets.map { $0 ?? NSNull() as Any }])
    }
    func setProbeType(index: Int, preset: String?, disabled: Bool) async throws {
        var body: [String: Any] = ["index": index, "disabled": disabled]
        if let preset { body["preset"] = preset }
        try await post("probe-type", body)
    }
    /// Set (or clear, with value=nil) a probe's high-alarm target. Other alarm
    /// slots are sent as null = "keep current", so only this one changes.
    func setProbeTarget(probeIndex: Int, target: Double?) async throws {
        var thresholds: [Any] = Array(repeating: NSNull(), count: 8)
        thresholds[probeIndex * 2 + 1] = target ?? -1.0   // -1 clears/disables
        try await post("alarms", ["thresholds": thresholds])
    }

    func predict(channel: String, target: Double) async throws -> Prediction {
        try await get("predict?channel=\(channel)&target=\(target)")
    }

    // MARK: - Graph

    func history(minutes: Double, sessionId: Int? = nil, limit: Int = 5000) async throws -> HistoryColumns {
        var path = "history?minutes=\(minutes)&limit=\(limit)"
        if let sessionId { path += "&session_id=\(sessionId)" }
        return try await get(path)
    }

    // MARK: - Cook (sessions + programs)

    func sessions(search: String? = nil) async throws -> [CookSession] {
        let q = search.map { "?search=\($0.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")" } ?? ""
        return try await get("sessions" + q)
    }
    func finishCook() async throws { try await post("cook/finish") }
    func renameSession(id: Int, name: String) async throws {
        try await send("PATCH", "sessions/\(id)", body: ["name": name])
    }
    func deleteSession(id: Int) async throws { try await send("DELETE", "sessions/\(id)") }

    func presets() async throws -> Presets { try await get("presets") }
    func programStatus() async throws -> ProgramStatus { try await get("program") }
    func startProgram(stages: [JSONValue], name: String?) async throws {
        var body: [String: Any] = ["stages": stages.map(\.anyValue)]
        if let name { body["name"] = name }
        try await post("program/start", body)
    }
    func stopProgram() async throws { try await post("program/stop") }

    // MARK: - Settings (PID, autotune, lid recovery, units, integrations)

    func setPID(b: Double?, p: Double?, i: Double?, d: Double?) async throws {
        var body: [String: Any] = [:]
        if let b { body["b"] = b }; if let p { body["p"] = p }
        if let i { body["i"] = i }; if let d { body["d"] = d }
        try await post("pid", body)
    }
    func autotuneStatus() async throws -> AutoTuneStatus { try await get("autotune") }
    func startAutotune(setpoint: Double, rule: String) async throws -> AutoTuneStatus {
        try await postDecoding("autotune", ["setpoint": setpoint, "rule": rule])
    }
    func cancelAutotune() async throws { try await send("DELETE", "autotune") }

    func getLidRecovery() async throws -> LidRecovery { try await get("lid-recovery") }
    func saveLidRecovery(_ cfg: LidRecovery) async throws -> LidRecovery {
        try await postDecoding("lid-recovery", [
            "enabled": cfg.enabled, "recover_delta": cfg.recoverDelta,
            "start_pct": cfg.startPct, "ramp_secs": cfg.rampSecs,
            "min_armed_secs": cfg.minArmedSecs])
    }

    func setUnits(_ unit: String) async throws { try await post("units", ["unit": unit]) }

    func pushStatus() async throws -> PushStatus { try await get("push") }
    func registerPush(deviceToken: Data) async throws {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        try await post("push/register", ["token": hex, "platform": "ios"])
    }

    // MARK: - Live WebSocket feed

    func connect() {
        guard liveLoop == nil else { return }
        liveLoop = Task { [weak self] in
            var backoff: UInt64 = 1
            while let self, !Task.isCancelled {
                do { try await self.runSocket(); backoff = 1 }
                catch {
                    self.connected = false
                    self.lastError = error.localizedDescription
                }
                if Task.isCancelled { break }
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 15)
            }
        }
    }

    func disconnect() {
        liveLoop?.cancel(); liveLoop = nil
        wsTask?.cancel(with: .goingAway, reason: nil); wsTask = nil
        connected = false
    }

    private func runSocket() async throws {
        var comps = URLComponents(url: baseURL.appendingPathComponent("api/ws"),
                                  resolvingAgainstBaseURL: false)!
        comps.scheme = baseURL.scheme == "https" ? "wss" : "ws"
        if let token = authToken { comps.queryItems = [URLQueryItem(name: "token", value: token)] }
        let task = session.webSocketTask(with: comps.url!)
        wsTask = task
        task.resume()
        connected = true; lastError = nil
        let decoder = JSONDecoder()
        while !Task.isCancelled {
            let frame = try await task.receive()
            guard case let .string(text) = frame, let data = text.data(using: .utf8) else { continue }
            if let msg = try? decoder.decode(LiveMessage.self, from: data), let s = msg.state {
                state = s
            }
        }
    }

    // MARK: - Request plumbing

    private func get<T: Decodable>(_ path: String) async throws -> T {
        let (data, resp) = try await session.data(for: request(path, method: "GET"))
        try Self.check(resp, data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    private func post(_ path: String, _ body: [String: Any] = [:]) async throws -> Data {
        try await send("POST", path, body: body)
    }

    private func postDecoding<T: Decodable>(_ path: String, _ body: [String: Any]) async throws -> T {
        let data = try await send("POST", path, body: body)
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    private func send(_ method: String, _ path: String, body: [String: Any]? = nil) async throws -> Data {
        var req = request(path, method: method)
        if let body, !body.isEmpty {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp, data)
        return data
    }

    private func request(_ path: String, method: String) -> URLRequest {
        // Relative resolution (not appendingPathComponent) so query strings survive.
        let url = URL(string: "api/\(path)", relativeTo: baseURL)!
        var req = URLRequest(url: url)
        req.httpMethod = method
        if let token = authToken { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        return req
    }

    private static func check(_ resp: URLResponse, _ data: Data) throws {
        guard let http = resp as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw NSError(domain: "HeaterMeter", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(body)"])
        }
    }
}
