//  LiveActivityManager.swift
//  Starts a Live Activity when a cook is running, keeps it fresh from the live
//  state while the app is open (the daemon pushes the same content state when
//  it is not), registers the activity's push token with the daemon, and ends
//  it when the cook is over.

import ActivityKit
import Foundation
import Observation

@MainActor
@Observable
final class LiveActivityManager {
    private(set) var activity: Activity<CookActivityAttributes>?
    private(set) var lastError: String?
    private var tokenTask: Task<Void, Never>?
    private var lastUpdate: Date = .distantPast
    private var lastState: CookActivityAttributes.ContentState?

    var enabled: Bool = (UserDefaults.standard.object(forKey: "hm.liveActivity") as? Bool) ?? true {
        didSet { UserDefaults.standard.set(enabled, forKey: "hm.liveActivity") }
    }

    var isRunning: Bool { activity != nil }
    var available: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    init() {
        // Adopt an activity left over from a previous launch.
        activity = Activity<CookActivityAttributes>.activities.first
    }

    /// Called on state changes and on a timer: reconcile the activity with
    /// whether a cook is running.
    func sync(client: HeaterMeterClient) async {
        guard let state = client.state else { return }
        let cooking = Self.cookIsActive(state)
        guard enabled, available else {
            await endIfNeeded(dismissAfter: 0)
            return
        }
        if !cooking {
            await endIfNeeded(dismissAfter: 30 * 60)
            return
        }
        guard Date().timeIntervalSince(lastUpdate) >= 15 else { return }
        guard let cs = try? await client.liveActivityState() else { return }
        lastState = cs
        lastUpdate = .now
        let content = ActivityContent(state: cs, staleDate: .now.addingTimeInterval(180))
        if let activity {
            await activity.update(content)
            return
        }
        let attrs = CookActivityAttributes(host: client.baseURL.host ?? "HeaterMeter", startedAt: .now)
        do {
            activity = try Activity.request(attributes: attrs, content: content, pushType: .token)
        } catch {
            // No push entitlement (e.g. the simulator): still show it, updated locally.
            do { activity = try Activity.request(attributes: attrs, content: content, pushType: nil) }
            catch { lastError = error.localizedDescription; return }
        }
        lastError = nil
        watchPushToken(client: client)
    }

    private func watchPushToken(client: HeaterMeterClient) {
        tokenTask?.cancel()
        guard let activity else { return }
        tokenTask = Task { [weak self] in
            for await data in activity.pushTokenUpdates {
                let hex = data.map { String(format: "%02x", $0) }.joined()
                try? await client.registerLiveActivityToken(hex)
                if self == nil { break }
            }
        }
    }

    private func endIfNeeded(dismissAfter secs: TimeInterval) async {
        guard let activity else { return }
        tokenTask?.cancel()
        let final = ActivityContent(state: lastState ?? activity.content.state, staleDate: nil)
        await activity.end(final, dismissalPolicy: secs > 0 ? .after(.now.addingTimeInterval(secs)) : .immediate)
        self.activity = nil
    }

    static func cookIsActive(_ s: DeviceState) -> Bool {
        if let sp = s.status.setPoint, sp > 0 { return true }
        for (probe, temp) in [(1, s.status.food1), (2, s.status.food2), (3, s.status.ambient)] {
            if temp != nil, s.target(forProbe: probe) != nil { return true }
        }
        return false
    }
}
