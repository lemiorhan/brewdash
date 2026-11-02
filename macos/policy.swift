// The Mac app's decisions, apart from the window so tests/test_macos.py can compile and check them alone.
import AppKit

enum Navigation: Equatable {
    case allow, openOutside, cancel
}

// Only the dashboard loads in the window, which carries the page's message handler; anything else a link asks for
// goes to the default browser, or nowhere.
func navigation(to url: URL?, dashboard: URL?) -> Navigation {
    guard let url else { return .cancel }
    if let d = dashboard, url.scheme == d.scheme, url.host == d.host, url.port == d.port { return .allow }
    if url.scheme == "about" { return .allow }
    if ["http", "https", "mailto"].contains(url.scheme ?? "") { return .openOutside }
    return .cancel
}

// Two launch.sh opens at once would each start a server, and each server's updater could swap the app at the same
// moment; so a connect asked for while one runs only joins it.
struct ConnectGate {
    private(set) var running = false
    private var show = false, reload = false

    // true: start launch.sh now. false: one is running and will answer for this request too.
    mutating func request(show: Bool, reload: Bool) -> Bool {
        self.show = self.show || show
        self.reload = self.reload || reload
        if running { return false }
        running = true
        return true
    }

    mutating func finish() -> (show: Bool, reload: Bool) {
        defer { running = false; show = false; reload = false }
        return (show, reload)
    }
}

// A dead server is restarted once; a page that still does not load after that is reported, so a server that starts
// but does not answer cannot send the app round in a loop.
enum LoadFailure: Equatable {
    case ignore, reconnect, report
}

struct RetryLimit {
    private var retried = false

    mutating func failed(_ error: NSError) -> LoadFailure {
        if error.domain == NSURLErrorDomain && error.code == NSURLErrorCancelled { return .ignore }
        if retried { return .report }
        retried = true
        return .reconnect
    }

    mutating func loaded() {
        retried = false
    }
}

// The page asks for a window drag on mousedown, and the message can arrive after a quick click has already ended:
// only a left button still held drags.
func dragsWindow(pressedButtons: Int) -> Bool {
    pressedButtons & 1 == 1
}

enum TitleBarAction: Equatable {
    case zoom, minimize, ignore
}

// A double-click on the title bar, as the AppleActionOnDoubleClick default asks for it.
func titleBarAction(_ setting: String?) -> TitleBarAction {
    switch setting {
    case "Minimize": return .minimize
    case "None": return .ignore
    default: return .zoom
    }
}

// "#161a21" or "rgb(22, 26, 33)", as getComputedStyle may give either.
func cssColor(_ css: String) -> NSColor? {
    let s = css.trimmingCharacters(in: .whitespaces)
    var parts: [Double] = []
    if s.hasPrefix("#"), s.count == 7, let v = Int(s.dropFirst(), radix: 16) {
        parts = [Double(v >> 16 & 255), Double(v >> 8 & 255), Double(v & 255)]
    } else if s.hasPrefix("rgb") {
        parts = s.drop(while: { $0 != "(" }).dropFirst().prefix(while: { $0 != ")" })
            .split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
    }
    guard parts.count >= 3 else { return nil }
    return NSColor(srgbRed: parts[0] / 255, green: parts[1] / 255, blue: parts[2] / 255, alpha: 1)
}
