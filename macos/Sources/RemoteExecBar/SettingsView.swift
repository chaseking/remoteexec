import AppKit
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

/// How the app starts its backend. The full Settings live on the jobs
/// window's Settings page; this window is the way in when the backend can't
/// start (and that page can't load). A plain stack, explicitly sized: an
/// unsized grouped Form can come up blank in a hosting window.
struct BackendSettingsView: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var backend: Backend

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("remoteexec CLI").font(.headline)
            VStack(alignment: .leading, spacing: 6) {
                TextField("Command", text: $settings.backendCommand,
                          prompt: Text(Backend.detectCommand() ?? "auto-detect"))
                    .textFieldStyle(.roundedBorder)
                Text("How the app starts `remoteexec gui` in the background. Leave empty to find it "
                     + "automatically, or give e.g. ~/miniconda3/bin/remoteexec or "
                     + "/path/to/python -m remoteexec.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Text("Port")
                TextField("Port", value: $settings.port, format: .number.grouping(.never))
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 90)
            }
            Divider()
            HStack {
                Text(statusLine).foregroundStyle(.secondary)
                Spacer()
                Button("Open Log") { NSWorkspace.shared.open(backend.logURL) }
                Button("Restart Backend") { Task { await backend.restart() } }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 500)
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
