// brewdash.app as a Mac app: the dashboard in its own window instead of a browser tab.
// Compiled with policy.swift by macos/build-app.sh when BREWDASH_MODE=app. It shares launch.sh with the browser applet, so the
// server, its token and its stop-with-the-app watch are the same in both.
import Cocoa
import WebKit

let resources = Bundle.main.resourceURL!.appendingPathComponent("brewdash")
let repoURL = URL(string: "https://github.com/lemiorhan/brewdash")!

// The page talks to the window through window.webkit.messageHandlers.brewdash; theme.js and app.js only call it
// when it is there, so the same web folder serves the browser too.
let pageSetup = """
document.documentElement.classList.add("app");
addEventListener("contextmenu", (e) => {
  if (!e.target.closest("input, textarea, select, pre, .mono") && !String(getSelection())) e.preventDefault();
});
"""

func launcher(_ args: [String]) throws -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = [resources.appendingPathComponent("launch.sh").path] + args
    let out = Pipe(), err = Pipe()
    p.standardOutput = out
    p.standardError = err
    try p.run()
    let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    let why = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    p.waitUntilExit()
    guard p.terminationStatus == 0 else {
        throw NSError(domain: "brewdash", code: Int(p.terminationStatus),
                      userInfo: [NSLocalizedDescriptionKey: why.isEmpty ? "launch.sh exited with \(p.terminationStatus)" : why])
    }
    return text.trimmingCharacters(in: .whitespacesAndNewlines)
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                         WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var dashboard: URL?
    // Measured once, on the window as built, so the band depends on the zoom alone (see fitTitleBar).
    var titleBarDepth: CGFloat?
    // The mouseDown that started a drag, which performDrag(with:) wants (NSWindow.h): the page's message asking for the
    // drag comes later, when NSApp.currentEvent may already be another event.
    var titleBarPress: NSEvent?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.mainMenu = makeMenu()
        makeWindow()
        connect(show: true)
        NSApp.activate(ignoringOtherApps: true)
    }

    // The server stops itself when this process is gone; a quit stops it now. A brew job it started runs in its own
    // session (Brew.popen) and is not stopped.
    func applicationWillTerminate(_ note: Notification) {
        _ = try? launcher(["stop"])
    }

    // A Dock click on an app whose window is closed shows it again, with a new server if the old one died.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { connect(show: true) }
        return true
    }

    // Main thread only, as are the retry and the dashboard.
    var gate = ConnectGate()
    var retry = RetryLimit()

    func connect(show: Bool, reload: Bool = false) {
        guard gate.request(show: show, reload: reload) else { return }
        let pid = String(ProcessInfo.processInfo.processIdentifier)
        DispatchQueue.global().async {
            let result = Result { try launcher(["open", pid]) }
            DispatchQueue.main.async { self.connected(result) }
        }
    }

    func connected(_ result: Result<String, Error>) {
        let (show, reload) = gate.finish()
        switch result {
        case .success(let text):
            guard let url = URL(string: text), url.host == "127.0.0.1" else {
                fail("launch.sh did not answer with the dashboard's address: \(text)")
                return
            }
            if url != dashboard || webView.url == nil {
                dashboard = url
                webView.load(URLRequest(url: url))
            } else if reload {
                webView.reload()
            }
            if show { window.makeKeyAndOrderFront(nil) }
        case .failure(let error):
            fail(error.localizedDescription)
        }
    }

    // Without a dashboard yet there is nothing to show, so the app quits; once one has loaded, the window stays and
    // the next reload or Dock click tries again.
    func fail(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = dashboard == nil ? "brewdash could not start" : "brewdash is not responding"
        alert.informativeText = message
        if dashboard == nil {
            alert.runModal()
            NSApp.terminate(nil)
        } else {
            alert.beginSheetModal(for: window)
        }
    }

    func makeWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "brewdash")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "brewdash"
        // The page lies under the title bar and draws its sidebar and toolbar there (html.app in web/style.css). An
        // empty toolbar makes the bar tall enough for the page's toolbar line, centred on the window buttons (see
        // fitTitleBar); clicks in the bar still reach the page, so the page asks for the window drag (see "drag").
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.toolbar = NSToolbar(identifier: "brewdash")
        window.toolbarStyle = .unified
        window.tabbingMode = .disallowed
        window.minSize = NSSize(width: 900, height: 600)
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("brewdash")
        titleBarDepth = window.standardWindowButton(.closeButton).map { window.frame.height - $0.convert($0.bounds, to: nil).midY }
        NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown, handler: recordPress)
        fitTitleBar()
    }

    func recordPress(_ event: NSEvent) -> NSEvent? {
        titleBarPress = event
        return event
    }

    // --bar in web/style.css: twice the window buttons' depth, so the page's toolbar line has them in its middle. It
    // is in CSS px, which the page zoom scales.
    func fitTitleBar() {
        guard let depth = titleBarDepth else { return }
        let bar = "document.documentElement.style.setProperty('--bar', '\(2 * depth / webView.pageZoom)px');"
        let content = webView.configuration.userContentController
        content.removeAllUserScripts()
        content.addUserScript(WKUserScript(source: pageSetup + bar, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView.evaluateJavaScript(bar)
    }

    // MARK: messages from the page

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any] else { return }
        if let theme = body["theme"] as? String {
            window.appearance = theme == "dark" ? NSAppearance(named: .darkAqua)
                : theme == "light" ? NSAppearance(named: .aqua) : nil
        }
        if let panel = body["panel"] as? String, let color = cssColor(panel) {
            window.backgroundColor = color
        }
        if let name = body["save"] as? String, let text = body["text"] as? String {
            save(text, as: name)
        }
        titleBar(body, pressedButtons: NSEvent.pressedMouseButtons,
                 doubleClick: UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick"))
    }

    func titleBar(_ body: [String: Any], pressedButtons: Int, doubleClick: String?) {
        if body["drag"] != nil, dragsWindow(pressedButtons: pressedButtons), let press = titleBarPress {
            window.performDrag(with: press)
        }
        if body["zoom"] != nil {
            switch titleBarAction(doubleClick) {
            case .zoom: window.zoom(nil)
            case .minimize: window.miniaturize(nil)
            case .ignore: break
            }
        }
    }

    func save(_ text: String, as name: String) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = name
        panel.beginSheetModal(for: window) { response in
            guard response == .OK, let url = panel.url else { return }
            do {
                try text.write(to: url, atomically: true, encoding: .utf8)
            } catch {
                NSAlert(error: error).beginSheetModal(for: self.window)
            }
        }
    }

    // MARK: navigation

    // Only the dashboard opens in the window; every other link goes to the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        switch navigation(to: action.request.url, dashboard: dashboard) {
        case .allow: decisionHandler(.allow)
        case .openOutside: NSWorkspace.shared.open(action.request.url!); decisionHandler(.cancel)
        case .cancel: decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if navigation(to: action.request.url, dashboard: nil) == .openOutside { NSWorkspace.shared.open(action.request.url!) }
        return nil
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        switch retry.failed(error as NSError) {
        case .ignore: break
        case .reconnect: connect(show: false, reload: true)
        case .report: fail("Press ⌘R to try again.")
        }
    }

    func webView(_ webView: WKWebView, didFinish nav: WKNavigation!) {
        retry.loaded()
    }

    // MARK: menu actions

    @objc func reload(_ sender: Any?) {
        connect(show: false, reload: true)
    }

    @objc func openSettings(_ sender: Any?) {
        window.makeKeyAndOrderFront(nil)
        webView.evaluateJavaScript("document.querySelector('#tabs [data-tab=\"settings\"]')?.click()")
    }

    @objc func zoomIn(_ sender: Any?) { zoom(to: webView.pageZoom + 0.1) }
    @objc func zoomOut(_ sender: Any?) { zoom(to: webView.pageZoom - 0.1) }
    @objc func actualSize(_ sender: Any?) { zoom(to: 1) }

    func zoom(to scale: CGFloat) {
        webView.pageZoom = min(max(scale, 0.5), 3)
        fitTitleBar()
    }
    @objc func back(_ sender: Any?) { webView.goBack() }
    @objc func forward(_ sender: Any?) { webView.goForward() }
    @objc func openRepo(_ sender: Any?) { NSWorkspace.shared.open(repoURL) }

    func makeMenu() -> NSMenu {
        let main = NSMenu()
        @discardableResult func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            let holder = NSMenuItem()
            holder.submenu = m
            main.addItem(holder)
            return m
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            if let action, responds(to: action) { i.target = self }
            return i
        }
        submenu("brewdash", [
            item("About brewdash", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Settings…", #selector(openSettings(_:)), ","),
            .separator(),
            item("Hide brewdash", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("Quit brewdash", #selector(NSApplication.terminate(_:)), "q"),
        ])
        submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu("View", [
            item("Reload", #selector(reload(_:)), "r"),
            .separator(),
            item("Back", #selector(back(_:)), "["),
            item("Forward", #selector(forward(_:)), "]"),
            .separator(),
            item("Actual Size", #selector(actualSize(_:)), "0"),
            item("Zoom In", #selector(zoomIn(_:)), "+"),
            item("Zoom Out", #selector(zoomOut(_:)), "-"),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        let windowMenu = submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
            .separator(),
            item("Bring All to Front", #selector(NSApplication.arrangeInFront(_:))),
        ])
        NSApp.windowsMenu = windowMenu
        let help = submenu("Help", [item("brewdash on GitHub", #selector(openRepo(_:)))])
        NSApp.helpMenu = help
        return main
    }
}

// tests/window_check.swift brings its own main.
#if !BREWDASH_CHECK
@main
enum Main {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
#endif
