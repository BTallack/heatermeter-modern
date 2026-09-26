//  PushManager.swift
//  Remote-notification registration: asks for permission, hands the APNs device
//  token to the daemon (POST /api/push/register), and re-registers on launch
//  once permission has been granted.

import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate {
    static var onDeviceToken: ((Data) -> Void)?
    static var lastError: String?

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Self.lastError = nil
        Self.onDeviceToken?(deviceToken)
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Self.lastError = error.localizedDescription   // the simulator has no APNs
    }
}

@MainActor
enum PushManager {
    /// Ask for permission (first time) and register for a device token, which
    /// the AppDelegate forwards to the daemon.
    @discardableResult
    static func enable(client: HeaterMeterClient) async -> Bool {
        let center = UNUserNotificationCenter.current()
        let granted = (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        guard granted else { return false }
        register(client: client)
        return true
    }

    /// Refresh the token silently when permission was granted earlier.
    static func registerIfAuthorized(client: HeaterMeterClient) async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        guard settings.authorizationStatus == .authorized else { return }
        register(client: client)
    }

    static func authorizationStatus() async -> UNAuthorizationStatus {
        await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }

    private static func register(client: HeaterMeterClient) {
        AppDelegate.onDeviceToken = { data in
            Task { try? await client.registerPush(deviceToken: data) }
        }
        UIApplication.shared.registerForRemoteNotifications()
    }
}
