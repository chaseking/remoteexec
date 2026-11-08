import AppKit
import Foundation

/// Keeps the app up to date with the git checkout it was built from.
///
/// build.sh stamps the bundle with that checkout's path (`RXSourceDir`) and its
/// `pyproject.toml` version. Every few hours the app fetches the checkout's
/// upstream branch and reads the version there; a higher one is offered as an
/// update. Installing pulls, rebuilds with build.sh, refreshes a non-editable
/// CLI install, then swaps the bundle and relaunches.
@MainActor
final class Updater: ObservableObject {
    enum State: Equatable {
        case idle, checking, installing(String), failed(String)
    }

    /// The newer upstream version, if there is one.
    @Published private(set) var available: String?
    @Published private(set) var state: State = .idle
    /// The last check's result, for Settings ("Up to date", an error…).
    @Published private(set) var lastResult = ""
    let current: String
    /// The checkout the app was built from; nil turns updates off.
    let repo: URL?
    let logURL: URL
    /// Called after any change, so the jobs window can redraw its button.
    var onChange: (() -> Void)?

    private var lastCheck: Date?
    private var timer: Timer?

    init() {
        let info = Bundle.main.infoDictionary ?? [:]
        current = info["CFBundleShortVersionString"] as? String ?? "0"
        if let path = info["RXSourceDir"] as? String, !path.isEmpty,
           FileManager.default.fileExists(atPath: (path as NSString).appendingPathComponent(".git")) {
            repo = URL(fileURLWithPath: path, isDirectory: true)
        } else {
            repo = nil
        }
        logURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/remoteexec-update.log")
    }

    /// Check shortly after launch, then every four hours.
    func start() {
        guard repo != nil, timer == nil else { return }
        Task {
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            await check()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 4 * 3600, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.check() }
        }
    }

    /// Fetch upstream and compare versions. Without `force`, at most every 20 min.
    func check(force: Bool = false) async {
        guard let repo else { return }
        switch state {
        case .checking, .installing: return
        default: break
        }
        if !force, let last = lastCheck, Date().timeIntervalSince(last) < 20 * 60 { return }
        lastCheck = Date()
        state = .checking
        changed()
        let (status, output) = await Self.run(
            "git fetch --quiet && git show '@{u}:pyproject.toml'", in: repo, log: logURL)
        state = .idle
        if status == 0, let remote = Self.version(in: output) {
            available = Self.isNewer(remote, than: current) ? remote : nil
            lastResult = available == nil ? "Up to date" : "Version \(remote) is available"
        } else {
            lastResult = "Couldn't check for updates: " + Self.tail(output, lines: 2)
        }
        changed()
    }

    /// Pull, rebuild and relaunch. `reopenDashboard` reopens the jobs window.
    func install(reopenDashboard: Bool) async {
        guard let repo else { return }
        if case .installing = state { return }

        state = .installing("Downloading…")
        changed()
        var (status, output) = await Self.run("git pull --ff-only", in: repo, log: logURL)
        guard status == 0 else { return fail("git pull failed", output) }

        state = .installing("Building…")
        changed()
        (status, output) = await Self.run("./macos/build.sh", in: repo, log: logURL)
        guard status == 0 else { return fail("The build failed", output) }

        state = .installing("Updating the CLI…")
        changed()
        if let refresh = Self.cliRefreshScript(repo: repo) {
            // Best effort: an editable or source install needs nothing, and a
            // failure here shouldn't keep the new app from installing.
            _ = await Self.run(refresh, in: repo, log: logURL)
        }

        state = .installing("Relaunching…")
        changed()
        relaunch(with: repo.appendingPathComponent("macos/build/remoteexec.app"), reopenDashboard: reopenDashboard)
    }

    private func fail(_ what: String, _ output: String) {
        state = .failed("\(what): \(Self.tail(output, lines: 6))\n\nFull log: \(logURL.path)")
        changed()
    }

    private func changed() {
        onChange?()
    }

    /// Wait for this process to exit, put the new bundle where this one is, open it.
    private func relaunch(with built: URL, reopenDashboard: Bool) {
        let target = Bundle.main.bundleURL.standardizedFileURL.path
        let source = built.standardizedFileURL.path
        let pid = ProcessInfo.processInfo.processIdentifier
        let copy = target == source ? "" : """
            rm -rf "$TARGET" && cp -R "$SOURCE" "$TARGET" && touch "$TARGET"
            """
        let script = """
            while kill -0 \(pid) 2>/dev/null; do sleep 0.2; done
            \(copy)
            open "$TARGET" \(reopenDashboard ? "--args --open-dashboard" : "")
            """
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/nohup")
        p.arguments = ["/bin/bash", "-c", script]
        var env = ProcessInfo.processInfo.environment
        env["TARGET"] = target
        env["SOURCE"] = source
        p.environment = env
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.seekToEndOfFile()
            p.standardOutput = log
            p.standardError = log
        }
        p.standardInput = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            return fail("Couldn't relaunch", error.localizedDescription)
        }
        NSApp.terminate(nil)  // also stops the backend, so the new one starts fresh
    }

    // MARK: helpers

    /// Reinstall the CLI from the checkout when the recorded Python imports
    /// remoteexec from somewhere else (a plain `pip install .` / `uv tool install .`).
    private static func cliRefreshScript(repo: URL) -> String? {
        let url = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".remoteexec/cli.json")
        guard let data = try? Data(contentsOf: url),
              let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let python = info["python"] as? String,
              FileManager.default.isExecutableFile(atPath: python)
        else { return nil }
        let repoPath = repo.resolvingSymlinksInPath().path
        if let path = info["path"] as? String, !path.isEmpty,
           URL(fileURLWithPath: path).resolvingSymlinksInPath().path.hasPrefix(repoPath) {
            return nil  // runs straight from this checkout
        }
        let py = quote(python), src = quote(repoPath)
        return """
            loc=$(\(py) -c 'import os, remoteexec; print(os.path.realpath(remoteexec.__file__))' 2>/dev/null)
            case "$loc" in
              \(src)/*) echo "CLI runs from the checkout; nothing to reinstall" ;;
              *) \(py) -m pip install -q --upgrade \(src) || uv pip install -q --python \(py) --upgrade \(src) ;;
            esac
            """
    }

    /// Runs `script` in a login shell (so git finds the user's PATH, ssh agent
    /// and credentials), appending its output to the update log.
    nonisolated private static func run(_ script: String, in dir: URL, log: URL) async -> (Int32, String) {
        await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .utility).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh")
                p.arguments = ["-l", "-c", script]
                p.currentDirectoryURL = dir
                var env = ProcessInfo.processInfo.environment
                env["GIT_TERMINAL_PROMPT"] = "0"  // never wait on a password prompt nobody sees
                if env["GIT_SSH_COMMAND"] == nil { env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes" }
                p.environment = env
                let pipe = Pipe()
                p.standardOutput = pipe
                p.standardError = pipe
                p.standardInput = FileHandle.nullDevice
                do {
                    try p.run()
                } catch {
                    cont.resume(returning: (-1, error.localizedDescription))
                    return
                }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                let output = String(decoding: data, as: UTF8.self)
                if !FileManager.default.fileExists(atPath: log.path) {
                    FileManager.default.createFile(atPath: log.path, contents: nil)
                }
                if let h = try? FileHandle(forWritingTo: log) {
                    h.seekToEndOfFile()
                    h.write(Data("[\(Date())] $ \(script)\n\(output)\n[exit \(p.terminationStatus)]\n".utf8))
                    try? h.close()
                }
                cont.resume(returning: (p.terminationStatus, output))
            }
        }
    }

    /// The `version = "…"` line of a pyproject.toml.
    static func version(in toml: String) -> String? {
        guard let regex = try? NSRegularExpression(pattern: #"(?m)^version\s*=\s*"([^"]+)""#),
              let m = regex.firstMatch(in: toml, range: NSRange(toml.startIndex..., in: toml)),
              let r = Range(m.range(at: 1), in: toml)
        else { return nil }
        return String(toml[r])
    }

    /// Numeric, part-by-part comparison: 0.10.0 > 0.9.2.
    static func isNewer(_ a: String, than b: String) -> Bool {
        let parts = { (s: String) in s.split(whereSeparator: { !$0.isNumber }).map { Int($0) ?? 0 } }
        let x = parts(a), y = parts(b)
        for i in 0..<max(x.count, y.count) {
            let l = i < x.count ? x[i] : 0, r = i < y.count ? y[i] : 0
            if l != r { return l > r }
        }
        return false
    }

    private static func tail(_ s: String, lines: Int) -> String {
        s.split(separator: "\n").suffix(lines).joined(separator: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func quote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
