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
    let updater = Updater()
    lazy var dashboard = DashboardController(backend: backend, updater: updater)

    private var statusItem: NSStatusItem?
    private var dropdown: DropdownPanel?
    private var settingsWindow: NSWindow?
    private var globalClickMonitor: Any?
    private var localClickMonitor: Any?
    private var statusClickMonitor: Any?
    /// The menu bar runs the dropdown's lifecycle (macOS 27; see `useExpandedInterface`).
    private var usesExpandedInterface = false
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
        }
        statusItem = item
        usesExpandedInterface = Self.useExpandedInterface(item, delegate: self)
        if !usesExpandedInterface {
            // Before macOS 27: open and close the dropdown by hand. Mouse clicks on the
            // icon are taken here, on the press (as system menus open), before the
            // button sees them: its own click handling clears the highlight when the
            // mouse is released, and the icon should stay highlighted while open.
            item.button?.target = self
            item.button?.action = #selector(toggleDropdown(_:))  // VoiceOver and keyboard
            statusClickMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] event in
                guard let self, let window = self.statusItem?.button?.window, event.window === window else { return event }
                self.toggleDropdown(nil)
                return nil
            }
        }

        let actions = MenuActions(
            openJob: { [weak self] id in self?.openDashboard(path: "/?job=\(id)") },
            openDashboard: { [weak self] in self?.openDashboard(path: "/") },
            openHosts: { [weak self] in self?.openDashboard(path: "/?view=settings") },
            openSettings: { [weak self] in self?.openSettings() },
            openBackendSettings: { [weak self] in self?.openBackendSettings() },
            quit: { NSApp.terminate(nil) }
        )
        let panel = DropdownPanel(rootView: MenuView(store: store, backend: backend, updater: updater,
                                                     actions: actions))
        dropdown = panel

        updater.onChange = { [weak self] in self?.dashboard.pushUpdate() }
        updater.start(minutes: settings.updateCheckMinutes)
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

    /// On macOS 27 and later the menu bar runs the dropdown's lifecycle: a click on the
    /// icon opens it through `NSStatusItemExpandedInterfaceDelegate` (`showDropdown`), and
    /// the menu bar highlights the icon while it is open. Closing it is up to the app
    /// (`closeDropdown` cancels the session; the menu bar then calls `hideDropdown`),
    /// including on a second click on the icon, which reaches only the global monitor.
    private static func useExpandedInterface(_ item: NSStatusItem, delegate: AppDelegate) -> Bool {
        #if compiler(>=6.4)  // a toolchain with the macOS 27 SDK
        if #available(macOS 27.0, *) {
            item.expandedInterfaceDelegate = delegate
            return true
        }
        #endif
        return false
    }

    /// The icon was clicked (before macOS 27; see `useExpandedInterface`).
    @objc private func toggleDropdown(_ sender: Any?) {
        guard let dropdown else { return }
        if dropdown.isVisible {
            closeDropdown()
        } else {
            showDropdown()
            statusItem?.button?.highlight(true)
        }
    }

    private func showDropdown() {
        guard let button = statusItem?.button, let dropdown, !dropdown.isVisible else { return }
        // Show first, with whatever was last loaded; fresh data fills in when it arrives.
        // The app is not activated (see DropdownPanel), so the app in front keeps focus.
        dropdown.appearance = settings.menuAppearance.nsAppearance
        dropdown.show(below: button)
        startDismissMonitors()
        store.fast = true
        Task { await store.refresh() }
    }

    /// Close the dropdown from our side (a click elsewhere, Escape, opening a window).
    private func closeDropdown() {
        #if compiler(>=6.4)
        if #available(macOS 27.0, *), let session = statusItem?.expandedInterfaceSession {
            session.cancel()  // the menu bar then calls `hideDropdown`
            return
        }
        #endif
        hideDropdown()
    }

    private func hideDropdown() {
        guard let dropdown, dropdown.isVisible else { return }
        dropdown.orderOut(nil)
        store.fast = false
        stopDismissMonitors()
        statusItem?.button?.highlight(false)
    }

    /// Close the dropdown on a click anywhere outside it: in other apps or other menu
    /// bar items (a global monitor: those clicks never reach this app), or in our own
    /// windows. A click on our own status item is left to the menu bar / `toggleDropdown`.
    private func startDismissMonitors() {
        stopDismissMonitors()
        let clicks: NSEvent.EventTypeMask = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        globalClickMonitor = NSEvent.addGlobalMonitorForEvents(matching: clicks) { [weak self] _ in
            let location = NSEvent.mouseLocation
            Task { @MainActor [weak self] in
                guard let self else { return }
                // A click on our own icon: before macOS 27 the icon handles it too (see
                // `isOnStatusItem`). With the menu bar running the dropdown (macOS 27), the
                // icon gets no click while the dropdown is open and the menu bar doesn't
                // close it either, so close it here.
                if !self.usesExpandedInterface, self.isOnStatusItem(location) { return }
                self.closeDropdown()
            }
        }
        localClickMonitor = NSEvent.addLocalMonitorForEvents(matching: clicks.union(.keyDown)) { [weak self] event in
            guard let self, let dropdown = self.dropdown, dropdown.isVisible else { return event }
            if event.type == .keyDown {
                // Only seen while the app is active (e.g. the jobs window is open); the
                // dropdown never takes the keyboard from the app in front.
                if event.keyCode == 53 {  // Escape
                    self.closeDropdown()
                    return nil
                }
                return event
            }
            if let window = event.window, window === dropdown || window === self.statusItem?.button?.window {
                return event
            }
            // A menu the dropdown opened (the host switcher) is its own window.
            if let window = event.window, window.level.rawValue > dropdown.level.rawValue {
                return event
            }
            self.closeDropdown()
            return event
        }
    }

    /// Whether a screen point is on our icon's slot in the menu bar. The menu bar is drawn
    /// by another process, so a click on the icon reaches the global monitor too (as a click
    /// outside the app), just before the icon handles it. Closing there would make the
    /// icon's own handling reopen the dropdown, so those clicks are left to it. The slot is
    /// the status item's whole window, from the bottom of the menu bar to the screen's top
    /// edge, which is taller than the button inside it.
    private func isOnStatusItem(_ point: NSPoint) -> Bool {
        guard let slot = statusItem?.button?.window?.frame else { return false }
        return point.x >= slot.minX && point.x <= slot.maxX && point.y >= slot.minY
    }

    private func stopDismissMonitors() {
        if let m = globalClickMonitor { NSEvent.removeMonitor(m) }
        if let m = localClickMonitor { NSEvent.removeMonitor(m) }
        globalClickMonitor = nil
        localClickMonitor = nil
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
        closeDropdown()
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
        closeDropdown()
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

#if compiler(>=6.4)
@available(macOS 27.0, *)
extension AppDelegate: NSStatusItemExpandedInterfaceDelegate {
    func statusItem(_ statusItem: NSStatusItem, didBegin session: NSStatusItemExpandedInterfaceSession) {
        showDropdown()
    }

    func statusItemDidEndExpandedInterfaceSession(_ statusItem: NSStatusItem, animated: Bool) {
        hideDropdown()
    }
}
#endif
