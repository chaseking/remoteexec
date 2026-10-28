import Foundation
import UserNotifications

/// Polls the backend for running + recently finished jobs: every few seconds
/// while the dropdown is open, every 30 s otherwise (for the menu bar count
/// and finish notifications).
@MainActor
final class JobStore: ObservableObject {
    @Published private(set) var jobs: [JobRow] = []
    @Published private(set) var hostErrors: [String: String] = [:]
    @Published private(set) var hosts: HostsInfo?
    @Published private(set) var error: String?
    /// Minute of the last successful poll (only published when it changes).
    @Published private(set) var lastUpdate: Date?
    // Not @Published: every change re-renders the dropdown, and these flip on
    // every poll. Views get updates only when the data they show changes.
    private(set) var refreshing = false
    private var lastPoll: Date?

    /// Poll quickly (the dropdown is open).
    var fast = false

    let backend: Backend
    let settings: AppSettings
    private var pollTask: Task<Void, Never>?
    private var lastPhase: [String: JobPhase] = [:]

    init(backend: Backend, settings: AppSettings) {
        self.backend = backend
        self.settings = settings
    }

    var active: [JobRow] { jobs.filter { $0.isActive } }

    var finished: [JobRow] {
        jobs.filter { !$0.isActive }
            .sorted { ($0.endDate ?? .distantPast) > ($1.endDate ?? .distantPast) }
    }

    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                var waited = 0
                while waited < (self.fast ? 4 : 30), !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    waited += 1
                }
            }
        }
    }

    func refresh() async {
        if refreshing { return }
        refreshing = true
        defer { refreshing = false }
        guard await backend.ensureRunning() else {
            error = backend.failureMessage ?? "backend not running"
            return
        }
        do {
            let response: JobsResponse = try await backend.get("/api/jobs?filters=running,finished_6h")
            notifyFinished(response.jobs, since: lastPoll)
            lastPoll = Date()
            if jobs != response.jobs { jobs = response.jobs }
            let errors = response.errors ?? [:]
            if hostErrors != errors { hostErrors = errors }
            if error != nil { error = nil }
            let minute = Calendar.current.dateInterval(of: .minute, for: Date())?.start ?? Date()
            if lastUpdate != minute { lastUpdate = minute }
        } catch {
            if self.error != error.localizedDescription { self.error = error.localizedDescription }
        }
        if let info: HostsInfo = try? await backend.get("/api/hosts"), info != hosts {
            hosts = info
        }
    }

    // MARK: host routing

    func setRoute(cluster: String, alias: String) async {
        guard var clusters = hosts?.clusters, clusters[cluster] != nil else { return }
        clusters[cluster]?.active = alias
        let payload = clusters.mapValues { ["hosts": $0.hosts, "active": $0.active] as [String: Any] }
        do {
            let data = try await backend.post("/api/hosts", json: ["clusters": payload])
            hosts = try JSONDecoder().decode(HostsInfo.self, from: data)
        } catch {
            self.error = error.localizedDescription
        }
        await refresh()
    }

    // MARK: notifications

    /// Notify for jobs that were running at the last poll and have ended now,
    /// and for short jobs that started and ended between two polls.
    private func notifyFinished(_ rows: [JobRow], since previous: Date?) {
        if let previous, settings.notify {
            for row in rows where !row.isActive && row.phase != .unknown {
                if let before = lastPhase[row.jobId] {
                    if before == .running || before == .queued { Notifier.shared.jobFinished(row) }
                } else if let end = row.endDate, end > previous {
                    Notifier.shared.jobFinished(row)
                }
            }
        }
        var next: [String: JobPhase] = [:]
        for row in rows { next[row.jobId] = row.phase }
        lastPhase = next
    }
}

final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()
    var onOpenJob: ((String) -> Void)?

    /// UNUserNotificationCenter needs a real app bundle (not `swift run`).
    private var available: Bool { Bundle.main.bundleIdentifier != nil }

    func setup() {
        guard available else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func jobFinished(_ job: JobRow) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = job.phase == .completed ? "\(job.name) finished" : "\(job.name): \(job.status.lowercased())"
        content.body = "Job \(job.jobId) on \(job.host)" + (job.running == "—" ? "" : " · ran \(job.running)")
        content.userInfo = ["job": job.jobId]
        content.sound = .default
        let request = UNNotificationRequest(identifier: "job-\(job.jobId)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { _ in }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let id = response.notification.request.content.userInfo["job"] as? String {
            DispatchQueue.main.async { self.onOpenJob?(id) }
        }
        completionHandler()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}
