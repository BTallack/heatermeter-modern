//  HeaterMeterClient.swift
//  The one object the SwiftUI views talk to: REST control + a live WebSocket
//  feed of board state. `@Observable` so views re-render as `state` updates.
//
//  Targets iOS 17+ (Observation, async/await, URLSessionWebSocketTask).

import Foundation
import Observation

@MainActor
@Observable
final class HeaterMeterClient {

    /// e.g. http://192.168.3.164:8080 on the LAN, or a Tailscale / reverse-proxy
    /// host when away. Bonjour discovery can fill this in automatically.
    var baseURL: URL
    /// Bearer token when the daemon's optional auth is enabled (else nil).
    var authToken: String?

    private(set) var state: DeviceState?
    private(set) var connected = false
    private(set) var lastError: String?

    private var wsTask: URLSessionWebSocketTask?
    private var liveLoop: Task<Void, Never>?
    private let session = URLSession(configuration: .default)

    init(baseURL: URL, authToken: String? = nil) {
        self.baseURL = baseURL
        self.authToken = authToken
    }

    // MARK: - REST

    func fetchStatus() async throws -> DeviceState {
        try await get("status", as: DeviceState.self)
    }

    /// Positive setpoint = PID auto control.
    func setSetpoint(_ fahrenheit: Double) async throws {
        try await post("setpoint", body: ["value": fahrenheit, "unit": "F"])
    }

    /// Turn the cooker off (PID + fan idle). The setpoint endpoint takes a
    /// float, so "off" goes through the raw command passthrough like the web UI.
    func turnOff() async throws {
        try await post("command", body: ["path": "/set?sp=O"])
    }

    /// Manual fan output 0–100 (overrides PID until a setpoint is set again).
    func setManualOutput(_ percent: Int) async throws {
        try await post("manual", body: ["percent": percent])
    }

    func openLid() async throws { try await post("lid/open") }
    func cancelLid() async throws { try await post("lid/cancel") }

    func predict(channel: String, target: Double) async throws -> Prediction {
        try await get("predict?channel=\(channel)&target=\(target)",
                      as: Prediction.self)
    }

    func sessions() async throws -> [CookSession] {
        try await get("sessions", as: [CookSession].self)
    }

    // MARK: - Push registration (call after APNs grants a device token)

    func registerPush(deviceToken: Data) async throws {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        try await post("push/register", body: ["token": hex, "platform": "ios"])
    }

    func pushStatus() async throws -> PushStatus {
        try await get("push", as: PushStatus.self)
    }

    // MARK: - Live WebSocket feed

    /// Open the live feed; reconnects with backoff until `disconnect()`.
    func connect() {
        guard liveLoop == nil else { return }
        liveLoop = Task { [weak self] in
            var backoff: UInt64 = 1
            while let self, !Task.isCancelled {
                do {
                    try await self.runSocket()
                    backoff = 1                        // clean close: reset
                } catch {
                    await MainActor.run {
                        self.connected = false
                        self.lastError = error.localizedDescription
                    }
                }
                if Task.isCancelled { break }
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 15)         // 1,2,4,8,15s cap
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
        if let token = authToken {                     // WS can't set headers
            comps.queryItems = [URLQueryItem(name: "token", value: token)]
        }
        let task = session.webSocketTask(with: comps.url!)
        wsTask = task
        task.resume()
        connected = true
        lastError = nil

        let decoder = JSONDecoder()
        while !Task.isCancelled {
            let frame = try await task.receive()
            guard case let .string(text) = frame,
                  let data = text.data(using: .utf8) else { continue }
            if let msg = try? decoder.decode(LiveMessage.self, from: data),
               let newState = msg.state {
                state = newState                       // @MainActor: drives UI
            }
            // msg.event carries alarms / lid_recovery / timeline — route as needed.
        }
    }

    // MARK: - Request plumbing

    private func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        let (data, resp) = try await session.data(for: request(path, method: "GET"))
        try Self.check(resp, data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    private func post(_ path: String, body: [String: Any] = [:]) async throws -> Data {
        var req = request(path, method: "POST")
        if !body.isEmpty {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp, data)
        return data
    }

    private func request(_ path: String, method: String) -> URLRequest {
        // Relative-URL resolution (not appendingPathComponent) so query strings
        // like "predict?channel=food1&target=203" survive unescaped.
        let url = URL(string: "api/\(path)", relativeTo: baseURL)!
        var req = URLRequest(url: url)
        req.httpMethod = method
        if let token = authToken {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return req
    }

    private static func check(_ resp: URLResponse, _ data: Data) throws {
        guard let http = resp as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw NSError(domain: "HeaterMeter", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey:
                            "HTTP \(http.statusCode): \(body)"])
        }
    }
}
