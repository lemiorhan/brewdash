// Compiled as main.swift with macos/policy.swift by tests/test_macos.py; prints each failed check and exits 1.
import AppKit

var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1; print("FAIL: \(what)") }
}

let dash = URL(string: "http://127.0.0.1:5000/?t=abc")!
let cases: [(String?, URL?, Navigation)] = [
    ("http://127.0.0.1:5000/api/jobs", dash, .allow),
    ("http://127.0.0.1:5000/#tab=settings", dash, .allow),
    ("http://127.0.0.1:5001/", dash, .openOutside),
    ("http://localhost:5000/", dash, .openOutside),
    ("https://127.0.0.1:5000/", dash, .openOutside),
    ("https://github.com/lemiorhan/brewdash", dash, .openOutside),
    ("mailto:someone@example.com", dash, .openOutside),
    ("http://127.0.0.1:5000/", nil, .openOutside),
    ("about:blank", dash, .allow),
    ("file:///etc/passwd", dash, .cancel),
    ("javascript:alert(1)", dash, .cancel),
    (nil, dash, .cancel),
]
for (url, d, want) in cases {
    let got = navigation(to: url.flatMap(URL.init(string:)), dashboard: d)
    check(got == want, "navigation(\(url ?? "nil"), dashboard: \(d?.absoluteString ?? "nil")) = \(got), want \(want)")
}

var gate = ConnectGate()
check(gate.request(show: true, reload: false), "the first connect starts launch.sh")
check(!gate.request(show: false, reload: true), "a connect while one runs starts nothing")
check(!gate.request(show: false, reload: false), "a third connect while one runs starts nothing")
let joined = gate.finish()
check(joined.show && joined.reload, "the running connect answers for every request that joined it: \(joined)")
check(!gate.running, "a finished connect frees the gate")
check(gate.request(show: false, reload: false), "after it finishes, a connect starts launch.sh again")
let alone = gate.finish()
check(!alone.show && !alone.reload, "a finished connect forgets what earlier ones asked for: \(alone)")

var retry = RetryLimit()
let refused = NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotConnectToHost)
let cancelled = NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled)
let otherCancelled = NSError(domain: "elsewhere", code: NSURLErrorCancelled)
check(retry.failed(cancelled) == .ignore, "a cancelled load is ignored")
check(retry.failed(refused) == .reconnect, "the first failed load reconnects")
check(retry.failed(cancelled) == .ignore, "a cancel after a retry is still ignored")
check(retry.failed(refused) == .report, "a second failed load is reported, not retried")
check(retry.failed(refused) == .report, "and so is a third")
retry.loaded()
check(retry.failed(refused) == .reconnect, "a page that loaded earns a new retry")
var fresh = RetryLimit()
check(fresh.failed(otherCancelled) == .reconnect, "only NSURLErrorDomain's cancel is a cancel")

check(dragsWindow(pressedButtons: 1), "the left button held drags")
check(dragsWindow(pressedButtons: 3), "the left button held with the right one drags")
check(!dragsWindow(pressedButtons: 0), "a click already let go of does not drag")
check(!dragsWindow(pressedButtons: 2), "the right button alone does not drag")

check(titleBarAction(nil) == .zoom, "with the setting never changed, a double-click zooms")
check(titleBarAction("Maximize") == .zoom, "Maximize zooms")
check(titleBarAction("Minimize") == .minimize, "Minimize minimizes")
check(titleBarAction("None") == .ignore, "None does nothing")

func rgb(_ css: String) -> [Int]? {
    guard let c = cssColor(css)?.usingColorSpace(.sRGB) else { return nil }
    return [c.redComponent, c.greenComponent, c.blueComponent].map { Int(($0 * 255).rounded()) }
}
check(rgb("#161a21") == [22, 26, 33], "hex: \(String(describing: rgb("#161a21")))")
check(rgb(" #ffffff") == [255, 255, 255], "hex with the space getPropertyValue keeps")
check(rgb("rgb(22, 26, 33)") == [22, 26, 33], "rgb()")
check(rgb("rgba(1, 2, 3, 0.5)") == [1, 2, 3], "rgba()")
check(rgb("rgb(1, 2)") == nil, "rgb() with two parts is not read")
check(rgb("rgb(1)") == nil, "rgb() with one part is not read")
check(rgb("#fff") == nil, "three-digit hex is not read")
check(rgb("tomato") == nil, "a colour name is not read")
check(rgb("") == nil, "empty")

if failures > 0 { exit(1) }
print("ok")
