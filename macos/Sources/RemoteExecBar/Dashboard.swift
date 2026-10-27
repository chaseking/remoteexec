import AppKit
import WebKit

/// The dashboard window: the `remoteexec gui` web UI (jobs, live logs, output
/// files, cluster/host settings) in a native window.
@MainActor
final class DashboardController: NSObject, NSWindowDelegate, WKUIDelegate, WKNavigationDelegate {
    let backend: Backend
    var onClose: (() -> Void)?
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

    private func build() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()  // keeps the web UI's theme choice
        let web = WKWebView(frame: .zero, configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "remoteexec"
        w.contentView = web
        w.minSize = NSSize(width: 720, height: 420)
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
        <style>body{font:13px -apple-system;padding:32px;color:#555}
        pre{white-space:pre-wrap;font-size:12px;background:#f3f1ee;padding:12px;border-radius:8px}
        @media (prefers-color-scheme: dark){body{background:#1f1e1d;color:#bbb}pre{background:#2a2928}}</style></head>
        <body><h3>Can't reach the remoteexec backend</h3><pre>\(escaped)</pre>
        <p>Retrying in a few seconds. Check the command in the app's Settings.</p></body></html>
        """
        webView?.loadHTMLString(html, baseURL: nil)
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
