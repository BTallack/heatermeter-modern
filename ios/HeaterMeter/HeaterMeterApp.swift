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

enum AppTab: String { case dashboard, graph, cook, settings }

struct RootView: View {
    @Environment(HeaterMeterClient.self) private var client
    var onDisconnect: () -> Void
    // `-hm.initialTab cook` as a launch argument (Xcode scheme or `simctl launch`)
    // opens that tab first; UserDefaults exposes launch arguments without persisting them.
    @State private var tab = AppTab(rawValue: UserDefaults.standard.string(forKey: "hm.initialTab") ?? "") ?? .dashboard

    var body: some View {
        TabView(selection: $tab) {
            NavigationStack { DashboardView() }
                .tabItem { Label("Dashboard", systemImage: "flame") }
                .tag(AppTab.dashboard)
            NavigationStack { GraphView() }
                .tabItem { Label("Graph", systemImage: "chart.xyaxis.line") }
                .tag(AppTab.graph)
            NavigationStack { CookView() }
                .tabItem { Label("Cook", systemImage: "fork.knife") }
                .tag(AppTab.cook)
            NavigationStack { SettingsView(onDisconnect: onDisconnect) }
                .tabItem { Label("Settings", systemImage: "slider.horizontal.3") }
                .tag(AppTab.settings)
        }
        .tint(.orange)
        .task { client.connect() }       // live WebSocket for the whole session
    }
}
