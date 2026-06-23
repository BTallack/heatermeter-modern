//  HeaterMeterApp.swift
//  App entry. Gates on a configured connection: no host yet -> onboarding;
//  otherwise the four-tab control app, with the live client in the environment.

import SwiftUI

@main
struct HeaterMeterApp: App {
    @State private var conn = ConnectionStore()

    var body: some Scene {
        WindowGroup {
            if let client = conn.client {
                RootView(onDisconnect: { conn.disconnect() })
                    .environment(client)
                    .environment(conn)
            } else {
                OnboardingView(conn: conn)
            }
        }
    }
}

struct RootView: View {
    @Environment(HeaterMeterClient.self) private var client
    var onDisconnect: () -> Void

    var body: some View {
        TabView {
            NavigationStack { DashboardView() }
                .tabItem { Label("Dashboard", systemImage: "flame") }
            NavigationStack { GraphView() }
                .tabItem { Label("Graph", systemImage: "chart.xyaxis.line") }
            NavigationStack { CookView() }
                .tabItem { Label("Cook", systemImage: "fork.knife") }
            NavigationStack { SettingsView(onDisconnect: onDisconnect) }
                .tabItem { Label("Settings", systemImage: "slider.horizontal.3") }
        }
        .tint(.orange)
        .task { client.connect() }       // live WebSocket for the whole session
    }
}
