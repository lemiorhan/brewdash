// Compiled with macos/brewdash.swift and macos/policy.swift, with -D BREWDASH_CHECK, by scripts/screenshots.py.
//     screenshots <dashboard url> <out folder>
// Builds the Mac app's own window, loads the demo into it instead of a server from launch.sh, and saves the window
// once per view and appearance with screencapture, which needs Screen Recording for the app that runs this.
import Cocoa
import WebKit

let shots: [(file: String, tab: String, detailRow: String?)] = [
    ("overview", "overview", nil),
    ("packages", "requested", "f:node"),
    ("security", "security", nil),
    ("services", "services", nil),
    ("apps", "apps", nil),
    ("shell", "shell", nil),
]
// The aspect the Pages tour lays the images out in (.shot img in docs/index.html).
let windowSize = NSSize(width: 1440, height: 900)

@main
enum Screenshots {
    static func main() {
        let args = CommandLine.arguments
        guard args.count == 3, let url = URL(string: args[1]) else {
            FileHandle.standardError.write("usage: screenshots <dashboard url> <out folder>\n".data(using: .utf8)!)
            exit(2)
        }
        let app = NSApplication.shared
        let shooter = Shooter(url: url, out: URL(fileURLWithPath: args[2]))
        app.delegate = shooter
        app.setActivationPolicy(.regular)
        app.run()
    }
}

enum ShotError: Error, CustomStringConvertible {
    case timedOut(String), screencapture(Int32)
    var description: String {
        switch self {
        case .timedOut(let what): return "timed out waiting for \(what)"
        case .screencapture(let status): return "screencapture exited with \(status)"
        }
    }
}

final class Shooter: NSObject, NSApplicationDelegate {
    let url: URL
    let out: URL
    let brewdash = AppDelegate()

    init(url: URL, out: URL) {
        self.url = url
        self.out = out
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        brewdash.makeWindow()
        let window = brewdash.window!
        // Otherwise the frame below is saved in this program's defaults domain.
        window.setFrameAutosaveName("")
        // screencapture saves the window at its screen's scale: the sharpest screen that holds it gives 2x images.
        guard let screen = NSScreen.screens
            .filter({ $0.visibleFrame.width >= windowSize.width && $0.visibleFrame.height >= windowSize.height })
            .max(by: { $0.backingScaleFactor < $1.backingScaleFactor }) else {
            FileHandle.standardError.write("screenshots: no screen holds a \(Int(windowSize.width))x\(Int(windowSize.height)) window\n".data(using: .utf8)!)
            exit(1)
        }
        let area = screen.visibleFrame
        window.setFrame(NSRect(x: area.midX - windowSize.width / 2, y: area.midY - windowSize.height / 2,
                               width: windowSize.width, height: windowSize.height), display: true)
        // The waits below look for English text, and the theme follows NSApp.appearance only while it is auto.
        brewdash.webView.configuration.userContentController.addUserScript(WKUserScript(
            source: "localStorage.setItem('brewdash-lang', 'en'); localStorage.setItem('brewdash-theme', 'auto');",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        for (name, action) in [(NSApplication.didBecomeActiveNotification, #selector(cameToFront)),
                               (NSWindow.didBecomeKeyNotification, #selector(cameToFront)),
                               (NSApplication.didResignActiveNotification, #selector(leftFront)),
                               (NSWindow.didResignKeyNotification, #selector(leftFront))] {
            NotificationCenter.default.addObserver(self, selector: action, name: name, object: nil)
        }
        brewdash.dashboard = url
        brewdash.webView.load(URLRequest(url: url))
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        Task { @MainActor in
            do {
                try await shoot()
                exit(0)
            } catch {
                FileHandle.standardError.write("screenshots: \(error)\n".data(using: .utf8)!)
                exit(1)
            }
        }
    }

    @MainActor func js(_ source: String) async throws -> Any? {
        try await brewdash.webView.evaluateJavaScript(source)
    }

    @MainActor func wait(for condition: String, _ what: String) async throws {
        let deadline = Date().addingTimeInterval(20)
        while Date() < deadline {
            if (try? await js("!!(\(condition))")) as? Bool == true { return }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        throw ShotError.timedOut(what)
    }

    @MainActor func shoot() async throws {
        try await wait(for: "document.readyState === 'complete' && document.documentElement.classList.contains('app')", "the page")
        try await wait(for: """
            [...document.querySelectorAll('.tile')].some(t => t.textContent.includes('on disk') && t.querySelector('.tile-value')?.textContent.trim() !== '…')
            """, "the disk use tile")
        try await wait(for: "[...document.querySelectorAll('.att')].some(a => a.textContent.includes('apps without a real signature'))",
                       "the app signatures in Needs attention")
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        for scheme in ["light", "dark"] {
            NSApp.appearance = NSAppearance(named: scheme == "dark" ? .darkAqua : .aqua)
            try await wait(for: "matchMedia('(prefers-color-scheme: dark)').matches === \(scheme == "dark")", "the \(scheme) appearance")
            for shot in shots {
                _ = try await js("document.querySelector('nav button[data-tab=\"\(shot.tab)\"]').click()")
                if let row = shot.detailRow {
                    _ = try await js("document.querySelector('tr.row[data-id=\"\(row)\"] td:nth-child(2)').click()")
                    try await wait(for: "document.querySelector('#graph canvas')", "the graph of \(row)")
                }
                try await Task.sleep(nanoseconds: 800_000_000)
                var since: Date?
                let deadline = Date().addingTimeInterval(120)
                repeat {
                    if Date() > deadline { throw ShotError.timedOut("the window to stay in front for \(shot.file)-\(scheme)") }
                    since = try await comeToFront()
                    try await capture(out.appendingPathComponent("\(shot.file)-\(scheme).png"))
                } while !inFront() || frontSince != since
                if shot.detailRow != nil { _ = try await js("document.querySelector('#detail [data-close]').click()") }
            }
        }
    }

    // A window of an app that is not in front draws its buttons grey and takes a moment to colour them again, and a
    // click anywhere else during the run takes the front away. frontSince is when the window last came to the front,
    // nil while it is not there; the notifications also catch a front lost and won back while nothing polls.
    var frontSince: Date?
    @objc func cameToFront() { frontSince = Date() }
    @objc func leftFront() { frontSince = nil }

    // NSApp.isActive and isKeyWindow have both said true over a grey capture, so the system's frontmost app is asked
    // as well.
    @MainActor func inFront() -> Bool {
        let front = NSWorkspace.shared.frontmostApplication?.processIdentifier == getpid() && brewdash.window.isKeyWindow
        if !front { frontSince = nil } else if frontSince == nil { frontSince = Date() }
        return front
    }

    // Returns frontSince once the window has been in front long enough to draw its buttons in colour.
    @MainActor func comeToFront() async throws -> Date? {
        if !inFront() {
            NSApp.activate(ignoringOtherApps: true)
            brewdash.window.makeKeyAndOrderFront(nil)
        }
        var asked = false
        let start = Date()
        while !inFront() || Date().timeIntervalSince(frontSince ?? Date()) < 0.5 {
            if !asked && !inFront() && Date().timeIntervalSince(start) > 2 {
                FileHandle.standardError.write("screenshots: the brewdash window is not in front; click it\n".data(using: .utf8)!)
                asked = true
            }
            if Date().timeIntervalSince(start) > 120 { throw ShotError.timedOut("the window to come to the front") }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        return frontSince
    }

    // -o leaves out the shadow: the Pages tour draws its own frame around the image (.shot in docs/index.html).
    @MainActor func capture(_ file: URL) async throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = ["-x", "-o", "-l", String(brewdash.window.windowNumber), file.path]
        let status: Int32 = try await withCheckedThrowingContinuation { done in
            p.terminationHandler = { done.resume(returning: $0.terminationStatus) }
            do { try p.run() } catch { done.resume(throwing: error) }
        }
        guard status == 0 else { throw ShotError.screencapture(status) }
    }
}
