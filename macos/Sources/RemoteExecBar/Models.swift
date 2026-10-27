import Foundation

/// One row of `GET /api/jobs` (see remoteexec/gui/service.py: build_row).
struct JobRow: Decodable, Identifiable, Equatable {
    let jobId: String
    let name: String
    let status: String
    let host: String
    let running: String
    let startedIso: String
    let end: String
    let partition: String
    let env: String
    let kind: String?
    let command: String?

    var id: String { jobId }

    // Explicit keys: a snake_case key strategy would also rewrite dictionary
    // keys elsewhere (cluster names), so it isn't used.
    enum CodingKeys: String, CodingKey {
        case jobId = "job_id", name, status, host, running
        case startedIso = "started_iso", end, partition, env, kind, command
    }
}

struct JobsResponse: Decodable {
    let jobs: [JobRow]
    let errors: [String: String]?
}

/// `~/.remoteexec/hosts.json`: which ssh alias each cluster goes through.
struct ClusterRoute: Codable, Equatable {
    var hosts: [String]
    var active: String
}

struct HostsInfo: Decodable, Equatable {
    let clusters: [String: ClusterRoute]
    let seen: [String]
}

enum JobPhase {
    case running, queued, completed, failed, stopped, unknown
}

extension JobRow {
    var phase: JobPhase {
        switch status.uppercased() {
        case "RUNNING", "COMPLETING", "CONFIGURING", "RESIZING": return .running
        case "PENDING", "REQUEUED", "SUSPENDED": return .queued
        case "COMPLETED": return .completed
        case "FAILED", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL": return .failed
        case "CANCELLED", "TIMEOUT", "DEADLINE", "PREEMPTED": return .stopped
        default: return .unknown
        }
    }

    var isActive: Bool { phase == .running || phase == .queued }
    var endDate: Date? { SlurmTime.parse(end) }
}

enum SlurmTime {
    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()

    /// Slurm/Python timestamps without a zone, in local time.
    static func parse(_ raw: String) -> Date? {
        guard raw.count >= 19 else { return nil }
        return formatter.date(from: String(raw.prefix(19)))
    }

    static func ago(_ date: Date, now: Date = Date()) -> String {
        let s = max(0, Int(now.timeIntervalSince(date)))
        if s < 60 { return "just now" }
        if s < 3600 { return "\(s / 60)m ago" }
        if s < 86400 { return "\(s / 3600)h ago" }
        return "\(s / 86400)d ago"
    }
}
