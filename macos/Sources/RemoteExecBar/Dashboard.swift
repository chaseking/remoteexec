import AppKit
import WebKit

/// The dashboard window: the `remoteexec gui` web UI (jobs, live logs, output
/// files, cluster/host settings) in a native window.
@MainActor
final class DashboardController: NSObject, NSWindowDelegate, WKUIDelegate, WKNavigationDelegate,
                                 WKScriptMessageHandler {
    let backend: Backend
    var onClose: (() -> Void)?
    /// Opens the native backend settings (for when the page can't load).
    var onOpenBackendSettings: (() -> Void)?
    private(set) var window: NSWindow?
    private var webView: WKWebView?
    private var currentPath = "/"

    init(backend: Backend) {
        self.backend = backend
    }

    /// Show the window at `path`, e.g. "/", "/?job=123" or "/?view=settings".
    func show(path: String) {
        if window == nil { build() }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        currentPath = path
        Task {
            if await backend.ensureRunning() {
                webView?.load(URLRequest(url: backend.url(path)))
            } else {
                showError(backend.failureMessage ?? "The backend is not running.")
            }
        }
    }

    func reload() {
        guard window?.isVisible == true else { return }
        webView?.reload()
    }

    /// The page's own background (style.css --bg), shown while it loads.
    static let pageBackground = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(srgbRed: 0x26 / 255, green: 0x26 / 255, blue: 0x24 / 255, alpha: 1)
            : NSColor(srgbRed: 0xFA / 255, green: 0xF9 / 255, blue: 0xF5 / 255, alpha: 1)
    }

    private func build() {
        // Content runs edge to edge under a transparent titlebar, like the
        // Claude app: one surface, with the traffic lights over the sidebar and
        // the title right after them (a compact unified toolbar puts it there).
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = "remoteexec"
        w.toolbar = NSToolbar(identifier: "dashboard")
        w.toolbarStyle = .unifiedCompact
        w.titleVisibility = .visible
        w.titlebarAppearsTransparent = true
        w.titlebarSeparatorStyle = .none
        w.backgroundColor = Self.pageBackground
        let titlebar = max(28, w.frame.height - w.contentLayoutRect.height)

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()  // keeps the web UI's theme choice
        // Tells the page it runs under the titlebar and how tall that is, so it
        // keeps the strip free of controls.
        config.userContentController.addUserScript(WKUserScript(
            source: "window.remoteexecApp = {topInset: \(Int(titlebar))};",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        // The Settings page reads and changes the app's own settings through this.
        config.userContentController.add(self, name: "remoteexec")
        let web = WKWebView(frame: .zero, configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self
        if web.responds(to: NSSelectorFromString("_setDrawsBackground:")) {
            web.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints
        }

        // The web view would take every click under the transparent titlebar,
        // leaving nothing to drag the window by; a native strip sits on top.
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 1180, height: 760))
        web.frame = container.bounds
        web.autoresizingMask = [.width, .height]
        container.addSubview(web)
        let drag = TitlebarDragView(frame: NSRect(x: 0, y: container.bounds.height - titlebar,
                                                  width: container.bounds.width, height: titlebar))
        drag.autoresizingMask = [.width, .minYMargin]
        container.addSubview(drag)
        w.contentView = container
        w.minSize = NSSize(width: 760, height: 460)
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        _ = w.setFrameAutosaveName("RemoteExecDashboard")
        window = w
        webView = web
    }

    private func showError(_ message: String) {
        let escaped = message
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        let retry = backend.url(currentPath).absoluteString
        let html = """
        <html><head><meta http-equiv="refresh" content="5;url=\(retry)">
        <style>body{font:13px -apple-system;padding:56px 40px;color:#6b6963;background:#faf9f5}
        h3{color:#1f1e1d;font-weight:600}
        pre{white-space:pre-wrap;font-size:12px;background:#f3f1ea;padding:12px;border-radius:8px}
        @media (prefers-color-scheme: dark){body{background:#262624;color:#a8a59d}h3{color:#f0eee8}
        pre{background:#1f1e1d}}</style></head>
        <body><h3>Can't reach the remoteexec backend</h3><pre>\(escaped)</pre>
        <p>Retrying in a few seconds.
        <a href="#" onclick="webkit.messageHandlers.remoteexec.postMessage({type:'backendSettings'});return false">Backend settings…</a></p>
        </body></html>
        """
        webView?.loadHTMLString(html, baseURL: nil)
    }

    // MARK: messages from the page (Settings → Menu bar app)

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        let settings = backend.settings
        switch type {
        case "setAppSettings":
            if let notify = body["notify"] as? Bool {
                settings.notify = notify
                if notify { Notifier.shared.setup() }
            }
            if let command = body["backendCommand"] as? String { settings.backendCommand = command }
            if let port = body["port"] as? Int, port > 0 { settings.port = port }
        case "restartBackend":
            Task {
                await backend.restart()
                webView?.load(URLRequest(url: backend.url("/?view=settings")))  // the port may have changed
            }
            return
        case "backendSettings":
            onOpenBackendSettings?()
            return
        default:
            break
        }
        sendAppSettings()
    }

    private func sendAppSettings() {
        let s = backend.settings
        let payload: [String: Any] = [
            "notify": s.notify, "backendCommand": s.backendCommand, "port": s.port,
            "detected": Backend.detectCommand() ?? "", "log": backend.logURL.path,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else { return }
        webView?.evaluateJavaScript("window.remoteexecAppSettings && window.remoteexecAppSettings(\(json))",
                                    completionHandler: nil)
    }

    // MARK: NSWindowDelegate

    func windowWillClose(_ notification: Notification) {
        onClose?()
    }

    // MARK: WKUIDelegate (the web UI uses confirm() and target=_blank links)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        showError(error.localizedDescription)
    }
}

/// Drags the window (double-click: zoom/minimize per the system setting) for
/// the strip under the transparent titlebar, where the page has no controls.
final class TitlebarDragView: NSView {
    override func mouseDown(with event: NSEvent) {
        guard event.clickCount == 2 else {
            window?.performDrag(with: event)
            return
        }
        switch UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") {
        case "Minimize": window?.performMiniaturize(nil)
        case "None": break
        default: window?.performZoom(nil)
        }
    }
}
