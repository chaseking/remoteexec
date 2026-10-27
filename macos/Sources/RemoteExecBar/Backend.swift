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
        let resolved = command
        let p = Process()
        p.executableURL = URL(fileURLWithPath: shell)
        // With nothing found, the shell has to find `remoteexec` on its PATH. An
        // app's shell is non-interactive, which skips ~/.zshrc, where conda and
        // pyenv usually set PATH up, so ask for an interactive one then.
        p.arguments = (resolved == nil ? ["-l", "-i", "-c"] : ["-l", "-c"])
            + ["exec \(resolved ?? "remoteexec") gui --no-browser --port \(settings.port) --parent-pid \(pid)"]
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
        if tail.contains("command not found") || tail.contains("No module named remoteexec") {
            status = .failed("""
            Couldn't find the remoteexec CLI. Run any remoteexec command once in Terminal \
            (e.g. `remoteexec ls`) so the app learns where it is installed, or set the \
            command in Settings.

            \(tail)
            """)
        } else {
            status = .failed(tail.isEmpty ? "The remoteexec backend did not start." : tail)
        }
        return false
    }

    /// The configured command, else the detected one; nil means "let the
    /// shell's PATH find `remoteexec`".
    var command: String? {
        let custom = settings.backendCommand.trimmingCharacters(in: .whitespaces)
        if !custom.isEmpty { return custom }
        return Backend.detectCommand()
    }

    /// How to run the CLI without relying on PATH: the Python that last ran
    /// remoteexec (recorded in ~/.remoteexec/cli.json), else a `remoteexec`
    /// script in a usual install location.
    static func detectCommand() -> String? {
        if let recorded = recordedCommand() { return recorded }
        return candidates().first { FileManager.default.isExecutableFile(atPath: $0) }.map(quote)
    }

    private static func recordedCommand() -> String? {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".remoteexec/cli.json")
        guard let data = try? Data(contentsOf: url),
              let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let python = info["python"] as? String,
              FileManager.default.isExecutableFile(atPath: python)
        else { return nil }
        let path = info["path"] as? String ?? ""
        // PYTHONPATH covers a source checkout run without installing.
        return (path.isEmpty ? "" : "env PYTHONPATH=\(quote(path)) ") + "\(quote(python)) -m remoteexec"
    }

    private static func candidates() -> [String] {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        var paths = [
            "\(home)/.local/bin/remoteexec",  // uv tool / pipx
            "/opt/homebrew/bin/remoteexec",
            "/usr/local/bin/remoteexec",
            "\(home)/.pyenv/shims/remoteexec",
        ]
        let condas = ["\(home)/miniforge3", "\(home)/miniconda3", "\(home)/anaconda3", "\(home)/mambaforge",
                      "\(home)/micromamba", "/opt/miniconda3", "/opt/anaconda3",
                      "/opt/homebrew/Caskroom/miniconda/base"]
        for base in condas {
            paths.append("\(base)/bin/remoteexec")
            let envs = (try? fm.contentsOfDirectory(atPath: "\(base)/envs")) ?? []
            paths += envs.sorted().map { "\(base)/envs/\($0)/bin/remoteexec" }
        }
        let userPythons = (try? fm.contentsOfDirectory(atPath: "\(home)/Library/Python")) ?? []
        paths += userPythons.sorted().reversed().map { "\(home)/Library/Python/\($0)/bin/remoteexec" }
        return paths
    }

    private static func quote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
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
