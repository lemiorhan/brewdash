"""macos/launch.sh and install.sh, run with bash in temporary folders.

Nothing here touches /Applications, ~/.local/state or a running brewdash.app.
"""
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_brewdash import wait_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def servers_of(script):
    out = subprocess.run(["pgrep", "-f", str(script)], capture_output=True, text=True).stdout
    return {int(pid) for pid in out.split()}


def shell_script(path, body):
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path

# Reports the umask and PATH it was started with, prints a URL whose token names its --parent, then waits.
FAKE_SERVER = """import os, sys, time
mask = os.umask(0)
with open(os.environ["FAKE_REPORT"], "w") as f:
    f.write(f"{mask:o} {os.environ['PATH']}")
print("brewdash running at http://127.0.0.1:1/?t=p" + sys.argv[-1], flush=True)
time.sleep(60)
"""


@unittest.skipUnless(sys.platform == "darwin", "brewdash.app is macOS only")
class LaunchScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        app = self.root / "app"
        app.mkdir()
        shutil.copy(ROOT / "macos" / "launch.sh", app)
        self.fake = app / "brewdash.py"
        self.fake.write_text(FAKE_SERVER)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        login_shell = self.root / "login-shell"
        login_shell.write_text("#!/bin/sh\necho 'printed by .zprofile'\nprintf '\\n%s' \"$FAKE_LOGIN_PATH\"\n")
        login_shell.chmod(0o755)
        self.login_path = f"{bin_dir}:/usr/bin:/bin"
        self.state = self.root / "state" / "brewdash"
        self.report = self.root / "report"
        self.env = {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"), "SHELL": str(login_shell),
                    "PATH": "/usr/bin:/bin", "FAKE_LOGIN_PATH": self.login_path, "FAKE_REPORT": str(self.report)}
        self.addCleanup(lambda: [os.kill(pid, signal.SIGKILL) for pid in self.servers()])

    def run_script(self, *args):
        return subprocess.run(["/bin/bash", "-c", 'umask 022; exec /bin/bash "$0" "$@"', str(self.fake.parent / "launch.sh"),
                               *args], env=self.env, capture_output=True, text=True, timeout=30)

    def open(self, app_pid):
        r = self.run_script("open", str(app_pid))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def recorded(self):
        server, app, url = (self.state / "app-server").read_text().split()
        return int(server), app, url

    def servers(self):
        return servers_of(self.fake)

    def test_an_app_gets_one_server_and_keeps_getting_it(self):
        url = self.open(111)
        server, app, saved = self.recorded()
        self.assertEqual((url, app, saved, self.servers()), ("http://127.0.0.1:1/?t=p111", "111", url, {server}))
        self.assertEqual(self.open(111), url)
        self.assertEqual(self.servers(), {server})

    def test_a_relaunched_app_gets_a_new_server_and_the_old_one_stops(self):
        self.open(111)
        old = self.recorded()[0]
        self.assertEqual(self.open(222), "http://127.0.0.1:1/?t=p222")
        new = self.recorded()[0]
        self.assertNotEqual(new, old)
        wait_for(lambda: self.servers() == {new})

    def test_stop_ends_the_server_and_forgets_it(self):
        self.open(111)
        self.assertEqual(self.run_script("stop").returncode, 0)
        self.assertFalse((self.state / "app-server").exists())
        wait_for(lambda: not self.servers())

    def test_a_recorded_pid_that_is_not_this_apps_server_is_never_killed(self):
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.state.mkdir(parents=True)
        (self.state / "app-server").write_text(f"{other.pid} 111 http://127.0.0.1:1/?t=old\n")
        self.assertEqual(self.run_script("stop").returncode, 0)
        self.assertEqual((other.poll(), (self.state / "app-server").exists()), (None, False))
        (self.state / "app-server").write_text(f"{other.pid} 111 http://127.0.0.1:1/?t=old\n")
        self.assertEqual(self.open(111), "http://127.0.0.1:1/?t=p111")
        self.assertIsNone(other.poll())
        self.assertIn(self.recorded()[0], self.servers())

    def test_the_token_files_are_owner_only_and_the_server_keeps_the_users_umask_and_path(self):
        self.state.mkdir(parents=True, mode=0o755)
        (self.state / "server.log").write_text("old\n")
        (self.state / "server.log").chmod(0o644)
        self.open(111)
        mode = lambda p: p.stat().st_mode & 0o777  # noqa: E731
        self.assertEqual((mode(self.state), mode(self.state / "server.log"), mode(self.state / "app-server")),
                         (0o700, 0o600, 0o600))
        self.assertEqual(self.report.read_text(), f"22 {self.login_path}")


@unittest.skipUnless(sys.platform == "darwin", "brewdash.app is macOS only")
class LaunchScriptWithTheRealServerTest(unittest.TestCase):
    """launch.sh next to a symlink to the real brewdash.py. brew, the browser, the login shell and the app are
    stand-ins, so the server reads no Homebrew and a tab it opens lands in a file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        app = root / "app"
        app.mkdir()
        self.launch = Path(shutil.copy(ROOT / "macos" / "launch.sh", app))
        self.server = app / "brewdash.py"
        self.server.symlink_to(ROOT / "brewdash.py")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        shell_script(bin_dir / "brew", "exit 1")
        self.opened = root / "opened"
        self.env = {"HOME": str(root), "XDG_STATE_HOME": str(root / "state"), "PATH": "/usr/bin:/bin",
                    "SHELL": str(shell_script(root / "login-shell", f"printf '\\n%s' '{bin_dir}:/usr/bin:/bin'")),
                    "BROWSER": str(shell_script(root / "browser", f'echo "$1" > "{self.opened}"'))}
        self.addCleanup(lambda: [os.kill(pid, signal.SIGKILL) for pid in servers_of(self.server)])

    def test_the_app_gets_a_server_that_answers_opens_no_tab_and_stops_when_the_app_is_gone(self):
        app = subprocess.Popen(["sleep", "60"])
        self.addCleanup(app.wait)
        self.addCleanup(app.kill)
        r = subprocess.run(["/bin/bash", str(self.launch), "open", str(app.pid)], env=self.env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        url = r.stdout.strip()
        token = parse_qs(urlparse(url).query)["t"][0]
        with urlopen(Request(urljoin(url, "/api/jobs"), headers={"X-Brewdash-Token": token}), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(servers_of(self.server)), 1)
        app.kill()
        app.wait()
        wait_for(lambda: not servers_of(self.server))
        self.assertFalse(self.opened.exists())


def has_brew():
    return bool(shutil.which("brew")) or any(Path(p).exists() for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"))


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS only")
class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.apps = self.root / "Applications"
        self.plist = self.apps / "brewdash.app" / "Contents" / "Info.plist"
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))

    def assert_refused(self, *args):
        before = self.plist.read_bytes()
        env = dict(os.environ, BREWDASH_APP_DIR=str(self.apps), BREWDASH_NO_OPEN="1",
                   BREWDASH_TARBALL=f"file://{self.root}/missing.tar.gz")
        r = subprocess.run(["/bin/bash", str(ROOT / "install.sh"), *args], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("is not brewdash's app (its bundle id is 'com.example.other')", r.stderr)
        self.assertEqual(self.plist.read_bytes(), before)

    def test_uninstall_leaves_an_app_with_another_bundle_id_alone(self):
        self.assert_refused("--uninstall")

    @unittest.skipUnless(has_brew(), "install.sh checks for Homebrew before the app")
    def test_install_does_not_replace_an_app_with_another_bundle_id(self):
        self.assert_refused()


if __name__ == "__main__":
    unittest.main()
