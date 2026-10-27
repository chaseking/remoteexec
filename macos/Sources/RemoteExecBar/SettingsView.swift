import SwiftUI

/// App preferences, stored in UserDefaults.
final class AppSettings: ObservableObject {
    /// How to launch the CLI; empty = auto-detect.
    @Published var backendCommand: String {
        didSet { UserDefaults.standard.set(backendCommand, forKey: "backendCommand") }
    }
    @Published var port: Int {
        didSet { UserDefaults.standard.set(port, forKey: "port") }
    }
    @Published var notify: Bool {
        didSet { UserDefaults.standard.set(notify, forKey: "notify") }
    }

    init() {
        let d = UserDefaults.standard
        backendCommand = d.string(forKey: "backendCommand") ?? ""
        let savedPort = d.integer(forKey: "port")
        port = savedPort > 0 ? savedPort : 8765  // same as `remoteexec gui`, so one can be reused
        notify = d.object(forKey: "notify") as? Bool ?? true
    }
}

struct SettingsView: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var backend: Backend
    let manageHosts: () -> Void

    var body: some View {
        Form {
            Section {
                TextField("Command", text: $settings.backendCommand,
                          prompt: Text(Backend.detectCommand() ?? "remoteexec"))
                TextField("Port", value: $settings.port, format: .number.grouping(.never))
                HStack {
                    Text(statusLine).foregroundStyle(.secondary)
                    Spacer()
                    Button("Restart Backend") { Task { await backend.restart() } }
                }
            } header: {
                Text("Backend")
            } footer: {
                Text("How to launch the remoteexec CLI (run in your login shell). Leave empty to "
                     + "auto-detect, or give e.g. ~/miniconda3/bin/remoteexec or python3 -m remoteexec. "
                     + "Log: ~/Library/Logs/remoteexec-menubar.log")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Clusters") {
                Button("Manage clusters & ssh hosts…", action: manageHosts)
            }
            Section("Notifications") {
                Toggle("Notify when a job finishes", isOn: $settings.notify)
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
        .onChange(of: settings.notify) { enabled in
            if enabled { Notifier.shared.setup() }
        }
    }

    private var statusLine: String {
        switch backend.status {
        case .idle: return "Not started"
        case .starting: return "Starting…"
        case .ready: return "Running on 127.0.0.1:\(settings.port)"
        case .failed: return "Failed to start — see the log"
        }
    }
}
