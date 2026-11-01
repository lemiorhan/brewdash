"""macos/launch.sh and install.sh, run with bash in temporary folders.

Nothing here touches /Applications, ~/.local/state or a running brewdash.app.
"""
import json
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


@unittest.skipUnless(sys.platform == "darwin", "build-app.sh is macOS only")
class BuildAppTest(unittest.TestCase):
    def build(self, **env):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = Path(tmp.name) / "brewdash.app"
        r = subprocess.run(["/bin/bash", str(ROOT / "macos" / "build-app.sh"), str(ROOT), str(app)],
                           env={k: v for k, v in {**os.environ, **env}.items() if v is not None}, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return app, app / "Contents" / "Resources" / "brewdash"

    def test_the_version_install_sh_passes_is_sealed_into_the_bundle_with_the_installer(self):
        version = '{"sha": "' + "a" * 40 + '", "tag": "v1.2.3"}'
        app, res = self.build(BREWDASH_VERSION=version)
        self.assertEqual((res / "version.json").read_text(), version + "\n")
        self.assertEqual((res / "install.sh").read_bytes(), (ROOT / "install.sh").read_bytes())
        verify = subprocess.run(["codesign", "--verify", "--strict", str(app)], capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_a_build_without_a_version_has_no_version_file(self):
        _, res = self.build(BREWDASH_VERSION=None)
        self.assertFalse((res / "version.json").exists())
        self.assertTrue((res / "install.sh").exists())


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS only")
class InstallRefTest(unittest.TestCase):
    """install.sh's ref and version helpers, with curl answered by a shell function."""
    CURL = """curl() {{ case "$*" in
  *"/tags?per_page=100") printf '%s' '{tags}' ;;
  *"vnd.github.sha"*"/commits/"*) printf '%s' '{sha}' ;;
  *) return 22 ;;
esac; }}
"""

    def run_sh(self, call, tags="[]", sha="a" * 40):
        script = f'source "{ROOT / "install.sh"}"\n' + self.CURL.format(tags=tags, sha=sha) + call
        return subprocess.run(["/bin/bash", "-c", script], env=dict(os.environ, BREWDASH_SOURCE_ONLY="1"),
                              capture_output=True, text=True, timeout=30)

    def test_latest_tag_is_the_highest_vxyz_by_number(self):
        tags = '[{"name": "v0.9.0"}, {"name": "v0.10.0"}, {"name": "v2.0"}, {"name": "nightly"}]'
        self.assertEqual(self.run_sh("latest_tag", tags=tags).stdout, "v0.10.0\n")
        self.assertEqual(self.run_sh("latest_tag", tags='[{"name": "nightly"}]').stdout, "\n")

    def test_version_json_names_the_commit_and_only_a_vxyz_tag(self):
        self.assertEqual(json.loads(self.run_sh("version_json v1.2.3").stdout), {"sha": "a" * 40, "tag": "v1.2.3"})
        self.assertEqual(json.loads(self.run_sh("version_json master").stdout), {"sha": "a" * 40, "tag": None})

    def test_version_json_refuses_an_answer_that_is_not_a_commit(self):
        r = self.run_sh("version_json master", sha="<html>")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("GitHub did not answer with a commit for master", r.stderr)


@unittest.skipUnless(sys.platform == "darwin" and has_brew(), "install.sh is macOS only and checks for Homebrew")
class InstallFromGitHubTest(unittest.TestCase):
    """install.sh's main with GitHub answered by a shell function and a source archive whose build-app.sh only
    records the version it was handed."""
    SHA = "b" * 40

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        src = self.root / "brewdash-src" / "macos"
        src.mkdir(parents=True)
        (src / "build-app.sh").write_text('mkdir -p "$2"; printf %s "${BREWDASH_VERSION-unset}" > "$2/version"\n')
        self.tarball = self.root / "src.tar.gz"
        subprocess.run(["tar", "-czf", str(self.tarball), "-C", str(self.root), "brewdash-src"], check=True)
        self.calls = self.root / "curl.log"

    def install(self, tags, **env):
        fake = f"""curl() {{ printf '%s\\n' "$*" >> "{self.calls}"; case "$*" in
  *"/tags?per_page=100") printf '%s' '{tags}' ;;
  *"vnd.github.sha"*"/commits/"*) printf '%s' '{self.SHA}' ;;
  *"https://github.com/lemiorhan/brewdash/archive/{self.SHA}.tar.gz") cat "{self.tarball}" ;;
  *) return 22 ;;
esac; }}
quit_running() {{ :; }}
"""
        script = f'source "{ROOT / "install.sh"}"\n{fake}main\n'
        apps = self.root / "Applications"
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, BREWDASH_SOURCE_ONLY="1", BREWDASH_NO_OPEN="1",
                                    BREWDASH_APP_DIR=str(apps), **env))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads((apps / "brewdash.app" / "version").read_text()), self.calls.read_text().splitlines()

    def test_without_a_ref_it_installs_the_highest_tag_by_its_commit(self):
        version, calls = self.install('[{"name": "v0.2.0"}, {"name": "v0.10.0"}]')
        self.assertEqual(version, {"sha": self.SHA, "tag": "v0.10.0"})
        self.assertTrue(any(c.endswith("/commits/v0.10.0") for c in calls), calls)

    def test_without_a_tag_it_installs_master(self):
        version, calls = self.install("[]")
        self.assertEqual(version, {"sha": self.SHA, "tag": None})
        self.assertTrue(any(c.endswith("/commits/master") for c in calls), calls)

    def test_a_ref_skips_the_tags(self):
        version, calls = self.install("[]", BREWDASH_REF="some-branch")
        self.assertEqual(version, {"sha": self.SHA, "tag": None})
        self.assertFalse(any("/tags" in c for c in calls), calls)
        self.assertTrue(any(c.endswith("/commits/some-branch") for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
