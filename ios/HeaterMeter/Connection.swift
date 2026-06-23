//  Connection.swift
//  Persists the HeaterMeter host (and optional auth token) and vends the live
//  client. Plus the onboarding screen that captures + validates a host.

import SwiftUI
import Observation

@MainActor
@Observable
final class ConnectionStore {
    private let urlKey = "hm.baseURL"
    private let tokenKey = "hm.token"

    private(set) var client: HeaterMeterClient?

    init() {
        let d = UserDefaults.standard
        if let s = d.string(forKey: urlKey), let url = URL(string: s) {
            client = HeaterMeterClient(baseURL: url,
                                       authToken: d.string(forKey: tokenKey))
        }
    }

    /// Normalize a user-typed host into a base URL. Accepts "192.168.3.164:8080",
    /// "hm.local", or a full "http(s)://…". Defaults to http + :8080.
    static func normalize(_ raw: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "http://" + s }
        guard var comps = URLComponents(string: s), let host = comps.host,
              !host.isEmpty else { return nil }
        if comps.port == nil && comps.scheme == "http" { comps.port = 8080 }
        comps.path = ""
        return comps.url
    }

    func configure(host: String, token: String?) -> URL? {
        guard let url = Self.normalize(host) else { return nil }
        let t = (token?.isEmpty == false) ? token : nil
        UserDefaults.standard.set(url.absoluteString, forKey: urlKey)
        UserDefaults.standard.set(t, forKey: tokenKey)
        client = HeaterMeterClient(baseURL: url, authToken: t)
        return url
    }

    func disconnect() {
        client?.disconnect()
        client = nil
        UserDefaults.standard.removeObject(forKey: urlKey)
        UserDefaults.standard.removeObject(forKey: tokenKey)
    }
}

struct OnboardingView: View {
    let conn: ConnectionStore
    @State private var host = ""
    @State private var token = ""
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("192.168.3.164:8080", text: $host)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    SecureField("Password (if auth is on)", text: $token)
                } header: {
                    Text("Connect to HeaterMeter")
                } footer: {
                    Text("Enter your HeaterMeter's address on the LAN. Leave the password blank unless you turned auth on.")
                }

                if let error {
                    Section { Text(error).foregroundStyle(.red).font(.footnote) }
                }

                Section {
                    Button {
                        Task { await connect() }
                    } label: {
                        HStack {
                            Text("Connect")
                            if checking { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(host.isEmpty || checking)
                }
            }
            .navigationTitle("HeaterMeter")
        }
    }

    private func connect() async {
        checking = true; error = nil
        defer { checking = false }
        guard let url = ConnectionStore.normalize(host) else {
            error = "That doesn't look like a valid address."; return
        }
        // Validate before persisting, so a typo doesn't strand the app.
        let probe = HeaterMeterClient(baseURL: url,
                                      authToken: token.isEmpty ? nil : token)
        do {
            _ = try await probe.fetchStatus()
            _ = conn.configure(host: host, token: token)
        } catch {
            self.error = "Couldn't reach it: \(error.localizedDescription)"
        }
    }
}
