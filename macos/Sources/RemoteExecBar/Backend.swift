import Foundation

enum BackendError: LocalizedError {
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case let .http(code, body):
            return "backend returned \(code): \(body.prefix(200))"
        }
    }
}

/// The local `remoteexec gui` server: everything (job list, logs, files, host
/// routing) comes from its JSON API, and the dashboard window is its web UI.
/// An already-running server on the port is reused; otherwise one is started
/// through the login shell (so conda/uv/pipx PATHs work) and stopped on quit.
@MainActor
final class Backend: ObservableObject {
    enum Status: Equatable {
        case idle, starting, ready, failed(String)
    }

    @Published private(set) var status: Status = .idle
    let settings: AppSettings
    let logURL: URL
    private var process: Process?
    private var launching: Task<Bool, Never>?
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]  // never route localhost via a proxy
        config.timeoutIntervalForRequest = 45
        return URLSession(configuration: config)
    }()

    init(settings: AppSettings) {
        self.settings = settings
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        logURL = logs.appendingPathComponent("remoteexec-menubar.log")
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(settings.port)")! }

    func url(_ path: String) -> URL {
        URL(string: path, relativeTo: baseURL)!.absoluteURL
    }

    var failureMessage: String? {
        if case let .failed(message) = status { return message }
        return nil
    }

    // MARK: lifecycle

    /// Make sure a backend answers on the port, starting one if needed.
    func ensureRunning() async -> Bool {
        if let pending = launching { return await pending.value }
        if await health() {
            status = .ready
            return true
        }
        let task = Task { await self.launch() }
        launching = task
        let ok = await task.value
        launching = nil
        return ok
    }

    func restart() async {
        stop()
        status = .idle
        _ = await ensureRunning()
    }

    func stop() {
        if let p = process, p.isRunning { p.terminate() }
        process = nil
    }

    private func launch() async -> Bool {
        status = .starting
        stop()
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let pid = ProcessInfo.processInfo.processIdentifier
        let p = Process()
        p.executableURL = URL(fileURLWithPath: shell)
        p.arguments = ["-l", "-c",
                       "exec \(command) gui --no-browser --port \(settings.port) --parent-pid \(pid)"]
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let log = try? FileHandle(forWritingTo: logURL) {
            p.standardOutput = log
            p.standardError = log
        }
        p.standardInput = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            status = .failed("Could not run \(shell): \(error.localizedDescription)")
            return false
        }
        process = p
        for _ in 0..<80 {  // ~20 s: a login shell plus Python startup can be slow
            try? await Task.sleep(nanoseconds: 250_000_000)
            if await health() {
                status = .ready
                return true
            }
            if !p.isRunning { break }
        }
        if p.isRunning { p.terminate() }
        let tail = logTail()
        status = .failed(tail.isEmpty ? "The remoteexec backend did not start." : tail)
        return false
    }

    /// The configured command, else the first `remoteexec` in a usual install
    /// location, else whatever the login shell's PATH finds.
    var command: String {
        let custom = settings.backendCommand.trimmingCharacters(in: .whitespaces)
        if !custom.isEmpty { return custom }
        return Backend.detectCommand() ?? "remoteexec"
    }

    static func detectCommand() -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "\(home)/.local/bin/remoteexec",  // uv tool / pipx
            "/opt/homebrew/bin/remoteexec",
            "/usr/local/bin/remoteexec",
            "\(home)/miniforge3/bin/remoteexec",
            "\(home)/miniconda3/bin/remoteexec",
            "\(home)/anaconda3/bin/remoteexec",
            "\(home)/mambaforge/bin/remoteexec",
            "/opt/miniconda3/bin/remoteexec",
            "/opt/anaconda3/bin/remoteexec",
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }.map { "'\($0)'" }
    }

    func logTail(_ bytes: Int = 1500) -> String {
        guard let data = try? Data(contentsOf: logURL), !data.isEmpty else { return "" }
        return String(decoding: data.suffix(bytes), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: HTTP

    func health() async -> Bool {
        var request = URLRequest(url: url("/api/health"))
        request.timeoutInterval = 2
        guard let result = try? await session.data(for: request),
              (result.1 as? HTTPURLResponse)?.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: result.0) as? [String: Any]
        else { return false }
        return object["ok"] as? Bool == true
    }

    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        let (data, response) = try await session.data(from: url(path))
        try check(response, data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    func post(_ path: String, json: [String: Any]) async throws -> Data {
        var request = URLRequest(url: url(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("1", forHTTPHeaderField: "X-Remoteexec")  // required by the server for POSTs
        request.httpBody = try JSONSerialization.data(withJSONObject: json)
        let (data, response) = try await session.data(for: request)
        try check(response, data)
        return data
    }

    private func check(_ response: URLResponse, _ data: Data) throws {
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code != 200 {
            throw BackendError.http(code, String(decoding: data, as: UTF8.self))
        }
    }
}
