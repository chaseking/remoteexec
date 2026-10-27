import AppKit
import Combine
import SwiftUI

@main
struct RemoteExecBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        // A menu bar app has no main window; the status item, dropdown and
        // windows are managed by AppDelegate. This scene only supplies menus.
        Settings { EmptyView() }
            .commands {
                CommandGroup(replacing: .appSettings) {
                    Button("Settings…") { appDelegate.openSettings() }
                        .keyboardShortcut(",", modifiers: .command)
                }
                CommandGroup(replacing: .newItem) {
                    Button("Close Window") { NSApp.keyWindow?.performClose(nil) }
                        .keyboardShortcut("w", modifiers: .command)
                }
                CommandGroup(after: .toolbar) {
                    Button("Reload Dashboard") { appDelegate.dashboard.reload() }
                        .keyboardShortcut("r", modifiers: .command)
                }
            }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let settings = AppSettings()
    lazy var backend = Backend(settings: settings)
    lazy var store = JobStore(backend: backend, settings: settings)
    lazy var dashboard = DashboardController(backend: backend)

    private var statusItem: NSStatusItem?
    private let popover = NSPopover()
    private var settingsWindow: NSWindow?
    private var popoverCloseObserver: Any?
    private var subscriptions = Set<AnyCancellable>()

    /// Menu bar icon (SF Symbols). Change these to restyle the app.
    static let iconIdle = "terminal"
    static let iconBusy = "terminal.fill"

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.image = Self.statusImage(busy: false)
            button.imagePosition = .imageLeading
            button.toolTip = "remoteexec"
            button.target = self
            button.action = #selector(togglePopover(_:))
        }
        statusItem = item

        let actions = MenuActions(
            openJob: { [weak self] id in self?.openDashboard(path: "/?job=\(id)") },
            openDashboard: { [weak self] in self?.openDashboard(path: "/") },
            openHosts: { [weak self] in self?.openDashboard(path: "/?view=settings") },
            openSettings: { [weak self] in self?.openSettings() },
            openBackendSettings: { [weak self] in self?.openBackendSettings() },
            quit: { NSApp.terminate(nil) }
        )
        let hosting = NSHostingController(rootView: MenuView(store: store, backend: backend, actions: actions))
        hosting.sizingOptions = [.preferredContentSize]
        popover.contentViewController = hosting
        popover.behavior = .transient
        popover.animates = false
        popoverCloseObserver = NotificationCenter.default.addObserver(
            forName: NSPopover.didCloseNotification, object: popover, queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in self?.store.fast = false }
        }

        dashboard.onClose = { [weak self] in self?.refreshActivationPolicy() }
        dashboard.onOpenBackendSettings = { [weak self] in self?.openBackendSettings() }
        store.$jobs
            .receive(on: RunLoop.main)
            .sink { [weak self] jobs in self?.updateStatusItem(jobs) }
            .store(in: &subscriptions)

        Notifier.shared.onOpenJob = { [weak self] id in
            Task { @MainActor [weak self] in self?.openDashboard(path: "/?job=\(id)") }
        }
        if settings.notify { Notifier.shared.setup() }
        store.startPolling()
        if CommandLine.arguments.contains("--open-dashboard") {
            openDashboard(path: "/")
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        backend.stop()
    }

    // MARK: status item + dropdown

    @objc private func togglePopover(_ sender: Any?) {
        guard let button = statusItem?.button else { return }
        if popover.isShown {
            popover.performClose(sender)
            return
        }
        store.fast = true
        Task { await store.refresh() }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApp.activate(ignoringOtherApps: true)
        popover.contentViewController?.view.window?.makeKey()
    }

    private func updateStatusItem(_ jobs: [JobRow]) {
        guard let button = statusItem?.button else { return }
        let running = jobs.filter { $0.phase == .running }.count
        let queued = jobs.filter { $0.phase == .queued }.count
        let busy = running + queued > 0
        button.image = Self.statusImage(busy: busy)
        button.title = busy ? " \(running + queued)" : ""
        button.toolTip = busy ? "remoteexec — \(running) running, \(queued) queued" : "remoteexec"
    }

    static func statusImage(busy: Bool) -> NSImage? {
        let image = NSImage(systemSymbolName: busy ? iconBusy : iconIdle,
                            accessibilityDescription: "remoteexec")
        image?.isTemplate = true
        return image
    }

    // MARK: windows

    func openDashboard(path: String) {
        popover.performClose(nil)
        NSApp.setActivationPolicy(.regular)  // Dock icon + Cmd-Tab while a window is open
        dashboard.show(path: path)
    }

    /// Settings live on the jobs window's Settings page (clusters & hosts,
    /// and the app's own options).
    func openSettings() {
        openDashboard(path: "/?view=settings")
    }

    /// Fallback for when the backend can't start, so the Settings page can't load.
    func openBackendSettings() {
        popover.performClose(nil)
        if settingsWindow == nil {
            let view = BackendSettingsView(settings: settings, backend: backend)
            let window = NSWindow(contentViewController: NSHostingController(rootView: view))
            window.title = "Backend Settings"
            window.styleMask = [.titled, .closable]
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            settingsWindow = window
        }
        NSApp.setActivationPolicy(.regular)
        settingsWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        refreshActivationPolicy()
    }

    /// Back to a menu-bar-only app once no window is open.
    private func refreshActivationPolicy() {
        Task { @MainActor [weak self] in
            guard let self else { return }
            let open = [self.settingsWindow, self.dashboard.window].contains { $0?.isVisible == true }
            NSApp.setActivationPolicy(open ? .regular : .accessory)
        }
    }
}
