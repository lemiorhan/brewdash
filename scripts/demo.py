"""brewdash on made-up data: a fake Homebrew, a fake home folder and fake apps.

    python3 scripts/demo.py [--port 8765] [--no-browser]

Nothing on this Mac is read or changed: brew, lsof and codesign are stand-ins,
and every action only prints what it would have run. The README screenshots
are taken from this (scripts/screenshots.py).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import brewdash  # noqa: E402
from test_brewdash import cask, formula, make_app  # noqa: E402

DAY = 86400
NOW = time.time()


def f(name, desc, version, days_ago, **kw):
    rec = formula(name, desc=desc, homepage=f"https://formulae.brew.sh/formula/{name}", **kw)
    rec["versions"] = {"stable": version}
    rec["installed"][0].update(version=version, time=int(NOW - days_ago * DAY))
    return rec


def c(token, desc, version, days_ago, app, apps_dir, **kw):
    return cask(token, desc=desc, version=version, installed=version, installed_time=int(NOW - days_ago * DAY),
                name=[app], artifacts=[{"app": [app + ".app"], "target": str(apps_dir / (app + ".app"))}], **kw)


def build(root):
    apps_dir = root / "Applications"
    formulae = [
        f("git", "Distributed revision control system", "2.47.0", 400, requested=True, deps=["pcre2", "gettext"]),
        f("node", "Platform built on V8 to build network applications", "22.9.0", 210, requested=True,
          deps=["icu4c@76", "openssl@3", "brotli"]),
        f("postgresql@16", "Object-relational database system", "16.4", 300, requested=True,
          deps=["icu4c@76", "openssl@3", "readline"]),
        f("redis", "Persistent key-value database, with built-in net interface", "7.4.1", 120, requested=True,
          deps=["openssl@3"]),
        f("ffmpeg", "Play, record, convert, and stream audio and video", "7.1", 60, requested=True,
          deps=["x264", "lame", "libvpx"]),
        f("jq", "Lightweight and flexible command-line JSON processor", "1.7.1", 500, requested=True),
        f("wget", "Internet file retriever", "1.24.5", 500, requested=True, deps=["openssl@3"], pinned=True),
        f("ripgrep", "Search tool like grep and The Silver Searcher", "14.1.1", 90, requested=True, deps=["pcre2"]),
        f("gh", "GitHub command-line tool", "2.60.1", 30, requested=True),
        f("awscli", "Official Amazon AWS command-line interface", "2.18.0", 45, requested=True,
          deps=["python@3.12", "openssl@3"]),
        f("php@8.1", "General-purpose scripting language", "8.1.30", 700, requested=True,
          deps=["icu4c@76", "openssl@3"], deprecated=True, deprecation_reason="unsupported"),
        f("python@3.12", "Interpreted, interactive, object-oriented programming language", "3.12.7", 45,
          deps=["openssl@3", "readline", "sqlite"]),
        f("openssl@3", "Cryptography and SSL/TLS Toolkit", "3.4.0", 210),
        f("icu4c@76", "C/C++ and Java libraries for Unicode and globalization", "76.1", 210),
        f("readline", "Library for command-line editing", "8.2.13", 300),
        f("sqlite", "Command-line interface for SQLite", "3.47.0", 45),
        f("pcre2", "Perl compatible regular expressions library", "10.44", 400),
        f("gettext", "GNU internationalization (i18n) and localization (l10n) library", "0.22.5", 400),
        f("brotli", "Generic-purpose lossless compression algorithm", "1.1.0", 210),
        f("x264", "H.264/AVC encoder", "r3108", 60), f("lame", "High quality MPEG Audio Layer III (MP3) encoder", "3.100", 60),
        f("libvpx", "VP8/VP9 video codec", "1.14.1", 60),
        f("icu4c@74", "C/C++ and Java libraries for Unicode and globalization", "74.2", 640),
    ]
    casks = [
        c("visual-studio-code", "Open-source code editor", "1.95.1", 100, "Visual Studio Code", apps_dir, auto_updates=True),
        c("firefox", "Web browser", "132.0", 20, "Firefox", apps_dir, auto_updates=True),
        c("docker-desktop", "App to build and share containerised applications", "4.34.0", 150, "Docker", apps_dir),
        c("iterm2", "Terminal emulator as alternative to Apple's Terminal app", "3.5.10", 330, "iTerm", apps_dir),
        c("ngrok", "Reverse proxy, secure introspectable tunnels to localhost", "3.18.0", 80, "ngrok", apps_dir,
          tap="ngrok/ngrok"),
    ]
    casks[-1]["artifacts"] = [{"binary": ["ngrok"]}]
    info = {"formulae": formulae, "casks": casks}
    outdated = {"formulae": [{"name": "node", "current_version": "23.1.0"}, {"name": "wget", "current_version": "1.25.0"},
                             {"name": "awscli", "current_version": "2.19.1"}],
                "casks": [{"name": "docker-desktop", "current_version": "4.35.1"}]}
    services = [
        {"name": "postgresql@16", "status": "started", "running": True, "loaded": True, "pid": 611, "user": "demo",
         "file": "~/Library/LaunchAgents/homebrew.mxcl.postgresql@16.plist", "log_path": None},
        {"name": "redis", "status": "started", "running": True, "loaded": True, "pid": 612, "user": "demo",
         "file": "~/Library/LaunchAgents/homebrew.mxcl.redis.plist", "log_path": None},
    ]
    taps = [
        {"name": "homebrew/core", "remote": "https://github.com/Homebrew/homebrew-core", "official": True, "private": False,
         "last_commit": "2 hours ago", "formula_names": ["x"] * 7200, "cask_tokens": []},
        {"name": "homebrew/cask", "remote": "https://github.com/Homebrew/homebrew-cask", "official": True, "private": False,
         "last_commit": "3 hours ago", "formula_names": [], "cask_tokens": ["x"] * 7400},
        {"name": "ngrok/ngrok", "remote": "https://github.com/ngrok/homebrew-ngrok", "official": False, "private": False,
         "last_commit": "3 weeks ago", "formula_names": [], "cask_tokens": ["ngrok"]},
        {"name": "someone/tools", "remote": "https://github.com/someone/homebrew-tools", "official": False,
         "private": False, "last_commit": "14 months ago", "formula_names": ["oldtool"], "cask_tokens": []},
    ]
    return info, outdated, services, taps


class DemoBrew:
    """Answers the brew calls brewdash makes from the made-up data; actions only print."""

    def __init__(self, root):
        self.root = root
        self.prefix = root / "prefix"
        self.info, self.outdated, self.services, self.taps = build(root)

    def run(self, *args, timeout=300):
        out = {("--prefix",): str(self.prefix), ("--repository",): str(self.root / "repo"),
               ("--cache",): str(self.root / "cache"),
               ("autoremove", "--dry-run"): "==> Would autoremove 1 unneeded formula:\nicu4c@74\n",
               ("cleanup", "-n"): "Would remove: ~/Library/Caches/Homebrew/node--22.8.0.tar.gz (31.2MB)\n"
                                  "Would remove: ~/Library/Caches/Homebrew/ffmpeg--7.0.2.tar.gz (24.9MB)\n"
                                  "This operation would free approximately 412.6MB of disk space.",
               ("doctor",): "Warning: Some installed formulae are deprecated or disabled.\n"
                            "You should find replacements for the following formulae:\n  php@8.1"}
        if args and args[0] == "search":
            q = args[-1]
            names = [p["name"] for p in self.info["formulae"]] if args[1] == "--formula" else [c["token"] for c in self.info["casks"]]
            return 0, "\n".join(n for n in names if q in n) + "\n", ""
        return (1 if args == ("doctor",) else 0), out.get(args, ""), ""

    def json(self, *args):
        if args[0] == "info" and "--installed" in args:
            return self.info
        if args[0] == "info" and "--cask" in args:
            tokens = args[args.index("--cask") + 1:]
            return {"formulae": [], "casks": [cask(t, version="4.41.105", auto_updates=True,
                                                   artifacts=[{"app": [t.title() + ".app"]}]) for t in tokens]}
        if args[0] == "info":
            return {"formulae": [formula(args[-1], desc="Demo formula", versions={"stable": "1.0"})], "casks": []}
        return {"outdated": self.outdated, "services": self.services, "tap-info": self.taps}[args[0]]

    def popen(self, cmd):
        text = "demo mode: brewdash would run: " + " ".join(cmd)
        return subprocess.Popen(["/bin/echo", text], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True)


SIGNERS = {
    "Visual Studio Code": "Developer ID Application: Microsoft Corporation (UBF8T346G9)",
    "Firefox": "Developer ID Application: Mozilla Corporation (43AQ936H96)",
    "Docker": "Developer ID Application: Docker Inc (9BNSXJN65R)",
    "iTerm": "Developer ID Application: GEORGE NACHMAN (H7V7XYVQ7D)",
    "Slack": "Developer ID Application: Slack Technologies, Inc. (BQR82RBBHL)",
    "Xcode": "Software Signing", "Keynote": "Apple Mac OS Application Signing",
}
LISTENERS = {611: "p611\nf7\nn127.0.0.1:5432\nf8\nn[::1]:5432\n", 612: "p612\nf6\nn*:6379\n"}


def demo_sysrun(argv, timeout=60):
    if argv[0] == brewdash.CODESIGN:
        name = Path(argv[-1]).stem
        if name == "OldTool":
            return 1, "", f"{argv[-1]}: code object is not signed at all\n"
        if name == "Sketchy Helper":
            return 0, "", "Executable=x\nSignature=adhoc\n"
        return 0, "", f"Authority={SIGNERS[name]}\n"
    if "-iTCP" in argv:
        return 1, "".join(LISTENERS.values()), ""
    return 1, "", ""


def setup(root):
    """The files brewdash reads besides brew: sizes on disk, apps, shell files, secure-it's report."""
    brew = DemoBrew(root)
    sizes = {"node": 190, "postgresql@16": 88, "ffmpeg": 61, "python@3.12": 74, "awscli": 120, "php@8.1": 96,
             "openssl@3": 34, "icu4c@76": 76, "gh": 52, "git": 58, "redis": 9, "icu4c@74": 72}
    for p in brew.info["formulae"]:
        d = brew.prefix / "Cellar" / p["name"]
        d.mkdir(parents=True)
        (d / "data").write_bytes(b"\0" * 1024 * sizes.get(p["name"], 3) * 64)
    for k in brew.info["casks"]:
        (brew.prefix / "Caskroom" / k["token"]).mkdir(parents=True)
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / ".git" / "FETCH_HEAD").write_text("")
    os.utime(root / "repo" / ".git" / "FETCH_HEAD", (NOW - 2 * DAY, NOW - 2 * DAY))
    (root / "cache" / "api").mkdir(parents=True)
    (root / "cache" / "api" / "cask_names.txt").write_text("slack\nvisual-studio-code\nfirefox\ndocker-desktop\niterm2\n")
    (root / "cache" / "api" / "formula_names.txt").write_text("nvm\nfzf\njbang\n")
    apps = root / "Applications"
    for name, version in (("Visual Studio Code", "1.95.1"), ("Firefox", "132.0"), ("Docker", "4.34.0"), ("iTerm", "3.5.10"),
                          ("Slack", "4.41.105"), ("OldTool", "0.9"), ("Sketchy Helper", "1.2")):
        make_app(apps, name, version)
    for name in ("Xcode", "Keynote"):
        make_app(apps, name, "16.1" if name == "Xcode" else "14.2", app_store=True)
    home = root / "home"
    (home / "Library" / "CloudStorage" / "Dropbox" / "tools" / "bin").mkdir(parents=True)
    (home / "Dropbox").symlink_to(home / "Library" / "CloudStorage" / "Dropbox")
    (home / ".nvm").mkdir()
    (home / ".zprofile").write_text(f'eval "$({brew.prefix}/bin/brew shellenv)"\n')
    (home / ".zshrc").write_text("\n".join([
        'export NVM_DIR="$HOME/.nvm"',
        '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"',
        "source <(fzf --zsh)",
        f'export PATH="{brew.prefix}/Cellar/php@8.1/8.1.29/bin:$PATH"',
        'export PATH="$HOME/Dropbox/tools/bin:$PATH"',
        'export PATH="/usr/local/opt/mysql-client/bin:$PATH"',
        'export PATH="$HOME/.cargo/bin:$PATH"',
    ]) + "\n")
    (home / "secure-it").mkdir()
    (home / "secure-it" / "report.json").write_text(json.dumps(secure_it_report(brew.prefix)))
    return brew, home, apps


def alert(n, check_id, severity, title, detail, remediation, fix, subject):
    return {"check_id": check_id, "severity": severity, "title": title, "detail": detail, "remediation": remediation,
            "fix_command": fix, "accept_command": f'sudo secure-it accept {f"{n:02x}" * 5} --note "neden"', "subject": subject}


def secure_it_report(prefix):
    return {"schema": 2, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 3600)),
            "scan": {}, "alerts": [
        alert(1, "exposure.listening", "high", "redis listens on every interface (*:6379)",
              "pid 612; Homebrew package: redis", "Bind it to 127.0.0.1 in redis.conf, or stop the service when you do not need it.",
              "brew services stop redis", {"kind": "path", "path": "/opt/homebrew/Cellar/redis/7.4.1/bin/redis-server"}),
        alert(2, "system.firewall", "high", "The application firewall is off",
              "socketfilterfw reports state 0", "Turn it on in System Settings > Network > Firewall.",
              "sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate on", None),
        alert(3, "persistence.path_shadow", "medium", "Synced folder ahead of the system in PATH: ~/Dropbox/tools/bin",
              ".zshrc:5", "Move it after $PATH, or copy the tools to a local folder.", None, {"kind": "shell"}),
        alert(4, "packages.brew_taps", "low", "Third-party tap: ngrok/ngrok",
              "Remote: https://github.com/ngrok/homebrew-ngrok", "Keep it only if you trust its owner.",
              "brew untap ngrok/ngrok", {"kind": "tap", "name": "ngrok/ngrok"}),
    ]}


def start(port=0):
    root = Path(tempfile.mkdtemp(prefix="brewdash-demo-", dir="/tmp"))  # short paths read better in screenshots
    brew, home, apps = setup(root)
    (root / "secure-it").write_text("")
    catalog = ({**brewdash.PLUGIN_CATALOG[0], "exe": str(root / "secure-it")},)
    server, url, _ = brewdash.serve(port=port, open_browser=False, brew=brew, state_dir=str(root / "state"),
                                    home=str(home), app_dirs=[str(apps)], sysrun=demo_sysrun, catalog=catalog,
                                    unprotected=lambda path: None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, url, root


def main():
    ap = argparse.ArgumentParser(description="brewdash on made-up data")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    server, url, root = start(args.port)
    print(f"brewdash demo at {url}\ndata in {root}\nCtrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
