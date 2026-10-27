import SwiftUI

struct MenuActions {
    var openJob: (String) -> Void
    var openDashboard: () -> Void
    var openHosts: () -> Void
    var openSettings: () -> Void
    var quit: () -> Void
}

/// The dropdown: running jobs, then jobs that finished in the last 6 hours.
struct MenuView: View {
    @ObservedObject var store: JobStore
    @ObservedObject var backend: Backend
    let actions: MenuActions

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
            Divider()
            footer
        }
        .frame(width: 380)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "terminal").foregroundStyle(.secondary)
            Text("remoteexec").font(.system(size: 13, weight: .semibold))
            Spacer()
            RouteMenu(store: store, openHosts: actions.openHosts)
            Button {
                Task { await store.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Refresh")
            .disabled(store.refreshing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    @ViewBuilder private var content: some View {
        if let message = backend.failureMessage, store.jobs.isEmpty {
            BackendErrorView(message: message, retry: {
                Task {
                    await backend.restart()
                    await store.refresh()
                }
            }, openSettings: actions.openSettings)
        } else if store.lastUpdate == nil {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(backend.status == .starting ? "Starting backend…" : "Loading jobs…")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 80)
        } else if store.jobs.count > 9 {
            ScrollView { jobList }.frame(height: 420)
        } else {
            jobList
        }
    }

    private var jobList: some View {
        VStack(alignment: .leading, spacing: 1) {
            ForEach(store.hostErrors.keys.sorted(), id: \.self) { host in
                HostErrorRow(host: host, message: store.hostErrors[host] ?? "", openHosts: actions.openHosts)
            }
            if let error = store.error {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
                    .lineLimit(3)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 4)
            }
            if store.jobs.isEmpty {
                Text("No running or recently finished jobs")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 64)
            }
            if !store.active.isEmpty {
                SectionHeader(title: "Running", count: store.active.count)
                ForEach(store.active) { job in
                    JobRowView(job: job) { actions.openJob(job.jobId) }
                }
            }
            if !store.finished.isEmpty {
                SectionHeader(title: "Finished · last 6h", count: store.finished.count)
                ForEach(store.finished) { job in
                    JobRowView(job: job) { actions.openJob(job.jobId) }
                }
            }
        }
        .padding(.vertical, 6)
    }

    private var footer: some View {
        HStack(spacing: 14) {
            Button("Open Dashboard", action: actions.openDashboard)
            Spacer()
            if let updated = store.lastUpdate {
                Text(updated, style: .time)
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .help("Last updated")
            }
            Button("Settings…", action: actions.openSettings)
            Button("Quit", action: actions.quit)
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
    }
}

// Hover/pulse state avoids `@State`: in recent SDKs it is a macro whose compiler plugin
// ships only with Xcode, so `@State` breaks builds on the Command Line Tools alone.
final class HoverState: ObservableObject {
    @Published var on = false
}

struct JobRowView: View {
    let job: JobRow
    let open: () -> Void
    @StateObject private var hover = HoverState()

    var body: some View {
        Button(action: open) {
            HStack(spacing: 10) {
                StatusIcon(phase: job.phase)
                VStack(alignment: .leading, spacing: 1) {
                    Text(job.name)
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(subtitle)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                Text(timeText)
                    .font(.system(size: 12).monospacedDigit())
                    .foregroundStyle(job.isActive ? Color.primary : Color.secondary)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .contentShape(Rectangle())
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(hover.on ? Color.primary.opacity(0.08) : Color.clear)
            )
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 6)
        .onHover { hover.on = $0 }
        .help(job.command ?? job.name)
    }

    private var subtitle: String {
        var parts = [job.jobId, job.host]
        if job.partition != "—" { parts.append(job.partition) }
        return parts.joined(separator: " · ")
    }

    private var timeText: String {
        switch job.phase {
        case .queued:
            return "queued"
        case .running:
            return job.running
        default:
            if let end = job.endDate { return SlurmTime.ago(end) }
            return job.status.capitalized
        }
    }
}

struct StatusIcon: View {
    let phase: JobPhase

    var body: some View {
        if phase == .running {
            // pulse between 0.35 and 1 opacity every 1.8s
            TimelineView(.animation) { context in
                let t = context.date.timeIntervalSinceReferenceDate
                icon.opacity(0.675 + 0.325 * cos(t * 2 * .pi / 1.8))
            }
        } else {
            icon
        }
    }

    private var icon: some View {
        Image(systemName: symbol)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(color)
            .frame(width: 16)
    }

    private var symbol: String {
        switch phase {
        case .running: return "circle.fill"
        case .queued: return "circle.dashed"
        case .completed: return "checkmark.circle.fill"
        case .failed: return "xmark.circle.fill"
        case .stopped: return "minus.circle.fill"
        case .unknown: return "questionmark.circle"
        }
    }

    private var color: Color {
        switch phase {
        case .running: return .orange
        case .queued: return .blue
        case .completed: return .green
        case .failed: return .red
        case .stopped, .unknown: return .secondary
        }
    }
}

struct SectionHeader: View {
    let title: String
    let count: Int

    var body: some View {
        HStack(spacing: 6) {
            Text(title.uppercased())
            Text("\(count)").foregroundStyle(.tertiary)
        }
        .font(.system(size: 10.5, weight: .semibold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 3)
    }
}

/// "axon → axon-remote" switcher for each registered cluster.
struct RouteMenu: View {
    @ObservedObject var store: JobStore
    let openHosts: () -> Void

    var body: some View {
        Menu {
            let clusters = store.hosts?.clusters ?? [:]
            ForEach(clusters.keys.sorted(), id: \.self) { name in
                let route = clusters[name] ?? ClusterRoute(hosts: [name], active: name)
                Section(name) {
                    ForEach(route.hosts + ["auto"], id: \.self) { alias in
                        Button {
                            Task { await store.setRoute(cluster: name, alias: alias) }
                        } label: {
                            let title = alias == "auto" ? "Automatic (first that answers)" : alias
                            if route.active == alias {
                                Label(title, systemImage: "checkmark")
                            } else {
                                Text(title)
                            }
                        }
                    }
                }
            }
            if !clusters.isEmpty { Divider() }
            Button("Manage clusters & ssh hosts…", action: openHosts)
        } label: {
            Text(summary).font(.system(size: 12))
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Which ssh alias each cluster connects through")
    }

    private var summary: String {
        guard let clusters = store.hosts?.clusters, !clusters.isEmpty else { return "hosts" }
        return clusters.keys.sorted().map { name in
            let active = clusters[name]?.active ?? name
            return active == name ? name : "\(name) → \(active)"
        }.joined(separator: ", ")
    }
}

struct HostErrorRow: View {
    let host: String
    let message: String
    let openHosts: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text("Can't reach \(host)").font(.system(size: 12, weight: .medium))
                Text(message.components(separatedBy: "\n").first ?? message)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 4)
            Button("Switch alias…", action: openHosts)
                .buttonStyle(.borderless)
                .font(.system(size: 11))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
    }
}

struct BackendErrorView: View {
    let message: String
    let retry: () -> Void
    let openSettings: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("The remoteexec backend isn't running", systemImage: "exclamationmark.triangle.fill")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.orange)
            ScrollView {
                Text(message)
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 110)
            Text("Install the CLI (pip install -e <repo>) or set the command in Settings.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            HStack {
                Button("Retry", action: retry)
                Button("Settings…", action: openSettings)
            }
        }
        .padding(14)
    }
}
