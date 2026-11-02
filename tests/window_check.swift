// Compiled with macos/brewdash.swift and macos/policy.swift, with -D BREWDASH_CHECK, by tests/test_macos.py, and run
// once per -AppleActionOnDoubleClick value in `expected` below. Builds the app's window without showing it, prints
// each failed check and exits 1.
import Cocoa
import WebKit

// Records what the title bar band asks of the window instead of moving it.
final class TitleBarWindow: NSWindow {
    var asked: [String] = []
    var dragged: NSEvent?
    override func performDrag(with event: NSEvent) { asked.append("drag"); dragged = event }
    override func zoom(_ sender: Any?) { asked.append("zoom") }
    override func miniaturize(_ sender: Any?) { asked.append("minimize") }
}

@main
enum WindowCheck {
    // With --failed-page: a page that fails again after its one reconnect, on a window that has shown a dashboard,
    // so the alert is a sheet and does not block. Prints the alert's texts, one per line.
    static func failedPage() {
        _ = NSApplication.shared
        let app = AppDelegate()
        app.makeWindow()
        app.dashboard = URL(string: "http://127.0.0.1:1/")
        let refused = NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotConnectToHost,
                              userInfo: [NSLocalizedDescriptionKey: "Could not connect to the server."])
        _ = app.retry.failed(refused)
        app.webView(app.webView, didFailProvisionalNavigation: nil, withError: refused)
        func texts(_ view: NSView) -> [String] {
            ((view as? NSTextField).map { [$0.stringValue] } ?? []) + view.subviews.flatMap(texts)
        }
        guard let sheet = app.window.attachedSheet?.contentView else {
            print("FAIL: no alert on the window")
            exit(1)
        }
        print(texts(sheet).filter { !$0.isEmpty }.joined(separator: "\n"))
    }

    static func main() {
        if CommandLine.arguments.contains("--failed-page") { return failedPage() }
        var failures = 0
        func check(_ ok: Bool, _ what: String) {
            if !ok { failures += 1; print("FAIL: \(what)") }
        }
        func spin(until done: () -> Bool) {
            let deadline = Date().addingTimeInterval(10)
            while !done() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
        }
        _ = NSApplication.shared
        let app = AppDelegate()
        app.makeWindow()
        let window = app.window!
        check(window.frameAutosaveName == "brewdash", "the window keeps its place between launches")
        // Otherwise the frame changes below are saved in this check's own defaults domain.
        window.setFrameAutosaveName("")

        check(window.styleMask.contains(.fullSizeContentView), "the page lies under the title bar")
        check(window.titlebarAppearsTransparent && window.titleVisibility == .hidden, "the title bar draws nothing over the page")
        check(window.toolbar != nil && window.toolbarStyle == .unified, "an empty unified toolbar makes the title bar tall")
        check(window.contentView === app.webView, "the page fills the window")

        let scripts = { app.webView.configuration.userContentController.userScripts }
        let setupBar = { () -> Double? in
            guard let source = scripts().first?.source,
                  let match = source.firstMatch(of: #/'--bar', '([0-9.]+)px'/#) else { return nil }
            return Double(match.1)
        }
        let openPageBar = { () -> Double? in
            var value: Any?, done = false
            app.webView.evaluateJavaScript("getComputedStyle(document.documentElement).getPropertyValue('--bar')") { r, _ in
                value = r
                done = true
            }
            spin { done }
            return (value as? String)?.firstMatch(of: #/^([0-9.]+)px$/#).flatMap { Double($0.1) }
        }
        check(scripts().count == 1 && scripts()[0].source.contains(pageSetup), "one script, the page setup with --bar")
        check(scripts().first?.injectionTime == .atDocumentStart, "--bar is there before the first paint")
        app.webView.loadHTMLString("<html></html>", baseURL: nil)
        spin { !app.webView.isLoading }
        check(!app.webView.isLoading, "a page is open")

        let button = window.standardWindowButton(.closeButton)!
        let depth = Double(window.frame.height - button.convert(button.bounds, to: nil).midY)
        check((20...50).contains(depth), "the window buttons sit \(depth)pt down")
        func band(_ want: Double, _ what: String) {
            let (setup, open) = (setupBar(), openPageBar())
            check(setup == want && open == want, "\(what): want --bar \(want), the page setup has \(String(describing: setup)), the open page \(String(describing: open))")
        }
        func zoomed(_ want: Double) -> Bool { abs(Double(app.webView.pageZoom) - want) < 1e-9 }
        band(2 * depth, "the band has the window buttons in its middle")

        app.zoom(to: 0.5)
        check(zoomed(0.5), "zoomed out to half: \(app.webView.pageZoom)")
        band(4 * depth, "zoomed out to half, the band is twice as many CSS px")
        app.zoom(to: 0.1)
        check(zoomed(0.5), "the zoom stops at half: \(app.webView.pageZoom)")
        app.zoom(to: 9)
        check(zoomed(3), "the zoom stops at three times: \(app.webView.pageZoom)")
        band(2 * depth / 3, "zoomed in to three times, the band is a third as many CSS px")
        app.actualSize(nil)
        check(zoomed(1), "Actual Size: \(app.webView.pageZoom)")
        band(2 * depth, "Actual Size puts the band back")
        app.zoomIn(nil)
        check(zoomed(1.1), "Zoom In: \(app.webView.pageZoom)")
        band(2 * depth / Double(app.webView.pageZoom), "Zoom In refits the band")
        app.actualSize(nil)
        app.zoomOut(nil)
        check(zoomed(0.9), "Zoom Out: \(app.webView.pageZoom)")
        band(2 * depth / Double(app.webView.pageZoom), "Zoom Out refits the band")
        check(scripts().count == 1, "a zoom replaces the page setup, it does not add another: \(scripts().count)")

        window.toolbar = nil
        let moved = Double(window.frame.height - button.convert(button.bounds, to: nil).midY)
        check(moved != depth, "without the toolbar the window buttons move: \(moved)pt down")
        app.actualSize(nil)
        band(2 * depth, "the band keeps the depth the window was built with, whatever moves the buttons later")

        let bandWindow = TitleBarWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 300), styleMask: [.titled],
                                        backing: .buffered, defer: true)
        app.window = bandWindow
        func asked(_ body: [String: Any], buttons: Int = 1, doubleClick: String? = nil) -> [String] {
            bandWindow.asked = []
            app.titleBar(body, pressedButtons: buttons, doubleClick: doubleClick)
            return bandWindow.asked
        }
        app.titleBarPress = nil
        check(asked(["drag": true]) == [], "no drag before AppKit has handed over a mouseDown")
        let press = NSEvent.mouseEvent(with: .leftMouseDown, location: .zero, modifierFlags: [], timestamp: 0,
                                       windowNumber: bandWindow.windowNumber, context: nil, eventNumber: 0,
                                       clickCount: 1, pressure: 1)!
        check(app.recordPress(press) === press && app.titleBarPress === press,
              "a mouseDown goes on to the page, and is kept for the drag it may start")
        check(asked(["drag": true], buttons: 0) == [], "a click already over does not drag")
        check(asked(["drag": true]) == ["drag"] && bandWindow.dragged === press,
              "a held press drags the window with the mouseDown that started it")
        check(asked(["zoom": true]) == ["zoom"], "a double-click zooms by default")
        check(asked(["zoom": true], doubleClick: "Minimize") == ["minimize"], "a double-click minimizes when the system says so")
        check(asked(["zoom": true], doubleClick: "None") == [], "a double-click does nothing when the system says so")
        let setting = UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") ?? ""
        guard let expected = ["Minimize": ["minimize"], "None": [String]()][setting] else {
            print("FAIL: run with -AppleActionOnDoubleClick Minimize or None, not \(setting)")
            exit(1)
        }
        bandWindow.asked = []
        let before = bandWindow.backgroundColor
        app.webView.evaluateJavaScript("""
            webkit.messageHandlers.brewdash.postMessage({ drag: true });
            webkit.messageHandlers.brewdash.postMessage({ zoom: true });
            webkit.messageHandlers.brewdash.postMessage({ panel: "#123456" });
            """)
        spin { bandWindow.backgroundColor != before }
        check(bandWindow.backgroundColor != before, "the page's messages reach the window")
        check(bandWindow.asked == expected,
              "the page's drag waits for a held button, its double-click goes by AppleActionOnDoubleClick \(setting): \(bandWindow.asked)")

        if failures > 0 { exit(1) }
        print("ok")
    }
}
