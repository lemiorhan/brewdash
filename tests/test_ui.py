"""Browser smoke tests for web/app.js, against a server backed by FakeBrew.

Needs Playwright for Python: `python3 -m venv .venv && .venv/bin/pip install playwright`,
then run the suite with `.venv/bin/python -m unittest discover -s tests`. Skipped without it.
"""
import json
import re
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brewdash  # noqa: E402
from test_brewdash import (APP_CASKS, APP_ROOT, SHA_A, SHA_B, SHA_C, FakeBrew, FakeGitHub, cask, catalog,  # noqa: E402
                           formula, make_app, wait_for, zip_url)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

XSS = '<img src=x onerror="window.pwned=1">'
ATTR_XSS = '" onmouseover="window.pwned=1'
INFO = {
    "formulae": [
        formula("php@8.0", requested=True, versioned_formulae=["php@8.4"], deprecated=True,
                deprecation_reason="versioned_formula"),
        formula("jq", requested=True),
        formula("held", requested=True, pinned=True),
        formula("odd", requested=True, deprecated=True, deprecation_reason=XSS),
        formula("ssl@1"), formula("py", deps=["ssl@1"]),
        formula("ytdl", requested=True, deps=["py"], indirect=["ssl@1"]),
    ],
    "casks": [cask("drv", disabled=True, disable_reason="fails_gatekeeper_check")],
}
OUTDATED = {"formulae": [{"name": n, "current_version": "9.0"} for n in ("jq", "held")],
            "casks": [{"name": "drv", "current_version": "9.0"}]}


class UiBrew(FakeBrew):
    def json(self, *args):
        if args[0] == "outdated":
            return OUTDATED
        if args[0] == "services":
            return [{"name": "jq", "status": "started", "running": True, "loaded": True, "pid": 77, "user": "me",
                     "file": "/x/homebrew.mxcl.jq.plist", "log_path": None}]
        return super().json(*args)


SIGNED = {"Zed": "Authority=Developer ID Application: Zed Industries (ZED1)\n",
          "Shop": "Authority=Apple Mac OS Application Signing\n", "Bare": "Bare.app: code object is not signed at all\n"}


def ui_sysrun(argv, timeout=60):
    if argv[0] == brewdash.CODESIGN:
        return 0, "", SIGNED[Path(argv[-1]).stem]
    return 1, "p77\nf5\nn*:8080\nf6\nn127.0.0.1:9000\n" if "-iTCP" in argv else "", ""


ACCEPT = 'sudo secure-it accept 0123456789 --note "why"'
JQ_TITLE = {"en": "jq listens on *:8080", "tr": "jq *:8080 portunda dinliyor"}
OPEN_PORT = {"id": "exposure.listening/open_port", "category": "exposure", "label": {"en": "Open port", "tr": "Açık port"},
             "effort": "medium", "code": "exposure.listening · open_port", "explain": {
                 "what": {"en": "A port other machines can reach.", "tr": "Başka makinelerin erişebildiği bir port."},
                 "why": "Anyone on the network can talk to it.", "attack": "Attackers scan for it.",
                 "sources": [{"title": "MITRE ATT&CK T1046", "kind": "framework", "url": "https://attack.mitre.org/techniques/T1046/", "verified": "2026-10-01"},
                             {"title": "Apple Platform Security", "kind": "vendor", "url": None, "verified": None}]}}
TOPICS = [OPEN_PORT,
          {"id": "persistence.path_shadow", "category": "persistence", "label": "PATH shadowing", "effort": "small",
           "code": "persistence.path_shadow", "explain": None},
          {"id": "packages.brew_taps", "category": "packages", "label": "Third-party taps", "effort": "small", "code": "packages.brew_taps", "explain": None},
          {"id": "packages.inventory", "category": "packages", "label": "Inventory", "effort": None, "code": "packages.inventory", "explain": None}]


def finding(fingerprint, topic, severity, title, row=None, **extra):
    return {"fingerprint": fingerprint, "topic": topic, "row": row or {"key": f"title:{title}", "label": title}, "severity": severity,
            "title": title, "detail": None, "remediation": None, "fix_command": None, "accept_command": None, "accepted": None,
            "links": [], "subject": None, **extra}


JQ = dict(row={"key": "subject:/opt/homebrew/Cellar/jq/1.7/bin/jq", "label": "jq"}, detail="pid 77",
          remediation={"en": "Stop the service.", "tr": "Servisi durdur."}, fix_command="brew services stop jq",
          accept_command={"en": ACCEPT, "tr": 'sudo secure-it accept 0123456789 --note "neden"'},
          subject={"kind": "path", "path": "/opt/homebrew/Cellar/jq/1.7/bin/jq"})
FINDINGS = [
    finding("0123456789abcdef", OPEN_PORT["id"], "high", JQ_TITLE, **JQ,
            links=[{"label": "CVE-2026-1", "url": "https://osv.dev/vulnerability/CVE-2026-1"}]),
    finding("0123456789abcdef", OPEN_PORT["id"], "medium", "jq listens on 127.0.0.1:9000", **JQ),
    finding("cccccccccc", OPEN_PORT["id"], "low", "cupsd listens on *:631", row={"key": "subject:/usr/sbin/cupsd", "label": "cupsd"},
            remediation={"en": "Stop the service.", "tr": "Servisi durdur."}, accepted={"at": "2026-10-30T12:00:00", "note": "printer sharing"},
            accept_command='sudo secure-it accept cccccccccc --note "why"'),
    finding("ffffffffff", "persistence.path_shadow", "low", "~/bin shadows 1 system command", detail="git", remediation="Move it after $PATH.",
            accept_command='sudo secure-it accept ffffffffff --note "why"', subject={"kind": "shell"}),
    finding("eeeeeeeeee", "packages.brew_taps", "medium", XSS, row={"key": "subject:me/tap", "label": XSS}, detail=ATTR_XSS,
            fix_command=f"brew untap {ATTR_XSS}", accept_command=["not", "a", "string"], subject={"kind": "tap", "name": "me/tap"}),
    finding("dddddddddd", "packages.inventory", "info", "Homebrew: 7 formulae"),
]
SIP = {"en": "SIP is on", "tr": "SIP açık"}


def run_alert(f, accepted=False):
    return {"fingerprint": f["fingerprint"], "severity": f["severity"], "title": f["title"], "accepted": accepted}


REPORT = {"schema": 3, "generated_at": "2026-11-03T08:01:00",
          "scan": {"started_at": "2026-11-03T08:00:00", "facts": [{"label": "host", "value": "mac"}, {"label": {"en": "user", "tr": "kullanıcı"}, "value": "me"}]},
          "categories": [{"id": "exposure", "label": {"en": "Network", "tr": "Ağ"}}, {"id": "packages", "label": "Packages"},
                         {"id": "persistence", "label": "Persistence"}, {"id": "system", "label": "System"}],
          "efforts": [{"id": "small", "label": {"en": "small", "tr": "küçük"}}, {"id": "medium", "label": "medium"}, {"id": "large", "label": "large"}],
          "topics": TOPICS, "findings": FINDINGS,
          "history": [
              {"started_at": "2026-11-01T08:00:00",
               "alerts": [run_alert(FINDINGS[0]), {"fingerprint": "7e7e7e7e7e", "severity": "medium", "title": "telnetd listens on *:23", "accepted": False}],
               "checks": [{"id": "system.sip", "title": SIP, "status": "error", "reason": "timeout after 30s"}]},
              {"started_at": "2026-11-02T08:00:00", "alerts": [run_alert(f) for f in (FINDINGS[0], FINDINGS[1], FINDINGS[3])],
               "checks": [{"id": "system.sip", "title": SIP, "status": "skipped", "reason": {"en": "Full Disk Access missing", "tr": "Full Disk Access yok"}}]},
              {"started_at": "2026-11-03T08:00:00",
               "alerts": [run_alert(f) for f in (FINDINGS[0], FINDINGS[1], FINDINGS[3], FINDINGS[4])] + [run_alert(FINDINGS[2], accepted=True)],
               "checks": []}]}
SECURE_IT = "plugin-secure-it"
SCAN = ["/usr/bin/sudo", "-A", "/usr/local/bin/secure-it", "scan", "--exit-zero"]


class Installer:
    """A spawned install.sh that runs until the test sets its exit status."""
    returncode = None

    def poll(self):
        return self.returncode


class UiFixture(unittest.TestCase):
    spawned = []
    installers = []

    @classmethod
    def updater(cls, state):
        github = FakeGitHub(compare={SHA_B: "ahead", SHA_C: "ahead"})

        def spawn(*a):
            cls.spawned.append(a)
            cls.installers.append(Installer())
            return cls.installers[-1]
        return brewdash.Updater(state, {"sha": SHA_A, "tag": "v0.9.0"}, root=APP_ROOT, fetch=github, spawn=spawn)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "cache" / "api").mkdir(parents=True)
        (root / "cache" / "api" / "cask_names.txt").write_text("zed\nshop\n")
        cls.zed = make_app(root / "Applications", "Zed", "0.8")
        make_app(root / "Applications", "Shop", "1.0", app_store=True)
        make_app(root / "Applications", "Bare", "1.0")
        cls.report = root / "home" / ".local" / "state" / "brewdash" / "plugins" / "secure-it" / "report.json"
        cls.report.parent.mkdir(parents=True)
        cls.report.write_text(json.dumps(REPORT))
        cls.legacy_report = root / "home" / "secure-it" / "report.json"
        (root / "home" / ".zshrc").write_text("export PATH=/nope/bin:$PATH\nexport PATH=$HOME/Dropbox/bin:$PATH\n")
        cls.list_file = root / "state" / "list.json"
        (root / "prefix" / "Cellar" / "jq").mkdir(parents=True)
        (root / "prefix" / "Cellar" / "jq" / "bin").write_bytes(b"x" * 65536)
        cls.brew = UiBrew(info=INFO, cache=str(root / "cache"), casks=APP_CASKS, prefix=str(root / "prefix"))
        cls.exe = root / "secure-it"
        cls.exe.write_text("")
        cls.plugins_dir = root / "state" / "plugins"
        cls.plugins_dir.mkdir(parents=True)
        cls.home = root / "home"
        cls.unprotected = {}
        cls.server, cls.url, _ = brewdash.serve(port=0, open_browser=False, brew=cls.brew, state_dir=str(root / "state"),
                                                home=str(root / "home"), app_dirs=[str(root / "Applications")],
                                                sysrun=ui_sysrun, updater=cls.updater(root / "state"),
                                                catalog=catalog(str(cls.exe)), unprotected=cls.unprotected.get)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.list_file.unlink(missing_ok=True)
        self.page = self.browser.new_page()
        self.page.goto(self.url)
        self.page.wait_for_selector(".ov .att")

    def tearDown(self):
        self.page.close()

    def open(self, tab, pid):
        self.page.click(f'nav button[data-tab="{tab}"]')
        self.page.click(f'tr.row[data-id="{pid}"] td:nth-child(2)')
        self.page.wait_for_selector("#detail h2.detail-title")

    def advice(self):
        return self.page.inner_text("#detail .detail-section:has(h4:text-is('What to do'))")

    def attention(self):
        return [(a.locator(".att-level").inner_text().lower(), a.get_attribute("data-overview"),
                 a.locator("b").inner_text()) for a in self.page.locator(".att").all()]


@unittest.skipUnless(sync_playwright, "Playwright for Python is not installed")
class UiSmokeTest(UiFixture):

    def test_overview_is_the_start_page_and_lists_what_needs_attention_worst_first(self):
        self.page.wait_for_selector('.att:has-text("cleanup")')
        self.page.wait_for_selector('.att:has-text("apps without a real signature")')
        self.assertEqual(self.attention(), [
            ("critical", "health", "3 deprecated or disabled"),
            ("critical", SECURE_IT, "1 brewdash security plugin findings"),
            ("warning", "health", "brew doctor has warnings"),
            ("warning", "requested:upgradable", "1 can be upgraded in Installed by you"),
            ("warning", "requested:outdated,pinned", "1 outdated but pinned in Installed by you"),
            ("warning", "update", "brew update is stale"),
            ("warning", "shell", "1 synced folders ahead in PATH"),
            ("warning", "shell", "1 PATH problems"),
            ("warning", SECURE_IT, "2 brewdash security plugin findings"),
            ("warning", "services", "1 services reachable from the network"),
            ("warning", "apps", "1 apps without a real signature"),
            ("info", SECURE_IT, "1 brewdash security plugin findings"),
            ("info", "health", "cleanup"),
            ("info", "apps", "1 apps brew could manage"),
            ("info", "taps", "1 unused taps"),
        ])
        self.assertEqual([a.locator("b + .muted").inner_text() for a in self.page.locator(f'.att[data-overview="{SECURE_IT}"]').all()],
                         ["jq listens on *:8080", f"jq listens on 127.0.0.1:9000; {XSS}", "~/bin shadows 1 system command"])

    def rows(self):
        return [r.get_attribute("data-id") for r in self.page.locator("tr.row").all()]

    def test_each_package_item_opens_exactly_the_packages_it_counts(self):
        self.page.evaluate("S.pkgs.get('f:ssl@1').outdated = true; S.pkgs.get('f:py').outdated = true; S.pkgs.get('f:py').pinned = true; renderView()")
        items = {go: title for _, go, title in self.attention()}
        for go, title, rows in [("requested:upgradable", "1 can be upgraded in Installed by you", ["f:jq"]),
                                ("deps:upgradable", "1 can be upgraded in Dependencies", ["f:ssl@1"]),
                                ("requested:outdated,pinned", "1 outdated but pinned in Installed by you", ["f:held"]),
                                ("deps:outdated,pinned", "1 outdated but pinned in Dependencies", ["f:py"])]:
            self.assertEqual(items[go], title)
            self.page.click('nav button[data-tab="overview"]')
            self.page.click(f'.att[data-overview="{go}"]')
            self.page.wait_for_selector(f'nav button.active[data-tab="{go.split(":")[0]}"]')
            self.assertEqual(self.rows(), rows, go)

    def test_missing_dependencies_and_orphans_are_listed_and_names_are_escaped(self):
        self.page.evaluate("""xss => {
            const base = S.pkgs.get('f:py');
            S.pkgs.set('f:x', { ...base, id: 'f:x', name: xss, missing_deps: ['gone'], orphan: true, required_by: ['f:ytdl'] });
            S.snap.sizes['f:x'] = 999999;
            S.snap.apps.apps.push({ ...S.snap.apps.apps.find((a) => a.cask === 'zed'), id: 'a:x', name: xss });
            renderView();
        }""", XSS)
        att = self.attention()
        self.assertEqual(att[0], ("critical", "health", "1 missing dependencies"))
        self.assertIn(("info", "health", "1 orphans"), att)
        self.assertIn(XSS, self.page.inner_text(".att:has-text('missing dependencies')"))
        self.assertIn(XSS, self.page.inner_text(".att:has-text('apps brew could manage')"))
        self.assertIn(XSS, self.page.inner_text(".card:has(h3:text-is('Largest on disk'))"))
        self.assertIn(XSS, self.page.inner_text(".card:has(h3:text-is('Most depended on'))"))
        self.assertEqual(self.page.locator("#view img").count(), 0)
        self.assertIsNone(self.page.evaluate("window.pwned"))

    def test_an_overview_click_drops_a_leftover_search(self):
        self.page.click('nav button[data-tab="requested"]')
        self.page.fill("#q", "zzz")
        self.page.click('nav button[data-tab="overview"]')
        self.page.click('.att[data-overview="requested:upgradable"]')
        self.page.wait_for_selector('nav button.active[data-tab="requested"]')
        self.assertEqual(self.rows(), ["f:jq"])
        self.assertEqual(self.page.input_value("#q"), "")

    def test_names_cannot_break_out_of_a_title_attribute(self):
        self.page.evaluate("""attr => {
            S.pkgs.set('f:a', { ...S.pkgs.get('f:py'), id: 'f:a', name: attr, installed_time: Date.now() / 1000 });
            S.snap.sizes['f:a'] = 999999;
            renderView();
        }""", ATTR_XSS)
        self.assertIn(ATTR_XSS, self.page.get_attribute('.card:has(h3:text-is("Largest on disk")) .bar-row[data-goto="f:a"]', "title"))
        self.assertIn(ATTR_XSS, self.page.locator(".month").last.get_attribute("title"))
        self.assertEqual(self.page.locator("#view [onmouseover]").count(), 0)

    def test_a_recent_brew_update_is_not_flagged(self):
        self.page.evaluate("S.snap.last_update = Date.now() / 1000 - 6 * 86400; renderView()")
        self.assertNotIn("update", [go for _, go, _ in self.attention()])
        self.page.evaluate("S.snap.last_update = Date.now() / 1000 - 8 * 86400; renderView()")
        self.assertIn("update", [go for _, go, _ in self.attention()])

    def test_tiles_show_the_totals(self):
        self.page.wait_for_selector('.tile:has-text("64 KB")')
        tiles = [t.inner_text().split("\n") for t in self.page.locator(".tile").all()]
        self.assertEqual(tiles, [
            ["8", "packages", "5 by you · 2 dependencies · 1 casks"],
            ["64 KB", "on disk", "formulae 64 KB · casks 0 KB"],
            ["3", "outdated", "1 can be upgraded now"],
            ["15", "things to look at", "worst: critical"],
            ["1/1", "services running"],
            ["3", "apps", "0 managed by brew"],
            ["never", "last brew update"],
        ])

    def test_stale_update_item_runs_brew_update_through_the_confirm_dialog(self):
        self.page.click('.att[data-overview="update"]')
        self.page.wait_for_selector("#modal-card pre")
        self.assertEqual(self.page.inner_text("#modal-card pre"), "brew update")

    def test_composition_chart_names_each_part_with_its_count(self):
        self.assertEqual(self.page.locator(".card:has(h3:text-is('What is installed')) .legend").inner_text().split("\n"),
                         ["5 installed by you", "2 dependencies", "1 casks"])
        self.assertEqual(self.page.locator(".card:has(h3:text-is('How apps got here')) .legend").inner_text().split("\n"),
                         ["1 App Store", "2 by hand"])
        self.page.click(".card:has(h3:text-is('What is installed')) .seg[data-overview='deps']")
        self.page.wait_for_selector('nav button.active[data-tab="deps"]')
        self.assertEqual(self.rows(), ["f:py", "f:ssl@1"])

    def test_most_depended_on_counts_direct_and_indirect_dependents(self):
        rows = self.page.locator(".card:has(h3:text-is('Most depended on')) .bar-row").all()
        self.assertEqual([r.inner_text().split("\n") for r in rows], [["ssl@1", "2 pkg"], ["py", "1 pkg"]])

    def test_disk_bar_opens_its_package(self):
        self.page.click('.card:has(h3:text-is("Largest on disk")) .bar-row[data-goto="f:jq"]')
        self.page.wait_for_selector('#detail h2.detail-title:text-is("jq")')

    def test_installs_chart_puts_each_package_in_its_month(self):
        self.page.evaluate("""() => {
            const now = Date.now() / 1000;
            S.pkgs.get('f:jq').installed_time = now; S.pkgs.get('f:ytdl').installed_time = now;
            renderView();
        }""")
        bars = self.page.locator(".month").all()
        self.assertEqual(len(bars), 24)
        self.assertRegex(bars[-1].get_attribute("title"), r"^\d{4}-\d{2}: 2 \((jq, ytdl|ytdl, jq)\)$")
        self.assertEqual(self.page.locator(".months-axis + p").inner_text(), "6 package(s) were installed before " +
                         bars[0].get_attribute("title").split(":")[0] + ".")

    def test_header_counts_outdated_but_upgrades_only_what_brew_can(self):
        self.assertEqual(self.page.inner_text("#btn-upgrade-all"), "Upgrade all (1)")
        self.assertIn("3 outdated", self.page.inner_text("header"))

    def test_disabled_outdated_cask_shows_advice_and_no_upgrade_button(self):
        self.open("casks", "c:drv")
        self.assertIn("Version 9.0 exists, but not through brew.", self.advice())
        self.assertEqual(self.page.locator('#detail [data-act="upgrade"]').count(), 0)

    def test_upgradable_package_has_the_upgrade_button(self):
        self.open("requested", "f:jq")
        self.assertEqual(self.page.locator('#detail [data-act="upgrade"]').count(), 1)
        self.open("requested", "f:held")
        self.assertEqual(self.page.locator('#detail [data-act="upgrade"]').count(), 0)

    def test_advice_button_runs_its_request_through_the_confirm_dialog(self):
        self.open("requested", "f:php@8.0")
        self.page.click('#detail button:text-is("Install php@8.4")')
        self.page.wait_for_selector("#modal-card pre")
        self.assertIn("brew install --formula php@8.4", self.page.inner_text("#modal-card"))
        self.page.click("#m-ok")
        wait_for(lambda: ["brew", "install", "--formula", "php@8.4"] in self.brew.popened)

    def test_advice_text_is_escaped(self):
        self.open("requested", "f:odd")
        self.assertIn(XSS, self.advice())
        self.assertEqual(self.page.locator("#detail img").count(), 0)
        self.assertIsNone(self.page.evaluate("window.pwned"))

    def test_bulk_upgrade_is_disabled_when_nothing_selected_can_be_upgraded(self):
        self.page.click('nav button[data-tab="requested"]')
        self.page.check('[data-sel="f:held"]')
        self.assertTrue(self.page.is_disabled('[data-bulk="upgrade"]'))
        self.page.check('[data-sel="f:jq"]')
        self.assertFalse(self.page.is_disabled('[data-bulk="upgrade"]'))

    def bulk(self):
        return {b.get_attribute("data-bulk"): (b.inner_text(), b.is_disabled(), b.get_attribute("title"))
                for b in self.page.locator("#bulk button").all()}

    def test_bulk_bar_counts_what_each_action_applies_to_and_says_why_one_is_off(self):
        self.page.click('nav button[data-tab="requested"]')
        self.page.check('[data-sel="f:jq"]')
        self.page.check('[data-sel="f:held"]')
        self.assertEqual(self.bulk(), {
            "upgrade": ("Upgrade (1)", False, None), "pin": ("Pin (1)", False, None), "unpin": ("Unpin (1)", False, None),
            "uninstall": ("Uninstall (2)", False, None),
            "untap": ("Untap (0)", True, "Only taps nothing is installed from; uninstall their packages first."),
            "list": ("Add to list (2)", False, None), "clear": ("Clear", False, None)})
        self.page.uncheck('[data-sel="f:jq"]')
        self.assertEqual(self.bulk()["upgrade"], ("Upgrade (0)", True, "Nothing selected is outdated, or it is pinned or disabled."))
        self.assertEqual(self.bulk()["pin"], ("Pin (0)", True, "Everything selected is already pinned."))
        self.assertLess(self.page.locator("#bulk").bounding_box()["y"], self.page.locator("#view table").bounding_box()["y"])

    def test_bulk_action_runs_only_on_the_items_it_applies_to(self):
        self.page.click('nav button[data-tab="requested"]')
        self.page.check('[data-sel="f:jq"]')
        self.page.check('[data-sel="f:held"]')
        self.page.click('[data-bulk="pin"]')
        self.page.wait_for_selector("#modal-card pre")
        self.assertEqual(self.page.inner_text("#modal-card pre"), "brew pin --formula jq")

    def test_a_tap_alone_offers_the_list_and_untap(self):
        self.page.click('nav button[data-tab="taps"]')
        self.page.check('[data-sel="t:me/tap"]')
        b = self.bulk()
        self.assertEqual(b["list"], ("Add to list (1)", False, None))
        self.assertEqual(b["untap"], ("Untap (1)", False, None))
        self.assertEqual(b["uninstall"], ("Uninstall (0)", True, "Only installed formulae and casks; the selection has none."))

    def test_untap_skips_a_selected_tap_something_is_installed_from(self):
        self.page.click('nav button[data-tab="taps"]')
        self.page.check('[data-sel="t:me/tap"]')
        self.page.check('[data-sel="t:homebrew/core"]')
        self.assertEqual(self.bulk()["untap"], ("Untap (1)", False, None))
        self.page.click('[data-bulk="untap"]')
        self.page.wait_for_selector("#modal-card pre")
        self.assertEqual((self.page.inner_text("#modal-card h2"), self.page.inner_text("#modal-card pre")),
                         ("untap me/tap", "brew untap me/tap"))
        self.page.click("#m-ok")
        wait_for(lambda: ["brew", "untap", "me/tap"] in self.brew.popened)
        self.assertEqual(self.page.evaluate("[...S.selected]"), ["t:homebrew/core"])

    def test_a_new_state_keeps_selected_taps_and_apps(self):
        self.page.click('nav button[data-tab="taps"]')
        self.page.check('[data-sel="t:me/tap"]')
        self.page.click('nav button[data-tab="apps"]')
        self.page.check(f'[data-sel="a:{self.zed}"]')
        self.page.evaluate("S.version = -1; loadState()")
        self.assertTrue(self.page.is_visible("#bulk"))
        self.assertTrue(self.page.is_checked(f'[data-sel="a:{self.zed}"]'))
        self.assertEqual(self.bulk()["list"], ("Add to list (2)", False, None))

    def test_marked_packages_taps_and_apps_end_up_in_the_brewfile(self):
        self.page.click('nav button[data-tab="requested"]')
        self.page.check('[data-sel="f:jq"]')
        self.page.click('nav button[data-tab="taps"]')
        self.page.check('[data-sel="t:me/tap"]')
        self.page.click('nav button[data-tab="apps"]')
        self.page.check(f'[data-sel="a:{self.zed}"]')
        self.page.click('[data-bulk="list"]')
        self.page.wait_for_selector('nav button[data-tab="export"] .count:text-is("3")')
        self.assertEqual(self.bulk()["list"], ("Add to list (0)", True, "Everything selected is already on the export list."))
        self.page.click('nav button[data-tab="export"]')
        brewfile = ('# Brewfile from brewdash. On the new machine: brew bundle --file=Brewfile\n\n'
                    'tap "me/tap"\nbrew "jq"\ncask "zed"\n')
        self.assertEqual(self.page.text_content("#brewfile"), brewfile)
        with self.page.expect_download() as d:
            self.page.click("#dl-brewfile")
        self.assertEqual((d.value.suggested_filename, Path(d.value.path()).read_text()), ("Brewfile", brewfile))
        self.page.click('[data-unlist="brew:jq"]')
        self.page.wait_for_selector('nav button[data-tab="export"] .count:text-is("2")')
        self.assertNotIn('brew "jq"', self.page.inner_text("#brewfile"))
        self.assertIn("brew bundle --file=Brewfile", self.page.inner_text("#view"))

    def test_apps_tab_offers_adopt_only_for_a_matched_hand_installed_app(self):
        self.page.click('nav button[data-tab="apps"]')
        self.page.wait_for_selector("[data-adopt]")
        rows = {r.locator("td.name").inner_text(): r for r in self.page.locator("#view tbody tr").all()}
        self.assertEqual(rows["Shop"].locator("input, button:not([data-trash])").count(), 0)
        self.assertIn("updates through the App Store", rows["Shop"].inner_text())
        self.assertEqual(rows["Bare"].locator("button:not([data-trash])").inner_text(), "Search cask")
        self.assertEqual(self.page.locator("[data-adopt]").count(), 1)
        argv = ["/bin/sh", "-c", brewdash.ADOPT_SCRIPT, "sh", self.zed,
                str(Path(self.zed).with_name(".Zed.app.brewdash-backup")), "/fake/bin/brew", "zed"]
        rows["Zed"].locator("[data-adopt]").click()
        self.page.wait_for_selector("#modal-card pre")
        self.assertEqual(self.page.inner_text("#modal-card pre"), shlex.join(argv))
        self.assertEqual(self.page.inner_text("#modal-card .warn"),
                         "Quit Zed before you run this. Its .app is renamed and replaced while brew installs, and an "
                         "open copy can crash and lose unsaved work. Its settings in ~/Library are not touched.")
        self.page.click("#m-ok")
        wait_for(lambda: argv in self.brew.popened)

    def test_an_app_newer_than_its_cask_is_listed_but_not_offered_or_counted_for_adopt(self):
        self.page.evaluate("""() => {
            S.snap.apps.apps.push({ ...S.snap.apps.apps.find((a) => a.cask === 'zed'), id: 'a:ahead', name: 'Ahead',
                                    version: '1.0', cask_version: '0.9,42', newer_than_cask: true });
            renderView();
        }""")
        names = self.page.inner_text(".att:has-text('apps brew could manage')")
        self.assertIn("Zed", names)
        self.assertNotIn("Ahead", names)
        self.page.click('nav button[data-tab="apps"]')
        self.page.wait_for_selector("[data-adopt]")
        rows = {r.locator("td.name").inner_text(): r for r in self.page.locator("#view tbody tr").all()}
        self.assertEqual(rows["Ahead"].locator("[data-adopt]").count(), 0)
        self.assertIn("zed your copy is newer than the cask's 0.9, so it is not brought into brew: brew would install "
                      "the older one over it", rows["Ahead"].inner_text())
        self.assertEqual(rows["Zed"].locator("[data-adopt]").count(), 1)
        self.assertIn("1 app(s) installed by hand have a matching brew cask", self.page.inner_text("#view"))

    def test_apps_tab_moves_an_app_brew_did_not_install_to_the_trash(self):
        self.page.click('nav button[data-tab="apps"]')
        self.page.wait_for_selector("[data-trash]")
        rows = {r.locator("td.name").inner_text(): r for r in self.page.locator("#view tbody tr").all()}
        self.assertEqual({n: r.locator("[data-trash]").inner_text() for n, r in rows.items()},
                         {"Bare": "Move to Trash", "Shop": "Move to Trash", "Zed": "Move to Trash"})
        self.assertEqual(self.page.locator("[data-uninstall]").count(), 0)
        shop = str(Path(self.zed).with_name("Shop.app"))
        argv = ["osascript", "-e", "on run argv", "-e", "set f to POSIX file (item 1 of argv) as alias",
                "-e", 'tell application "Finder" to delete f', "-e", "end run", shop]
        rows["Shop"].locator("[data-trash]").click()
        self.page.wait_for_selector("#modal-card pre")
        self.assertEqual((self.page.inner_text("#modal-card h2"), self.page.inner_text("#modal-card pre")),
                         ("move Shop to the Trash", shlex.join(argv)))
        self.page.click("#m-ok")
        wait_for(lambda: argv in self.brew.popened)

    def test_apps_tab_uninstalls_an_app_brew_installed_through_its_cask(self):
        self.page.click('nav button[data-tab="apps"]')
        self.page.wait_for_selector("[data-trash]")
        self.page.evaluate("""S.snap.apps.apps.push({ ...S.snap.apps.apps.find((a) => a.name === 'Shop'),
            id: 'a:/x/Drv.app', name: 'Drv', source: 'brew', brew_id: 'c:drv' }); renderView()""")
        row = self.page.locator('#view tbody tr:has(td.name:text-is("Drv"))')
        self.assertEqual(row.locator("[data-trash]").count(), 0)
        row.locator("[data-uninstall]").click()
        self.page.wait_for_selector("#modal-card pre")
        self.assertEqual(self.page.inner_text("#modal-card pre"), "brew uninstall --cask drv")
        self.page.click("#m-ok")
        wait_for(lambda: ["brew", "uninstall", "--cask", "drv"] in self.brew.popened)

    def test_shell_tab_shows_the_issue_and_what_to_do(self):
        self.page.click('nav button[data-tab="shell"]')
        row = self.page.locator('#view tbody tr:has-text("/nope/bin")')
        self.assertEqual(row.inner_text().split("\t"), [".zshrc:1", "/nope/bin", "MISSING", "The directory does not exist; this line can go."])
        synced = self.page.locator('#view tbody tr:has-text("Dropbox")').inner_text().split("\t")
        self.assertEqual(synced[:3], [".zshrc:2", "$HOME/Dropbox/bin", "SYNCED FOLDER FIRST"])

    def test_sources_brewdash_could_not_read_are_said_and_escaped(self):
        self.page.evaluate("""S.snap.ports = {error: 'could not run lsof: <b>x</b>', by_id: {}};
            S.snap.plugins = S.snap.plugins.map((p) => ({...p, error: 'could not read it: <i>y</i>', alerts: []})); renderAll()""")
        self.assertIn(("warning", SECURE_IT, "brewdash security plugin report unreadable"), self.attention())
        self.tab("services")
        self.assertEqual(self.page.inner_text("#content .warn"), "could not run lsof: <b>x</b>")
        self.tab(SECURE_IT)
        self.assertEqual(self.page.inner_text("#content .warn"),
                         f"brewdash could not use {self.report}: could not read it: <i>y</i>")

    def uninstall_ssl(self):
        self.open("deps", "f:ssl@1")
        self.page.click('#detail [data-act="uninstall"]')
        self.page.wait_for_selector("#add-blockers")

    def where(self):
        return (self.page.get_attribute("nav button.active", "data-tab"),
                self.page.locator("#detail:not(.hidden) h2.detail-title").all_inner_texts())

    def test_back_and_forward_walk_the_tabs_and_packages_opened(self):
        self.open("requested", "f:ytdl")
        self.page.click('#detail .chip[data-goto="f:py"]')
        self.page.click('nav button[data-tab="services"]')
        steps = [("services", []), ("deps", ["py"]), ("requested", ["ytdl"]), ("requested", []), ("overview", [])]
        for want in steps[1:]:
            self.page.go_back()
            self.assertEqual(self.where(), want)
        for want in reversed(steps[:-1]):
            self.page.go_forward()
            self.assertEqual(self.where(), want)

    def test_a_package_opened_from_the_graph_is_a_history_step(self):
        self.open("requested", "f:ytdl")
        self.page.wait_for_function("cy && cy.$id('f:py').length")
        self.page.evaluate("cy.$id('f:py').emit('tap')")
        self.assertEqual(self.where(), ("deps", ["py"]))
        self.page.go_back()
        self.assertEqual(self.where(), ("requested", ["ytdl"]))

    def test_a_filter_toggle_does_not_add_a_history_step(self):
        self.page.click('nav button[data-tab="requested"]')
        self.page.click('[data-flag="outdated"]')
        self.page.click('[data-flag="pinned"]')
        self.assertEqual(self.rows(), ["f:held"])
        self.page.go_back()
        self.assertEqual(self.where(), ("overview", []))

    def test_a_reload_keeps_the_tab_package_and_filters(self):
        self.open("requested", "f:jq")
        self.page.click('[data-flag="outdated"]')
        self.page.reload()
        self.page.wait_for_selector("#detail h2.detail-title")
        self.assertEqual(self.where(), ("requested", ["jq"]))
        self.assertEqual(self.rows(), ["f:held", "f:jq"])

    def hist(self):
        return self.page.evaluate("[history.length, location.hash]")

    def test_a_click_that_changes_nothing_adds_no_history_step(self):
        before = self.hist()
        self.page.click("#intro")
        self.assertEqual(self.hist(), before)
        self.assertEqual(before[1], "#tab=overview")

    def test_a_package_brew_no_longer_has_leaves_no_dead_history_step(self):
        self.open("requested", "f:jq")
        self.page.evaluate("""async () => {
            const snap = { ...S.snap, version: S.snap.version + 1,
                           model: { ...S.snap.model, packages: S.snap.model.packages.filter((p) => p.id !== 'f:jq') } };
            api = async () => snap;
            await loadState();
        }""")
        before = self.hist()
        self.assertEqual(before[1], "#tab=requested")
        self.page.click("#intro")
        self.assertEqual(self.hist(), before)
        self.page.go_back()
        self.page.wait_for_function("location.hash === '#tab=overview'")
        self.assertEqual(self.where(), ("overview", []))
        self.page.go_forward()
        self.assertEqual(self.where(), ("requested", []))

    def test_forward_steps_over_a_copied_entry_too(self):
        self.open("requested", "f:jq")
        self.page.click('nav button[data-tab="services"]')
        self.page.evaluate("""async () => {
            const snap = { ...S.snap, version: S.snap.version + 1,
                           model: { ...S.snap.model, packages: S.snap.model.packages.filter((p) => p.id !== 'f:jq') } };
            api = async () => snap;
            await loadState();
        }""")
        self.page.go_back()
        self.page.wait_for_function("location.hash === '#tab=requested'")
        self.page.go_back()
        self.page.wait_for_function("location.hash === '#tab=overview'")
        self.page.go_forward()
        self.page.wait_for_function("location.hash === '#tab=requested'")
        self.page.go_forward()
        self.page.wait_for_function("location.hash === '#tab=services'", timeout=3000)
        self.assertEqual(self.where(), ("services", []))

    def test_stepping_over_a_copied_entry_never_leaves_the_app(self):
        self.page.evaluate("history.pushState({ i: 1 }, '', '#tab=overview&pkg=f%3Agone'); navIndex = 1")
        self.page.go_back()
        self.page.wait_for_timeout(300)
        self.assertTrue(self.page.url.startswith(self.url.split("#")[0]), self.page.url)
        self.assertEqual(self.where(), ("overview", []))

    def test_a_hash_naming_what_does_not_exist_falls_back(self):
        errors = []
        self.page.on("pageerror", lambda e: errors.append(e))
        for h, want in [("#tab=bogus", "#tab=overview"), ("#tab=requested&pkg=f%3Anope&flags=bogus,outdated", "#tab=requested&flags=outdated")]:
            self.page.goto(self.url + h)
            self.page.wait_for_selector(".intro, .ov .att")
            self.page.wait_for_function("S.snap?.model")
            self.assertEqual(self.page.evaluate("location.hash"), want)
            before = self.hist()
            self.page.click("#intro")
            self.assertEqual(self.hist(), before)
        self.assertEqual(errors, [])

    def test_a_description_cannot_break_out_of_a_title_attribute(self):
        self.page.evaluate("attr => { S.pkgs.get('f:jq').desc = attr; S.snap.sizes['f:jq'] = 999999; renderView() }", ATTR_XSS)
        self.assertTrue(self.page.get_attribute('.bar-row[data-goto="f:jq"]', "title").endswith(" — " + ATTR_XSS))
        self.page.click('nav button[data-tab="requested"]')
        self.assertEqual(self.page.get_attribute('tr.row[data-id="f:jq"] td.name', "title"), ATTR_XSS)
        self.page.click('tr.row[data-id="f:ytdl"] td:nth-child(2)')
        self.page.click('nav button[data-tab="services"]')
        self.assertEqual(self.page.get_attribute('#content .chip[data-goto="f:jq"]', "title"), ATTR_XSS)
        self.assertEqual(self.page.locator("[onmouseover]").count(), 0)

    def test_an_app_brew_manages_shows_its_cask_description_on_hover(self):
        self.tab("apps")
        self.page.evaluate("""() => { S.snap.apps.apps.push({ ...S.snap.apps.apps[0], id: 'a:drv', name: 'Drv', source: 'brew', brew_id: 'c:drv' });
                                      renderView() }""")
        self.assertEqual(self.page.get_attribute('#content .chip[data-goto="c:drv"]', "title"), "drv app")

    def test_package_names_show_their_description_on_hover(self):
        self.open("requested", "f:ytdl")
        self.assertEqual(self.page.get_attribute('tr.row[data-id="f:jq"] td.name', "title"), "jq desc")
        self.assertEqual(self.page.get_attribute('#detail .chip[data-goto="f:py"]', "title"), "py desc")
        self.page.click('nav button[data-tab="services"]')
        self.assertEqual(self.page.get_attribute('#content .chip[data-goto="f:jq"]', "title"), "jq desc")
        self.page.click('nav button[data-tab="overview"]')
        self.assertTrue(self.page.get_attribute('.bar-row[data-goto="f:jq"]', "title").endswith(" — jq desc"))

    def records(self, page=None):
        sub = lambda r: r.locator(".rec-sub").text_content() if r.locator(".rec-sub").count() else ""  # noqa: E731
        return {r.locator(".rec-title").text_content(): (r.locator(".rec-name").text_content(),
                                                         r.locator(".rec-value").text_content(), sub(r))
                for r in (page or self.page).locator(".rec").all()}

    def test_records_name_the_package_its_value_and_its_ties(self):
        recs = self.records()
        self.assertEqual(recs["Most dependencies"], ("ytdl", "2 packages", "1 direct"))
        self.assertEqual(recs["Longest dependency chain"], ("ytdl", "2 levels", "ytdl → py → ssl@1"))
        self.assertEqual(recs["Shortest name"], ("jq", "2 characters", "also py"))
        self.assertEqual(recs["Longest name"], ("php@8.0", "7 characters", ""))
        self.assertEqual(recs["Highest version"], ("drv", "2.0", ""))
        self.assertEqual(recs["Latest install or upgrade"][0], "drv")
        name, _, sub = recs["Longest without an upgrade"]
        self.assertEqual(name, "held")
        self.assertTrue(sub.endswith(" · also jq, odd, php@8.0 and 3 more"), sub)
        self.page.click('.rec-also[data-goto="f:py"]')
        self.assertEqual(self.page.inner_text("#detail h2.detail-title"), "py")

    def test_numbers_count_what_their_label_says(self):
        rows = {r.locator("span").first.text_content(): (r.locator(".num-value").text_content(), r.get_attribute("title"))
                for r in self.page.locator(".num-row").all()}
        values = {k: v for k, (v, _) in rows.items()}
        self.assertEqual({k: values[k] for k in ("Dependencies per formula you installed", "Direct dependency links",
                                                 "Need nothing, needed by nothing", "Versioned (name@version)",
                                                 "Different licenses", "Formulae without a license")},
                         {"Dependencies per formula you installed": "0.4 on average", "Direct dependency links": "2",
                          "Need nothing, needed by nothing": "5", "Versioned (name@version)": "2",
                          "Different licenses": "1", "Formulae without a license": "0"})
        self.assertTrue(rows["Versioned (name@version)"][1].endswith("\n\nphp@8.0, ssl@1"))

    def test_distribution_bars_are_groups_that_name_their_packages(self):
        page = self.browser.new_page(timezone_id="UTC")
        self.addCleanup(page.close)
        page.goto(self.url)
        page.wait_for_selector(".ov .att")
        bars = lambda title: [(r.locator(".bar-label").text_content(), r.locator(".bar-value").text_content(),  # noqa: E731
                               r.get_attribute("title")) for r in page.locator(f'.card:has(> h3:text-is("{title}")) .bar-row').all()]
        days = bars("Installs by weekday")
        self.assertEqual([(d, n) for d, n, _ in days], [("Monday", "0 pkg"), ("Tuesday", "8 pkg"), ("Wednesday", "0 pkg"),
                                                        ("Thursday", "0 pkg"), ("Friday", "0 pkg"), ("Saturday", "0 pkg"),
                                                        ("Sunday", "0 pkg")])
        self.assertEqual(days[1][2], "Tuesday: 8 pkg\ndrv, held, jq, odd, php@8.0, py, ssl@1, ytdl")
        self.assertEqual([(r, n) for r, n, _ in bars("Dependencies each package brings")][:3],
                         [("none", "6 pkg"), ("1", "1 pkg"), ("2–5", "1 pkg")])
        self.assertEqual(page.locator("h3.ov-section + .cards .card").count(), 6)
        self.assertGreater(page.locator("h3.ov-section + .cards .bar-row").count(), 0)
        self.assertEqual(page.locator("h3.ov-section + .cards .bar-row[data-goto]").count(), 0)

    def test_overview_helpers(self):
        ev = self.page.evaluate
        self.assertEqual(ev("""() => {
            const score = (p) => ({ a: 2, b: 2, c: 1 })[p.name] ?? null;
            const ps = ["b", "a", "c", "d"].map((name) => ({ name }));
            return [best(ps, score), best(ps, score, true)].map((r) => [r.top.map((p) => p.name), r.value]);
        }"""), [[["a", "b"], 2], [["c"], 1]])
        self.assertEqual(ev("ranges([1023, 1024, 10239, 10240, 2000000].map((kb) => ({ kb })), (p) => p.kb, SIZE_RANGES)"
                            ".map((r) => [r.label, r.value])"),
                         [["under 1 MB", 1], ["1–10 MB", 2], ["10–100 MB", 1], ["100 MB–1 GB", 0], ["1 GB or more", 1]])
        self.assertEqual(ev("topGroups([['x', [1, 2, 3]], ['y', [1, 2]], ['z', [1]], ['w', [1]]], 2).map((r) => [r.label, r.value])"),
                         [["x", 3], ["y", 2], ["2 others", 2]])
        self.assertEqual(ev("groupsBy(['b', 'a', 'b', 'c', 'a'].map((n) => ({ n })), (p) => p.n).map(([k, ps]) => [k, ps.length])"),
                         [["a", 2], ["b", 2], ["c", 1]])
        self.assertEqual(ev("['2026.4.1', '2026-10-20', '20240101', '577.0.0', '20.11.1', 'r3108']"
                            ".map((version) => majorVersion({ version }))"), [None, None, None, 577, 20, None])
        self.assertEqual(ev("""() => {
            S.pkgs = new Map([["a", ["b"]], ["b", ["c"]], ["c", []], ["d", ["b", "c"]], ["e", ["f"]], ["f", ["e"]]]
              .map(([id, deps]) => [id, { deps }]));
            const reach = dependencyReach();
            return ["a", "b", "c", "d"].map((id) => [[...reach.get(id).all].sort(), reach.get(id).chain])
              .concat([reach.has("e") && reach.has("f")]);
        }"""), [[["b", "c"], ["a", "b", "c"]], [["c"], ["b", "c"]], [[], ["c"]], [["b", "c"], ["d", "b", "c"]], True])

    def test_every_overview_figure_matches_a_value_worked_out_by_hand(self):
        page = self.browser.new_page(timezone_id="UTC")
        self.addCleanup(page.close)
        page.goto(self.url)
        page.wait_for_selector('.tile:has-text("64 KB")')
        page.evaluate(r"""() => {
            const now = 1700864000, day = 86400;
            Date.now = () => now * 1000;
            for (const name of ["gawk", "tree", "wget"]) {
              S.pkgs.set(`f:${name}`, { ...S.pkgs.get("f:held"), id: `f:${name}`, name, requested: false });
            }
            S.snap.sizes = {};
            for (const [id, kb, version, days, fields] of [
              ["c:drv", 409600, "126", 1 / 24, {}],
              ["f:php@8.0", 204800, "8.0.30", 60, { license: "PHP-3.01", keg_only: true }],
              ["f:py", 102400, "3.12.4", 60, { license: "Python-2.0" }],
              ["f:ssl@1", 51200, "r3108", 60, { license: "OpenSSL", keg_only: true }],
              ["f:ytdl", 40960, "2026.10.31", 45, { license: "Unlicense" }],
              ["f:held", 30720, "2.3", 400, {}],
              ["f:odd", 20480, "0.9.1", 60, { license: null, tap: "me/tools", caveats: "one line" }],
              ["f:gawk", 15360, "5.3.1", 60, { license: "GPL-3.0-or-later", tap: "me/tools" }],
              ["f:tree", 10240, "2.2.1", 60, { license: "GPL-2.0-or-later" }],
              ["f:wget", 8192, "1.25.0", 60, { license: "GPL-3.0-only" }],
              ["f:jq", 6144, "1.7.1", 5, { caveats: "a\nb\nc" }],
            ]) {
              Object.assign(S.pkgs.get(id), { version, installed_time: now - days * day, ...fields });
              S.snap.sizes[id] = kb;
            }
            S.snap.model.taps = [{ name: "me/tools", official: false, installed: ["f:gawk", "f:odd"] },
                                 { name: "homebrew/core", official: true, installed: ["f:jq"] }];
            renderView();
        }""")
        numbers = lambda: {r.locator("span").first.text_content(): r.locator(".num-value").text_content()  # noqa: E731
                           for r in page.locator(".num-row").all()}
        self.assertEqual(self.records(page), {
            "Largest": ("drv", "400 MB", ""),
            "Smallest": ("jq", "6 MB", ""),
            "Heaviest with its dependencies": ("ytdl", "190 MB", "40 MB itself and 2 dependencies"),
            "Most dependencies": ("ytdl", "2 packages", "1 direct"),
            "Longest dependency chain": ("ytdl", "2 levels", "ytdl → py → ssl@1"),
            "Longest without an upgrade": ("held", "2022-10-20", "400 days ago"),
            "Latest install or upgrade": ("drv", "2023-11-24", "1 h ago"),
            "Shortest name": ("jq", "2 characters", "also py"),
            "Longest name": ("php@8.0", "7 characters", ""),
            "Highest version": ("drv", "126", ""),
            "Longest caveats": ("jq", "3 lines", ""),
        })
        self.assertEqual(numbers(), {
            "Dependencies per formula you installed": "0.4 on average",
            "Direct dependency links": "2",
            "Median package size": "30 MB",
            "Average package size": "80 MB",
            "Share of disk in the ten largest": "99%",
            "Disk taken by dependencies": "183 MB (21%)",
            "Need nothing, needed by nothing": "8",
            "Keg-only formulae": "2",
            "Versioned (name@version)": "2",
            "With caveats": "2",
            "Different licenses": "8",
            "Formulae without a license": "1",
            "Packages from third-party taps": "2",
            "Installed in the last 30 days": "2",
            "Most common first letter": "p (2)",
        })
        charts = {c.locator("h3").text_content(): [(r.locator(".bar-label").text_content(),
                                                    r.locator(".bar-value").text_content()) for r in c.locator(".bar-row").all()]
                  for c in page.locator("h3.ov-section + .cards .card").all()}
        pkg = lambda *rows: [(label, f"{n} pkg") for label, n in rows]  # noqa: E731
        self.assertEqual(charts, {
            "Size on disk": pkg(("under 1 MB", 0), ("1–10 MB", 2), ("10–100 MB", 6), ("100 MB–1 GB", 3), ("1 GB or more", 0)),
            "Dependencies each package brings": pkg(("none", 9), ("1", 1), ("2–5", 1), ("6–10", 0), ("11–20", 0),
                                                    ("21–50", 0), ("more than 50", 0)),
            "Licenses of formulae": pkg(("MIT", 2), ("GPL-2.0-or-later", 1), ("GPL-3.0-only", 1), ("GPL-3.0-or-later", 1),
                                        ("none", 1), ("OpenSSL", 1), ("PHP-3.01", 1), ("Python-2.0", 1), ("1 other", 1)),
            "Packages per tap": pkg(("homebrew/core", 8), ("me/tools", 2), ("homebrew/cask", 1)),
            "How versions are numbered": pkg(("x.y.z", 6), ("0.x", 1), ("date", 1), ("one number", 1), ("other", 1),
                                             ("x.y", 1)),
            "Installs by weekday": pkg(("Monday", 7), ("Tuesday", 1), ("Wednesday", 0), ("Thursday", 1), ("Friday", 1),
                                       ("Saturday", 0), ("Sunday", 1)),
        })
        page.evaluate('delete S.snap.sizes["c:drv"]; renderView()')
        self.assertEqual({k: v for k, v in numbers().items() if k in ("Median package size", "Average package size")},
                         {"Median package size": "25 MB", "Average package size": "48 MB"})

    def test_detail_lists_indirect_dependents(self):
        self.open("deps", "f:ssl@1")
        self.assertIn("Also needed indirectly by:\nytdl", self.page.inner_text("#detail"))

    def test_blocked_uninstall_offers_both_ways_and_no_plain_run(self):
        self.uninstall_ssl()
        card = self.page.inner_text("#modal-card")
        self.assertIn("2 installed package(s) need this", card)
        self.assertEqual(self.page.locator("#m-ok").count(), 0)
        self.assertEqual(self.page.locator("#force-uninstall").count(), 1)

    def test_uninstall_them_too_removes_the_dependents_with_it(self):
        self.uninstall_ssl()
        self.page.click("#add-blockers")
        self.page.wait_for_selector("#m-ok")
        self.page.click("#m-ok")
        wait_for(lambda: ["brew", "uninstall", "--formula", "ssl@1", "py", "ytdl"] in self.brew.popened)

    def test_uninstall_anyway_ignores_dependencies_and_names_what_breaks(self):
        self.uninstall_ssl()
        self.page.click("#force-uninstall")
        self.page.wait_for_selector("#m-ok")
        card = self.page.inner_text("#modal-card")
        self.assertIn("brew uninstall --ignore-dependencies --formula ssl@1", card)
        self.assertIn("These stay installed but may stop working:\npy\nytdl", card)
        self.page.click("#m-ok")
        wait_for(lambda: ["brew", "uninstall", "--ignore-dependencies", "--formula", "ssl@1"] in self.brew.popened)

    def test_install_draft_survives_a_rerender(self):
        self.page.click('nav button[data-tab="install"]')
        self.page.fill("#search-q", "half typed")
        self.page.evaluate("renderAll()")
        self.assertEqual(self.page.input_value("#search-q"), "half typed")

    def tab(self, name):
        self.page.click(f'nav button[data-tab="{name}"]')

    def open_plugin(self):
        self.tab(SECURE_IT)
        self.page.wait_for_selector("#plist .pgroup")

    def unfold(self, *labels):
        """Opens the topic, then the row, named by each label in turn, as a click on its summary does."""
        box = self.page.locator("#plist")
        for label in labels:
            box = box.locator(f'details:has(> summary:has-text("{label}"))').first
            if not box.evaluate("(d) => d.open"):
                box.locator("> summary").click()
        return box

    def topics(self):
        return [(g.locator("> summary .sev").inner_text().lower(), g.locator("> summary b").inner_text(), g.locator("> summary .muted").first.inner_text())
                for g in self.page.locator("#plist .pgroup").all()]

    def test_a_plugin_tab_groups_open_findings_by_category_and_topic_worst_first_and_escapes_them(self):
        self.open_plugin()
        self.assertEqual(self.page.locator("#plist > h3").all_inner_texts(), ["Network 2 findings", "Packages 1 finding", "Persistence 1 finding"])
        self.assertEqual(self.topics(), [("high", "Open port", "2 findings · 1 high · 1 medium"), ("medium", "Third-party taps", "1 finding · 1 medium"),
                                         ("low", "PATH shadowing", "1 finding · 1 low")])
        self.assertEqual(self.page.inner_text("#pshown"), "4 findings shown")
        self.assertIn(XSS, self.unfold("Third-party taps", "onerror").inner_text())
        self.assertEqual(self.page.locator("#content img").count(), 0)
        self.assertEqual(self.page.locator("#content [onmouseover]").count(), 0)
        self.assertIsNone(self.page.evaluate("window.pwned"))
        self.assertEqual(self.page.locator(f'nav button[data-tab="{SECURE_IT}"] .count').inner_text(), "4")
        self.assertEqual(self.page.inner_text("#intro h2"), "Security")
        self.assertIn("Scan started 2026-11-03 08:00 · host: mac · user: me", self.page.inner_text("#content"))

    def test_the_findings_of_one_fingerprint_are_one_card_that_counts_each_of_them(self):
        self.open_plugin()
        topic = self.unfold("Open port")
        self.assertEqual(topic.locator(".prow > summary").all_inner_texts(), ["HIGH\njq\n2 findings"])
        self.assertEqual(self.page.inner_text("#plist .pgroup-body > p"), "Advice: Stop the service.")
        card = self.unfold("Open port", "jq").locator(".alert")
        self.assertEqual(card.count(), 1)
        self.assertEqual(card.locator("> div").first.inner_text(), "HIGH jq listens on *:8080 0123456789")
        self.assertEqual(card.locator("> div.muted").inner_text(), "Same fingerprint: jq listens on 127.0.0.1:9000")
        self.assertEqual(card.locator(".kv").inner_text().split("\n"), ["Detail", "pid 77", "Records", "CVE-2026-1"])
        self.assertEqual(card.locator('.kv a[href="https://osv.dev/vulnerability/CVE-2026-1"]').count(), 1)

    def test_a_finding_offers_its_fix_and_accept_commands_to_copy_never_to_run(self):
        self.open_plugin()
        jq = self.unfold("Open port", "jq").locator(".alert")
        self.assertEqual([p.inner_text() for p in jq.locator("pre").all()], ["brew services stop jq", ACCEPT])
        self.assertEqual(self.unfold("Third-party taps", "onerror").locator(".alert pre").count(), 1)
        self.assertEqual(self.page.locator("#content [data-act], #content [data-svc], #content [data-install]").count(), 0)
        before = list(self.brew.popened)
        copies = self.page.locator("#content [data-copytext]:visible")
        self.assertEqual(copies.count(), 3)
        for b in copies.all():
            b.click()
        self.assertEqual(self.brew.popened, before)

    def test_a_finding_links_to_its_package_and_the_package_shows_it(self):
        self.open_plugin()
        self.unfold("Open port", "jq").locator('[data-goto="f:jq"]').click()
        self.page.wait_for_selector("#detail h2.detail-title")
        section = self.page.locator("#detail .detail-section:has(h4:text-is('brewdash security plugin findings'))")
        self.assertEqual(section.locator(".alert").all_inner_texts(), ["HIGH jq listens on *:8080", "MEDIUM jq listens on 127.0.0.1:9000"])
        self.assertIn("TCP *:8080", self.page.inner_text("#detail"))
        section.locator('button:text-is("Open brewdash security plugin")').click()
        self.assertEqual(self.page.inner_text("#intro h2"), "Security")
        self.assertIn("Findings from brewdash security plugin, a separate tool.", self.page.inner_text("#intro"))
        self.assertEqual(self.page.locator(f'#tabs button[data-tab="{SECURE_IT}"]').evaluate("b => b.firstChild.textContent"), "Security")
        self.unfold("PATH shadowing", "~/bin shadows").locator('[data-tab="shell"]').click()
        self.assertEqual(self.page.inner_text("#intro h2"), "Shell")
        self.open_plugin()
        self.unfold("Third-party taps", "onerror").locator('button:text-is("Open me/tap in Taps")').click()
        self.assertEqual(self.page.inner_text("#intro h2"), "Taps")
        badge = self.page.locator('#content tr:has-text("me/tap") .badge:text-is("Security")')
        self.assertEqual((badge.count(), badge.get_attribute("title")), (1, "brewdash security plugin flags this tap"))
        badge.click()
        self.assertEqual(self.page.inner_text("#intro h2"), "Security")

    def choose(self, field, value):
        self.page.select_option(f'#content select[data-pf="{field}"]', value)

    def test_the_filters_narrow_the_findings_and_the_toggles_show_accepted_and_inventory(self):
        self.open_plugin()
        for change, shown, topics in (
                (lambda: self.choose("severity", "low"), 1, ["PATH shadowing"]),
                (lambda: self.page.check('[data-pf="accepted"]'), 2, ["Open port", "PATH shadowing"]),
                (lambda: self.choose("severity", ""), 5, ["Open port", "Third-party taps", "PATH shadowing"]),
                (lambda: self.page.check('[data-pf="info"]'), 6, ["Open port", "Third-party taps", "Inventory", "PATH shadowing"]),
                (lambda: self.choose("category", "packages"), 2, ["Third-party taps", "Inventory"]),
                (lambda: self.choose("category", ""), 6, ["Open port", "Third-party taps", "Inventory", "PATH shadowing"]),
                (lambda: self.choose("effort", "small"), 2, ["Third-party taps", "PATH shadowing"]),
                (lambda: self.choose("effort", ""), 6, ["Open port", "Third-party taps", "Inventory", "PATH shadowing"]),
                (lambda: self.page.uncheck('[data-pf="info"]'), 5, ["Open port", "Third-party taps", "PATH shadowing"]),
                (lambda: self.choose("severity", "info"), 1, ["Inventory"])):
            change()
            with self.subTest(shown=shown, topics=topics):
                self.assertEqual(self.page.inner_text("#pshown"), f"{shown} finding{'s' if shown > 1 else ''} shown")
                self.assertEqual(self.page.locator("#plist .pgroup > summary b").all_inner_texts(), topics)
        self.choose("severity", "")
        accepted = self.unfold("Open port", "cupsd").locator(".alert")
        self.assertIn("accepted", accepted.locator("> div").first.inner_text())
        self.assertIn("Accepted\n2026-10-30 12:00 — printer sharing", accepted.locator(".kv").inner_text())
        self.assertEqual(accepted.locator("pre").count(), 0)
        self.assertEqual(self.page.locator('#content select[data-pf="category"] option').all_inner_texts(),
                         ["Every category", "Network", "Packages", "Persistence"])

    def test_the_search_keeps_its_focus_and_opens_what_it_finds(self):
        self.open_plugin()
        self.page.click("#pq")
        self.page.keyboard.type("8080")
        self.assertEqual(self.page.evaluate("[document.activeElement.id, document.activeElement.selectionStart]"), ["pq", 4])
        self.assertEqual(self.page.inner_text("#pshown"), "2 findings shown")
        self.assertTrue(self.page.locator("#plist .prow").first.evaluate("(d) => d.open"))
        self.assertTrue(self.page.locator("#plist .alert").first.is_visible())
        self.page.keyboard.type("xyz")
        self.assertEqual(self.page.inner_text("#plist"), "Nothing matches these filters.")
        self.page.fill("#pq", "pid 77")
        self.assertEqual(self.page.inner_text("#pshown"), "2 findings shown")
        self.page.fill("#pq", "")
        self.assertEqual(self.page.inner_text("#pshown"), "4 findings shown")

    def test_a_topic_unfolded_stays_unfolded_when_a_filter_changes(self):
        self.open_plugin()
        self.unfold("Open port", "jq")
        self.page.check('[data-pf="info"]')
        self.assertTrue(self.unfold("Open port").evaluate("(d) => d.open"))
        self.assertTrue(self.page.locator('#plist .prow:has(> summary:has-text("jq"))').evaluate("(d) => d.open"))
        self.page.locator('#plist .pgroup:has(> summary:has-text("Open port")) > summary').click()
        self.page.uncheck('[data-pf="info"]')
        self.assertFalse(self.page.locator('#plist .pgroup:has(> summary:has-text("Open port"))').evaluate("(d) => d.open"))

    def test_topics_of_one_severity_come_least_effort_first_then_most_open_then_by_label(self):
        self.open_plugin()
        shadow = "reports.get('secure-it').report.topics.find((tp) => tp.id === 'persistence.path_shadow')"
        self.page.evaluate(f"""reports.get('secure-it').report.findings.find((f) => f.topic === 'persistence.path_shadow').severity = 'medium';
                               {shadow}.effort = 'large'; renderPlugin()""")
        self.assertEqual(self.page.locator("#plist .pgroup > summary b").all_inner_texts(), ["Open port", "Third-party taps", "PATH shadowing"])
        self.page.evaluate(f"{shadow}.effort = 'small'; renderPlugin()")
        self.assertEqual(self.page.locator("#plist .pgroup > summary b").all_inner_texts(), ["Open port", "PATH shadowing", "Third-party taps"])
        self.page.evaluate("""const r = reports.get('secure-it').report, tap = r.findings.find((f) => f.topic === 'packages.brew_taps');
            r.findings.push({ ...tap, fingerprint: 'abababab', title: 'you/tap', row: { key: 'subject:you/tap', label: 'you/tap' } }); renderPlugin()""")
        self.assertEqual(self.page.locator("#plist .pgroup > summary b").all_inner_texts(), ["Open port", "Third-party taps", "PATH shadowing"])

    def test_a_topic_names_its_amount_of_work_and_its_code(self):
        self.open_plugin()
        self.page.check('[data-pf="info"]')
        summaries = {g.locator("b").inner_text(): g for g in self.page.locator("#plist .pgroup > summary").all()}
        self.assertEqual({label: (s.locator(".tag").all_inner_texts(), s.locator(".mono").all_inner_texts()) for label, s in summaries.items()},
                         {"Open port": (["work: medium"], ["exposure.listening · open_port"]),
                          "Third-party taps": (["work: small"], ["packages.brew_taps"]),
                          "Inventory": ([], ["packages.inventory"]), "PATH shadowing": (["work: small"], ["persistence.path_shadow"])})
        self.page.select_option("#lang", "tr")
        self.assertEqual(self.page.locator('#plist .pgroup > summary:has-text("persistence.path_shadow") .tag').inner_text(), "iş: küçük")
        self.page.select_option("#lang", "en")

    def test_rows_and_their_fingerprints_come_worst_first_then_most_open_then_by_name(self):
        self.open_plugin()
        self.page.evaluate("""const r = reports.get('secure-it').report;
            const add = (fingerprint, severity, title, row) => r.findings.push({ ...r.findings[0], fingerprint, severity, title,
                row: { key: 'k:' + row, label: row }, links: [], link: null });
            add('m1', 'medium', 'mmm one', 'mmm'); add('a1', 'medium', 'aaa one', 'aaa');
            add('z2', 'low', 'zzz b', 'zzz'); add('z1', 'medium', 'zzz c', 'zzz'); add('z3', 'medium', 'zzz a', 'zzz'); renderPlugin()""")
        self.assertEqual(self.unfold("Open port").locator(".prow > summary").all_inner_texts(),
                         ["HIGH\njq\n2 findings", "MEDIUM\nzzz\n3 findings", "MEDIUM\naaa one\n1 finding", "MEDIUM\nmmm one\n1 finding"])
        self.assertEqual(self.unfold("Open port", "zzz").locator(".alert > div:first-child").all_inner_texts(),
                         ["MEDIUM zzz a z3", "MEDIUM zzz c z1", "LOW zzz b z2"])

    def test_advice_the_findings_of_a_topic_do_not_share_goes_on_each_card(self):
        self.open_plugin()
        self.page.evaluate("""reports.get('secure-it').report.findings.find((f) => f.fingerprint === 'cccccccccc').remediation = 'Close the printer port.';
            renderPlugin()""")
        self.assertEqual(self.unfold("Open port").locator(".pgroup-body > p").count(), 0)
        self.assertEqual(self.unfold("Open port", "jq").locator(".alert .kv").inner_text().split("\n"),
                         ["Detail", "pid 77", "Advice", "Stop the service.", "Records", "CVE-2026-1"])

    def test_the_tab_says_when_the_report_cannot_be_read_has_no_runs_or_no_run_changed_anything(self):
        self.open_plugin()
        failed = lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "disk on fire"}))
        self.page.route("**/api/plugin?*", failed)
        try:
            self.page.evaluate("reports.clear(); renderPlugin()")
            self.assertEqual(self.page.inner_text("#content .warn"), "brewdash could not read the report: disk on fire")
        finally:
            self.page.unroute("**/api/plugin?*")
        self.page.evaluate("reports.clear(); renderPlugin()")
        self.page.click('#content [data-pview="history"]')
        self.page.evaluate("const r = reports.get('secure-it').report; r.history.forEach((run) => { run.alerts = r.history[0].alerts; }); renderPlugin()")
        self.assertEqual((self.page.locator("#pbody .prun").count(), self.page.inner_text("#pbody > p.muted")),
                         (0, "No alert came or went between the kept runs."))
        self.page.evaluate("reports.get('secure-it').report.history = []; renderPlugin()")
        self.assertEqual(self.page.inner_text("#pbody"), "The report has no runs yet.")

    def test_a_new_brew_state_fetches_the_report_again_for_its_links(self):
        self.open_plugin()
        self.page.evaluate("""window.fetched = 0; const was = window.fetch;
            window.fetch = (url, ...rest) => { if (String(url).includes('/api/plugin?')) fetched++; return was(url, ...rest); }""")
        self.page.evaluate("renderPlugin()")
        self.assertEqual(self.page.evaluate("fetched"), 0)
        self.page.evaluate("S.version += 1; renderPlugin()")
        self.page.wait_for_function("window.fetched === 1", timeout=5000)
        self.page.wait_for_selector("#plist .pgroup")

    def test_a_topic_explains_itself_and_names_its_sources(self):
        self.open_plugin()
        why = self.unfold("Open port", "Why?")
        self.assertEqual(why.locator("p").all_inner_texts(), ["What: A port other machines can reach.", "Why it matters: Anyone on the network can talk to it.",
                                                              "How attackers use it: Attackers scan for it."])
        self.assertEqual(why.locator("li").all_inner_texts(), ["MITRE ATT&CK T1046 (framework, checked 2026-10-01)",
                                                               "Apple Platform Security (vendor, no link, not checked)"])
        self.assertEqual(why.locator("a").get_attribute("href"), "https://attack.mitre.org/techniques/T1046/")
        self.assertEqual(self.unfold("Third-party taps").locator(".pwhy").count(), 0)

    def test_the_plugins_texts_follow_the_chosen_language_and_fall_back_to_english(self):
        self.open_plugin()
        self.unfold("Open port", "jq")
        self.page.select_option("#lang", "tr")
        self.assertEqual(self.page.locator("#plist .pgroup > summary b").first.inner_text(), "Açık port")
        card = self.page.locator("#plist .alert").first
        self.assertIn("jq *:8080 portunda dinliyor", card.inner_text())
        self.assertEqual(card.locator("pre").all_inner_texts()[1], 'sudo secure-it accept 0123456789 --note "neden"')
        self.assertEqual(self.page.locator("#plist > h3").first.inner_text(), "Ağ 2 bulgu")
        self.assertEqual(self.page.locator("#content [data-pview]").all_inner_texts(), ["Uyarılar", "Geçmiş", "Check'ler"])
        self.page.select_option("#lang", "es")
        self.assertEqual(self.page.locator("#plist .pgroup > summary b").first.inner_text(), "Open port")
        self.assertEqual(self.page.locator("#plist > h3").first.inner_text(), "Network 2 hallazgos")
        self.page.select_option("#lang", "en")

    def view(self, name):
        self.open_plugin()
        self.page.click(f'#content [data-pview="{name}"]')

    def test_history_shows_the_open_alerts_of_each_run_and_what_came_and_went(self):
        self.view("history")
        rows = [r.locator("td").all_inner_texts() for r in self.page.locator("#pbody table tbody tr").all()]
        self.assertEqual(rows, [["2026-11-03 08:00", "0", "1", "2", "1", "4", "2", "0"],
                                ["2026-11-02 08:00", "0", "1", "1", "1", "3", "1", "1"],
                                ["2026-11-01 08:00", "0", "1", "1", "0", "2", "–", "–"]])
        self.assertEqual([m.get_attribute("title") for m in self.page.locator("#pbody .month").all()],
                         ["2026-11-01 08:00: 0 critical, 1 high, 1 medium, 0 low", "2026-11-02 08:00: 0 critical, 1 high, 1 medium, 1 low",
                          "2026-11-03 08:00: 0 critical, 1 high, 2 medium, 1 low"])
        self.assertEqual([s.evaluate("(e) => e.style.height") for s in self.page.locator("#pbody .run-stack").all()], ["50%", "75%", "100%"])
        changes = [[line for line in c.inner_text().split("\n") if line] for c in self.page.locator("#pbody .prun").all()]
        self.assertEqual(changes, [["2026-11-03 08:00", "New (2):", f"MEDIUM {XSS} eeeeeeeeee", "LOW cupsd listens on *:631 cccccccccc accepted"],
                                   ["2026-11-02 08:00", "New (1):", "LOW ~/bin shadows 1 system command ffffffffff",
                                    "Gone (1):", "MEDIUM telnetd listens on *:23 7e7e7e7e7e"]])

    def test_checks_lists_each_check_that_did_not_run_with_its_counts_and_last_reason(self):
        self.view("checks")
        self.assertEqual([r.locator("td").all_inner_texts() for r in self.page.locator("#pbody table tbody tr").all()],
                         [["SIP is on\nsystem.sip", "1", "1", "2026-11-02 08:00", "Full Disk Access missing"]])
        self.assertEqual(self.page.inner_text("#pbody > p"), "Checks that failed or were skipped in the 3 kept runs.")
        self.page.evaluate("reports.get('secure-it').report.history.forEach((r) => { r.checks = []; }); renderPlugin()")
        self.assertEqual(self.page.inner_text("#pbody"), "✔ Every check ran in each of the 3 kept runs.")

    def set_plugin(self, js):
        self.page.evaluate(f"S.snap.plugins = S.snap.plugins.map((p) => ({{...p, {js}}})); renderAll()")

    def test_a_plugin_that_is_not_installed_says_what_it_is_and_how_to_install_it(self):
        self.set_plugin("installed: false, scan: false, found: false, alerts: []")
        self.tab(SECURE_IT)
        content = self.page.inner_text("#content")
        self.assertIn("brewdash security plugin is not installed", content)
        self.assertIn("A separate scanner for macOS security settings", content)
        self.assertEqual(self.page.inner_text("#content pre"),
                         "git clone https://github.com/lemiorhan/brewdash-plugin-security.git\n"
                         "cd brewdash-plugin-security && sudo ./install.sh")
        self.assertEqual(self.page.get_attribute("#content a", "href"), "https://github.com/lemiorhan/brewdash-plugin-security")
        self.assertIn(str(self.report), content)
        self.assertEqual(self.page.locator("#content [data-plugin-scan]").count(), 0)
        self.assertEqual(self.page.locator(f'nav button[data-tab="{SECURE_IT}"] .count').count(), 0)
        self.tab("overview")
        self.assertNotIn(SECURE_IT, [go for _, go, _ in self.attention()])

    def test_an_installed_plugin_without_a_report_offers_a_scan(self):
        self.set_plugin("found: false, alerts: []")
        self.tab(SECURE_IT)
        self.assertIn(f"brewdash security plugin has not written {self.report} yet.", self.page.inner_text("#content"))
        self.assertEqual(self.page.inner_text("#content [data-plugin-scan]"), "Scan now")
        self.assertEqual(self.page.locator("#content .ok").count(), 0)

    def test_a_report_with_nothing_open_says_so(self):
        self.open_plugin()
        self.page.evaluate("const r = reports.get('secure-it').report; r.findings = r.findings.filter((f) => f.accepted || f.severity === 'info'); renderPlugin()")
        self.assertEqual(self.page.inner_text("#plist"), "✔ brewdash security plugin reports nothing open.")
        self.page.check('[data-pf="accepted"]')
        self.assertEqual(self.page.locator("#plist .ok").count(), 1)
        self.assertEqual(self.page.locator("#plist .pgroup > summary b").all_inner_texts(), ["Open port"])

    def test_installing_the_plugin_while_the_page_is_open_offers_a_scan(self):
        # With no report the plugin's seq stays put, so only the install itself can tell the page.
        self.report.unlink()
        self.exe.unlink()
        try:
            self.tab(SECURE_IT)
            self.page.wait_for_selector('#content h3:text-is("brewdash security plugin is not installed")')
            self.exe.write_text("")
            self.page.wait_for_selector('#content [data-plugin-scan="secure-it"]')
        finally:
            self.exe.write_text("")
            self.cls_report(REPORT)
        self.page.wait_for_selector("#plist .pgroup")

    def test_a_plugin_someone_other_than_root_can_change_says_why_scan_now_is_off(self):
        self.tab(SECURE_IT)
        self.page.wait_for_selector('#content [data-plugin-scan="secure-it"]')
        self.unprotected[str(self.exe)] = "/usr/local/bin"
        try:
            self.page.wait_for_selector('#content .warn:has-text("Scan now is off: brewdash security plugin runs as root, '
                                        'and someone other than root can change /usr/local/bin.")')
            self.assertEqual(self.page.locator("#content [data-plugin-scan]").count(), 0)
        finally:
            self.unprotected.clear()
        self.page.wait_for_selector('#content [data-plugin-scan="secure-it"]')

    def test_where_the_plugin_cannot_be_installed_safely_the_page_says_so_instead_of_how_to_install_it(self):
        self.report.unlink()
        self.exe.unlink()
        self.unprotected[str(self.exe)] = "/usr/local/bin"
        try:
            self.tab(SECURE_IT)
            self.page.wait_for_selector('#content .warn:has-text("brewdash security plugin cannot be installed safely on this Mac. '
                                        'It runs as root, and its files would go under /usr/local/bin, '
                                        'which someone other than root can change. '
                                        'On an Intel Mac, Homebrew gives /usr/local/bin to your user.")')
            self.assertNotIn("git clone", self.page.inner_text("#content"))
            self.unprotected[str(self.exe)] = "/Library/Application Support"
            self.page.wait_for_selector('#content .warn:has-text("would go under /Library/Application Support")')
            self.assertNotIn("Intel", self.page.inner_text("#content"))
            self.unprotected.clear()
            self.page.wait_for_selector('#content pre:has-text("git clone")')
        finally:
            self.unprotected.clear()
            self.exe.write_text("")
            self.cls_report(REPORT)
        self.page.wait_for_selector("#plist .pgroup")

    def test_scan_now_runs_the_catalogs_command_after_a_confirm(self):
        before = len(self.brew.popened)
        self.tab(SECURE_IT)
        self.page.click(f'#content [data-plugin-scan="secure-it"]')
        self.page.wait_for_selector("#m-ok")
        self.assertIn(shlex.join(SCAN), self.page.inner_text("#modal"))
        self.assertNotIn(SCAN, self.brew.popened[before:])
        self.page.click("#m-ok")
        wait_for(lambda: SCAN in self.brew.popened[before:])

    def test_a_scan_whose_password_dialog_was_cancelled_gives_a_command_that_runs_in_terminal(self):
        terminal = "sudo /usr/local/bin/secure-it scan --exit-zero"
        self.brew.scripts[("-A",)] = "echo 'sudo: a password is required; the brewdash password dialog was cancelled'; exit 1"
        try:
            last = self.page.evaluate("Math.max(0, ...S.jobs.map((j) => j.id))")
            self.tab(SECURE_IT)
            self.page.click('#content [data-plugin-scan="secure-it"]')
            self.page.click("#m-ok")
            self.page.wait_for_function(f"S.jobs.some((j) => j.id > {last} && j.status === 'failed')")
            job = self.page.evaluate(f"S.jobs.find((j) => j.id > {last}).id")
            hint = self.page.locator(f'.job:has([data-job="{job}"]) .terminal-hint')
            self.assertEqual(hint.inner_text().splitlines()[:2], ["This needs your sudo password. Run it in Terminal:", terminal])
            self.page.evaluate("() => { window.copied = []; navigator.clipboard.writeText = async (s) => { window.copied.push(s); }; }")
            hint.locator("[data-copy]").click()
            self.assertEqual(self.page.evaluate("window.copied"), [terminal])
        finally:
            del self.brew.scripts[("-A",)]

    UPDATE = "{ cd brewdash-plugin-security 2>/dev/null || cd secure-it; } && git pull && sudo ./install.sh"

    def test_a_report_from_an_older_plugin_says_to_update_the_plugin(self):
        try:
            self.cls_report({**REPORT, "schema": 2})
            self.page.wait_for_selector('.att:has-text("brewdash security plugin needs an update")')
            self.tab(SECURE_IT)
            warn = self.page.locator("#content .warn")
            self.assertIn("An older brewdash security plugin is installed", warn.inner_text())
            self.assertEqual(warn.locator("pre").inner_text(), self.UPDATE)
            self.assertEqual(self.page.locator("#content .alert, #content .ok, #content [data-pview]").count(), 0)
        finally:
            self.cls_report(REPORT)
        self.page.wait_for_selector("#plist .pgroup")

    def test_a_report_only_where_an_older_plugin_wrote_says_to_update_the_plugin_and_is_not_read(self):
        self.report.unlink()
        self.legacy_report.parent.mkdir(exist_ok=True)
        self.legacy_report.write_text(json.dumps(REPORT))
        try:
            self.page.wait_for_selector('.att:has-text("brewdash security plugin needs an update")')
            self.tab(SECURE_IT)
            warn = self.page.locator("#content .warn")
            self.assertIn("An older brewdash security plugin is installed", warn.inner_text())
            self.assertEqual(warn.locator("pre").inner_text(), self.UPDATE)
            self.assertIn(f"brewdash security plugin has not written {self.report} yet.", self.page.inner_text("#content"))
            self.assertEqual(self.page.locator("#content .alert, #content [data-pview]").count(), 0)
            self.assertEqual(self.page.locator(f'nav button[data-tab="{SECURE_IT}"] .count').count(), 0)
        finally:
            self.legacy_report.unlink()
            self.cls_report(REPORT)
        self.page.wait_for_selector("#plist .pgroup")

    def test_a_report_from_a_newer_plugin_says_to_update_brewdash(self):
        self.set_plugin("found: true, error: 'it has schema 4; this brewdash reads schema 3', outdated: 'brewdash', alerts: []")
        self.assertIn(("warning", SECURE_IT, "brewdash security plugin report unreadable"), self.attention())
        self.tab(SECURE_IT)
        self.assertEqual(self.page.inner_text("#content .warn"),
                         "brewdash security plugin writes a newer report than this brewdash reads. Update brewdash in Settings.")

    def test_a_new_report_shows_up_without_a_brew_change(self):
        self.open_plugin()
        try:
            self.cls_report({**REPORT, "findings": FINDINGS[:1]})
            self.page.wait_for_function(f"document.querySelector('nav button[data-tab=\"{SECURE_IT}\"] .count').textContent === '1'")
            self.page.wait_for_function("document.querySelector('#pshown')?.textContent === '1 finding shown'")
        finally:
            self.cls_report(REPORT)
        self.page.wait_for_function(f"document.querySelector('nav button[data-tab=\"{SECURE_IT}\"] .count').textContent === '4'")
        self.page.wait_for_function("document.querySelector('#pshown')?.textContent === '4 findings shown'")

    def cls_report(self, data):
        self.report.write_text(json.dumps(data) + " " * len(data["findings"]) + " " * data["schema"])

    def test_a_manifest_dropped_into_the_state_dir_gets_its_own_escaped_tab_and_no_scan(self):
        manifest = self.plugins_dir / "acme.json"
        (self.home / "acme.json").write_text(json.dumps({**REPORT, "findings": FINDINGS[:1]}))
        try:
            manifest.write_text(json.dumps({"schema": 1, "name": XSS, "report": "~/acme.json", "homepage": "https://acme.example/"}))
            self.page.wait_for_selector('nav button[data-tab="plugin-acme"]')
            order = self.page.evaluate("tabs()")
            self.assertEqual(order[order.index(SECURE_IT):order.index("history") + 1], [SECURE_IT, "plugin-acme", "history"])
            self.assertEqual(self.page.inner_text('nav button[data-tab="plugin-acme"]'), XSS + "1")
            self.page.click('nav button[data-tab="plugin-acme"]')
            self.page.wait_for_selector("#plist .pgroup")
            self.assertEqual(self.page.locator("#content .alert").count(), 1)
            self.assertEqual(self.page.inner_text('#content a[href="https://acme.example/"]'), "Homepage")
            self.assertEqual(self.page.locator("#content [data-plugin-scan]").count(), 0)
            self.assertEqual(self.page.locator("nav img, #content img, #intro img").count(), 0)
            self.assertIsNone(self.page.evaluate("window.pwned"))
        finally:
            manifest.unlink()
        self.page.wait_for_function("!document.querySelector('nav button[data-tab=\"plugin-acme\"]')")
        self.page.wait_for_selector('nav button.active[data-tab="overview"]')

    def test_a_manifest_edited_while_the_page_is_open_shows_each_change(self):
        # Steps two to four and the last each change one manifest field and leave the report's seq alone:
        # a new report path starts its own seq at 1, and a manifest with no report has seq 0.
        manifest = self.plugins_dir / "moved.json"
        (self.home / "moved-a.json").write_text(json.dumps({**REPORT, "findings": FINDINGS[:1]}))
        (self.home / "moved-b.json").write_text(json.dumps({**REPORT, "findings": FINDINGS[:2]}))
        tab = 'nav button[data-tab="plugin-moved"]'
        moved = {"schema": 1, "name": "Moved", "report": "~/moved-a.json"}
        steps = ((moved, "label", "Moved1"),
                 ({**moved, "report": "~/moved-b.json"}, "label", "Moved2"),
                 ({**moved, "report": "~/moved-b.json", "name": "Renamed"}, "label", "Renamed2"),
                 ({**moved, "report": "~/moved-b.json", "name": "Renamed", "homepage": "https://moved.example/"},
                  "link", "https://moved.example/"),
                 ({"schema": 9}, "warn", "has schema 9"),
                 ({"schema": 8}, "warn", "has schema 8"))
        try:
            for data, where, text in steps:
                with self.subTest(data=data):
                    manifest.write_text(json.dumps(data))
                    if where == "label":
                        self.page.wait_for_function("([sel, text]) => document.querySelector(sel)?.innerText === text", arg=[tab, text])
                        self.page.click(tab)
                    elif where == "link":
                        self.page.wait_for_selector(f'#content a[href="{text}"]')
                    else:
                        self.page.wait_for_selector(f'#content .warn:has-text("{text}")')
        finally:
            manifest.unlink()

    def test_a_broken_manifest_gets_a_tab_that_says_what_is_wrong(self):
        manifest = self.plugins_dir / "broken.json"
        try:
            manifest.write_text(json.dumps({"schema": 1, "name": "Broken", "report": "relative.json"}))
            self.page.wait_for_selector('nav button[data-tab="plugin-broken"]')
            self.assertIn(("warning", "plugin-broken", "Broken report unreadable"), self.attention())
            self.page.click('nav button[data-tab="plugin-broken"]')
            self.assertEqual(self.page.inner_text("#content .warn"),
                             f"brewdash could not use broken: its manifest {manifest} names no report; "
                             "give an absolute path or one under ~/")
            self.assertEqual(self.page.inner_text("#content").strip(), self.page.inner_text("#content .warn"))
        finally:
            manifest.unlink()

    def test_a_link_to_a_plugin_tab_opens_it_and_one_to_an_unknown_plugin_falls_back(self):
        # A new page each time: on the open page a new hash is read after the first state, not before it.
        for tab, shown in ((SECURE_IT, SECURE_IT), ("plugin-nope", "overview")):
            with self.subTest(tab=tab):
                page = self.browser.new_page()
                try:
                    page.goto(self.url + f"#tab={tab}")
                    page.wait_for_selector(".ov .att, #plist .pgroup")
                    page.wait_for_selector(f'nav button.active[data-tab="{shown}"]')
                    self.assertEqual(tab in page.url, tab == shown)
                finally:
                    page.close()

    def test_services_show_ports_and_mark_the_exposed_ones(self):
        self.tab("services")
        cells = self.page.locator("#content tbody tr .port").all()
        self.assertEqual([(c.inner_text(), "exposed" in c.get_attribute("class")) for c in cells],
                         [("TCP *:8080", True), ("TCP 127.0.0.1:9000", False)])

    def test_apps_show_who_signed_them(self):
        self.tab("apps")
        self.page.wait_for_selector("[data-adopt]")
        sig = {r.locator("td.name").inner_text(): r.locator("td:nth-child(5)").inner_text()
               for r in self.page.locator("#content tbody tr").all()}
        self.assertEqual(sig, {"Bare": "not signed", "Shop": "App Store", "Zed": "Developer ID Zed Industries (ZED1)"})

    def test_brewdash_app_built_here_is_not_flagged_for_its_ad_hoc_signature(self):
        self.tab("apps")
        self.page.wait_for_selector("[data-adopt]")
        self.page.evaluate("""const bare = S.snap.apps.apps.find((a) => a.name === 'Bare');
            S.snap.apps.apps.push({ ...bare, id: 'a:/x/brewdash.app', name: 'brewdash', bundle_id: 'io.github.lemiorhan.brewdash',
                                    signature: { kind: 'adhoc', signer: null } },
                                  { ...bare, id: 'a:/x/Other.app', name: 'Other', bundle_id: 'com.other',
                                    signature: { kind: 'adhoc', signer: null } },
                                  { ...bare, id: 'a:/x/Fork.app', name: 'Fork', bundle_id: 'io.github.lemiorhan.brewdash',
                                    signature: { kind: 'unsigned', signer: null } },
                                  { ...bare, id: 'a:/x/Safari.app', name: 'Safari', bundle_id: 'com.apple.Safari',
                                    signature: { kind: 'apple', signer: 'Software Signing' } }); renderView()""")
        rows = {r.locator("td.name").inner_text(): r for r in self.page.locator("#content tbody tr").all()}
        sig = {n: r.locator("td:nth-child(5)").inner_text() for n, r in rows.items()}
        self.assertEqual((sig["brewdash"], sig["Other"], sig["Fork"]),
                         ("built on this Mac (ad-hoc)", "ad-hoc, no signer", "not signed"))
        self.assertEqual(self.page.evaluate("appsList().filter(weakSignature).map((a) => a.name).sort()"), ["Bare", "Fork", "Other"])
        self.assertEqual(sorted(n for n, r in rows.items() if r.locator("[data-trash]").count()),
                         ["Bare", "Fork", "Other", "Shop", "Zed"])

    def test_every_page_says_what_it_is_and_what_to_do(self):
        tabs = self.page.eval_on_selector_all("nav button[data-tab]", "bs => bs.map(b => b.dataset.tab)")
        self.assertGreater(len(tabs), 1)
        for tab in tabs:
            self.tab(tab)
            if tab == "overview":
                self.assertIn("A local dashboard for Homebrew", self.page.inner_text("#intro .hero"))
                continue
            intro = self.page.inner_text("#intro .intro")
            self.assertEqual(self.page.inner_text("#intro h2"), self.page.inner_text(f'nav button[data-tab="{tab}"]').split("\n")[0].rstrip("0123456789"))
            self.assertIn("What to do:", intro, tab)

    def test_settings_page_names_the_author_and_links_the_repo(self):
        self.tab("settings")
        self.assertIn("Lemi Orhan Ergin", self.page.inner_text("#content"))
        hrefs = self.page.eval_on_selector_all("#content a", "as => as.map(a => a.href)")
        self.assertEqual(hrefs, ["https://github.com/lemiorhan", "https://github.com/lemiorhan/brewdash",
                                 "https://github.com/lemiorhan/brewdash/issues",
                                 "https://github.com/lemiorhan/brewdash/security/advisories/new"])

    def test_settings_page_switches_channel_and_auto_and_updates_on_a_click(self):
        self.page.evaluate("S.settings = undefined")
        self.page.click('nav button[data-tab="settings"]')
        self.page.click('[data-channel="stable"]')
        self.page.wait_for_selector('[data-channel="stable"]:checked')
        cell = lambda name: self.page.locator(f'#content tr:has(td:text-is("{name}")) td').nth(1)
        self.assertEqual((cell("Installed").inner_text(), cell("Last checked").inner_text()), ("v0.9.0", "never"))
        self.assertTrue(self.page.is_disabled('[data-update="now"]'))
        self.page.click('[data-update="check"]')
        self.page.wait_for_selector('#content p:has-text("is available")')
        self.assertEqual(self.page.inner_text('#content p:has-text("is available")'), "v0.10.0 is available.")
        self.assertEqual(self.page.inner_text('[data-update="now"]'), "Update to v0.10.0")
        self.page.click('[data-channel="beta"]')
        self.page.wait_for_selector('[data-update="now"]:disabled')
        self.assertEqual(cell("Last checked").inner_text(), "never")
        self.assertNotIn("There is no stable release yet.", self.page.inner_text("#content"))
        self.page.click('[data-update="check"]')
        self.page.wait_for_selector(f'[data-update="now"]:text-is("Update to {SHA_C[:7]}"):enabled')
        # The box is unchecked as soon as it is clicked; settings.json is written when the POST is answered.
        with self.page.expect_response(lambda r: r.url.endswith("/api/settings") and r.request.method == "POST"):
            self.page.uncheck("[data-auto]")
        self.page.wait_for_selector("[data-auto]:not(:checked)")
        self.assertEqual(json.loads((Path(self.tmp.name) / "state" / "settings.json").read_text()),
                         {"channel": "beta", "auto": False})
        self.spawned.clear()
        self.page.click('[data-update="now"]')
        self.page.wait_for_selector("#content .info")
        self.assertEqual(self.page.inner_text("#content .info"),
                         f"Installing {SHA_C[:7]}. brewdash quits and opens again when it is done.")
        self.assertEqual([a[1]["BREWDASH_ZIP"] for a in self.spawned], [zip_url("beta")])
        self.assertTrue(self.page.is_disabled('[data-update="now"]'))
        self.page.click('nav button[data-tab="history"]')
        with self.page.expect_request("**/api/settings*", timeout=10000):
            pass
        self.installers[-1].returncode = 1
        self.page.wait_for_function("S.settings && !S.settings.started", timeout=10000)
        self.page.click('nav button[data-tab="settings"]')
        self.page.wait_for_selector('#content .warn:has-text("install.sh stopped")')
        self.assertTrue(self.page.is_enabled('[data-update="now"]'))

    def test_a_server_gone_during_an_update_is_not_shown_as_a_failure(self):
        self.page.click('nav button[data-tab="settings"]')
        self.page.wait_for_selector('[data-update="check"]')
        self.page.evaluate("S.settings = { ...S.settings, started: 1, error: null, latest: { sha: 'c'.repeat(40), tag: 'v9.0.0' } }")
        self.page.route("**/api/settings*", lambda route: route.abort())
        try:
            self.page.evaluate("loadSettings()")
        finally:
            self.page.unroute("**/api/settings*")
        self.assertEqual(self.page.locator("#content .warn").count(), 0)
        self.assertIn("Installing v9.0.0", self.page.inner_text("#content .info"))
        self.page.evaluate("clearTimeout(S.settingsPoll); S.settingsPoll = null; S.settings = undefined")

    def test_a_copy_without_a_version_says_how_to_get_updates(self):
        self.page.click('nav button[data-tab="settings"]')
        self.page.wait_for_function("S.settings !== null")
        self.page.evaluate("S.settings = { installed: null }; renderView()")
        self.assertIn("Run install.sh once more to get updates.", self.page.inner_text("#content .info"))
        self.assertEqual(self.page.locator("[data-update], [data-channel], [data-auto]").count(), 0)

    def test_a_channel_without_a_release_says_which_one_is_missing(self):
        self.page.click('nav button[data-tab="settings"]')
        self.page.wait_for_selector('[data-update="check"]')
        for channel, says in (("stable", "There is no stable release yet."), ("beta", "There is no beta build right now.")):
            with self.subTest(channel=channel):
                self.page.evaluate(f"""S.settings = {{ installed: {{ sha: "{SHA_A}", tag: null }}, channel: "{channel}",
                    auto: true, checked: 1, latest: null, relation: null, error: null, started: null }}; renderView()""")
                self.assertEqual(self.page.inner_text("#content p.muted"), says)
        self.page.evaluate("S.settings = undefined")

    def test_settings_page_shows_while_brew_cannot_be_read(self):
        self.page.evaluate("S.snap = { error: 'boom' }; S.tab = 'settings'; renderView()")
        self.assertIn("github.com/lemiorhan/brewdash", self.page.inner_text("#content"))

    def test_theme_button_cycles_and_is_remembered(self):
        theme = lambda: self.page.evaluate("document.documentElement.dataset.theme || 'auto'")
        bg = lambda: self.page.evaluate("getComputedStyle(document.body).backgroundColor")
        self.page.emulate_media(color_scheme="dark")
        self.assertEqual((theme(), bg()), ("auto", "rgb(15, 17, 21)"))
        self.page.click("#btn-theme")
        self.assertEqual((theme(), bg()), ("light", "rgb(245, 246, 248)"))
        self.page.reload()
        self.page.wait_for_selector(".ov .att")
        self.assertEqual((theme(), self.page.inner_text("#btn-theme")), ("light", "☀ light"))
        self.page.click("#btn-theme")
        self.assertEqual(theme(), "dark")
        self.page.emulate_media(color_scheme="light")
        self.assertEqual(bg(), "rgb(15, 17, 21)")
        self.page.click("#btn-theme")
        self.assertEqual((theme(), bg()), ("auto", "rgb(245, 246, 248)"))

    def mac_app_page(self, viewport=None):
        """The page as macos/brewdash.swift's window shows it, with the window's message handler recorded."""
        setup = re.search(r'let pageSetup = """\n(.*?)\n"""', (WEB.parent / "macos" / "brewdash.swift").read_text(), re.S)
        page = self.browser.new_page(color_scheme="dark", viewport=viewport or {"width": 1280, "height": 720})
        self.addCleanup(page.close)
        page.add_init_script("window.sent = []; window.webkit = { messageHandlers: { brewdash: "
                             "{ postMessage: (m) => window.sent.push(m) } } };")
        # Chromium runs this before <html> exists; the window runs pageSetup once it does.
        page.add_init_script("new MutationObserver((_, o) => { if (document.documentElement) { o.disconnect(); "
                             + setup.group(1) + " } }).observe(document, { childList: true });")
        page.goto(self.url)
        page.wait_for_selector(".ov .att")
        return page

    @staticmethod
    def box(page, selector):
        b = page.locator(selector).bounding_box()
        return {**b, "right": b["x"] + b["width"], "bottom": b["y"] + b["height"],
                "middle": (b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)}

    @staticmethod
    def window_asks(page, x, y, double=False, button="left"):
        page.evaluate("window.sent = []")
        (page.mouse.dblclick if double else page.mouse.click)(x, y, button=button)
        return [k for m in page.evaluate("window.sent") for k in ("drag", "zoom") if k in m]

    def test_the_mac_app_window_moves_and_zooms_from_its_title_bar_band_only(self):
        page = self.mac_app_page()
        # Its click would refresh the server every test in this class shares.
        page.evaluate("$('#btn-refresh').onclick = null")
        box = lambda s: self.box(page, s)
        bar, side = box("header")["height"], box("nav")["width"]
        asks = {
            "page title": self.window_asks(page, *box("#page-title")["middle"]),
            "right-click on the page title": self.window_asks(page, *box("#page-title")["middle"], button="right"),
            "band over the sidebar": self.window_asks(page, side / 2, bar / 2),
            "toolbar button": self.window_asks(page, *box("#btn-refresh")["middle"]),
            "app name under the band": self.window_asks(page, side / 2, bar + 18),
            "sidebar tab": self.window_asks(page, *box('nav [data-tab="casks"]')["middle"]),
            "page content": self.window_asks(page, *box("#content")["middle"]),
            "double-click on the page title": self.window_asks(page, *box("#page-title")["middle"], double=True),
        }
        self.assertEqual(asks, {"page title": ["drag"], "right-click on the page title": [],
                                "band over the sidebar": ["drag"], "toolbar button": [],
                                "app name under the band": [], "sidebar tab": [], "page content": [],
                                "double-click on the page title": ["drag", "zoom"]})

    def test_in_the_mac_app_the_band_moves_the_window_over_an_open_dialog_and_leaves_it_open(self):
        page = self.mac_app_page()
        bar = self.box(page, "header")["height"]

        def press(x, y, double=False):
            page.evaluate("alertBox('x')")
            return self.window_asks(page, x, y, double), page.is_visible("#modal")
        self.assertEqual({"band": press(640, bar / 2), "double-click in the band": press(640, bar / 2, double=True),
                          "below the band": press(20, 700)},
                         {"band": (["drag"], True), "double-click in the band": (["drag", "zoom"], True),
                          "below the band": ([], False)})

    def test_in_a_browser_a_click_anywhere_on_the_dialog_shade_closes_it(self):
        for x, y in ((640, 10), (20, 700)):
            self.page.evaluate("alertBox('x')")
            self.page.mouse.click(x, y)
            self.assertFalse(self.page.is_visible("#modal"), (x, y))

    def test_the_mac_app_toolbar_names_the_open_page_in_the_chosen_language(self):
        page = self.mac_app_page()
        title = lambda: page.inner_text("#page-title")
        self.assertEqual(title(), "Overview")
        tabs = page.eval_on_selector_all("nav [data-tab]", "bs => bs.map((b) => [b.dataset.tab, b.firstChild.textContent])")
        self.assertGreater(len(tabs), 10)
        for tab, label in tabs:
            page.click(f'nav [data-tab="{tab}"]')
            self.assertEqual(title(), label, tab)
        page.click('nav [data-tab="casks"]')
        self.assertEqual(title(), "Casks")
        page.select_option("#lang", "tr")
        self.assertEqual(title(), "Cask'ler")

    def test_the_mac_app_puts_the_tabs_in_a_sidebar_and_the_toolbar_on_the_title_bar_band(self):
        page = self.mac_app_page()
        box = lambda s: self.box(page, s)
        page.click('nav [data-tab="casks"]')
        header, nav, brand, theme = box("header"), box("nav"), box(".brand"), box("#btn-theme")
        tabs = [box(f'nav [data-tab="{k}"]') for k in page.eval_on_selector_all("nav [data-tab]", "bs => bs.map((b) => b.dataset.tab)")]
        self.assertGreater(len(tabs), 10)
        self.assertEqual((nav["x"], nav["y"], nav["bottom"]), (0, 0, 720))
        self.assertEqual((header["x"], header["y"], header["height"]), (nav["width"], 0, 52))
        self.assertEqual((brand["x"], brand["y"], brand["bottom"]), (0, 0, header["bottom"] + 36))
        self.assertLessEqual(brand["right"], nav["right"])
        self.assertEqual(len({t["x"] for t in tabs}), 1, "the tabs are one column")
        self.assertEqual([t["y"] for t in tabs], sorted(t["y"] for t in tabs))
        self.assertGreaterEqual(tabs[0]["y"], brand["bottom"])
        self.assertLessEqual(tabs[-1]["bottom"], theme["y"])
        for prefs in (theme, box("#lang")):
            self.assertLessEqual(prefs["right"], nav["right"])
            self.assertGreater(prefs["y"], 720 - 60)
        self.assertEqual(box("main")["x"], nav["width"])
        self.assertTrue(page.is_visible("#page-title"))
        self.assertFalse(page.is_visible("#intro h2"))
        page.evaluate("S.pkgs.get('f:py').missing_deps = ['gone']; renderHeader()")
        self.assertEqual(page.eval_on_selector(".stat.bad", "s => getComputedStyle(s).color"), "rgb(230, 106, 90)")

    def test_the_mac_app_sidebar_puts_every_tab_in_its_group(self):
        page = self.mac_app_page()
        tabs = page.eval_on_selector_all("nav [data-tab]", """bs => bs.map((b) => {
          const r = b.getBoundingClientRect();
          return [b.dataset.tab, r.top, r.bottom];
        })""")
        gaps = [b[1] - a[2] for a, b in zip(tabs, tabs[1:])]
        groups = [[tabs[0][0]]]
        for (key, _, _), gap in zip(tabs[1:], gaps):
            if gap > min(gaps) + 5:
                groups.append([key])
            else:
                groups[-1].append(key)
        self.assertEqual(groups, [["overview"], ["requested", "deps", "casks", "services", "taps"],
                                  ["health", "apps", "shell"], [SECURE_IT], ["history", "install", "export"], ["settings"]])

    def test_the_mac_app_sidebar_draws_a_line_between_groups_and_spaces_tabs_evenly(self):
        page = self.mac_app_page()
        items = page.eval_on_selector_all("nav > *", """es => es.map((e) => {
          const r = e.getBoundingClientRect();
          return { key: e.dataset.tab || e.tagName, left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                   colour: getComputedStyle(e).borderTopColor };
        })""")
        lines = [i for i, e in enumerate(items) if e["key"] == "HR"]
        self.assertEqual([items[i + 1]["key"] for i in lines], ["requested", "health", SECURE_IT, "history", "settings"])
        for i in lines:
            above, line, below = items[i - 1], items[i], items[i + 1]
            self.assertEqual(line["bottom"] - line["top"], 1, below["key"])
            self.assertNotEqual(line["colour"], "rgba(0, 0, 0, 0)", below["key"])
            self.assertTrue(below["left"] < line["left"] < line["right"] < below["right"], f"the line before {below['key']} is not inset")
            self.assertGreater(line["top"] - above["bottom"], 0, f"the line before {below['key']} touches the tab above")
            self.assertAlmostEqual(line["top"] - above["bottom"], below["top"] - line["bottom"], delta=0.5,
                                   msg=f"the line before {below['key']} is off the middle of its gap")
        tabs = [e for e in items if e["key"] != "HR"]
        inside = {round(b["top"] - a["bottom"], 1) for a, b in zip(tabs, tabs[1:])
                  if b["key"] not in ("requested", "health", SECURE_IT, "history", "settings")}
        across = {round(items[i + 1]["top"] - items[i - 1]["bottom"], 1) for i in lines}
        self.assertEqual((len(inside), len(across)), (1, 1), f"uneven gaps: {inside} within groups, {across} between them")

    def test_the_mac_app_smallest_window_shows_every_tab_without_scrolling(self):
        swift = (WEB.parent / "macos" / "brewdash.swift").read_text()
        width, height = map(int, re.search(r"window\.minSize = NSSize\(width: (\d+), height: (\d+)\)", swift).groups())
        page = self.mac_app_page({"width": width, "height": height})
        self.assertEqual(page.eval_on_selector("nav", "n => n.scrollHeight - n.clientHeight"), 0)
        last = page.eval_on_selector_all("nav [data-tab]", "bs => bs.at(-1).getBoundingClientRect().bottom")
        self.assertLessEqual(last, self.box(page, "#btn-theme")["y"], "the last tab is under the theme button")

    def test_the_mac_app_band_follows_the_height_the_window_gives_it(self):
        page = self.mac_app_page({"width": 1280, "height": 400})
        page.evaluate("document.documentElement.style.setProperty('--bar', '104px')")
        box = lambda s: self.box(page, s)
        self.assertEqual((box("header")["height"], box(".brand")["bottom"]), (104, 140))
        self.assertEqual(self.window_asks(page, 640, 90), ["drag"])
        page.eval_on_selector("nav", "n => n.scrollTop = n.scrollHeight")
        self.assertTrue(page.eval_on_selector("nav", "n => n.scrollTop > 0"), "the sidebar does not scroll at this height")
        hits = page.evaluate("[...Array(400).keys()].filter((y) => document.elementFromPoint(98, y)?.closest('nav button'))")
        self.assertTrue(any(y >= 140 for y in hits), "the scan line crosses no tab at all")
        self.assertEqual([y for y in hits if y < 140], [], "a scrolled tab shows in the band or over the app name")
        page.evaluate("document.documentElement.style.setProperty('--bar', '20px')")
        self.assertLessEqual(box(".heading")["bottom"], box("header")["bottom"], "a band too short for the toolbar cuts it off")

    def test_the_mac_app_sidebar_fades_a_light_in_under_the_mouse(self):
        page = self.mac_app_page()
        bg = lambda tab: page.eval_on_selector(f'nav [data-tab="{tab}"]', "b => getComputedStyle(b).backgroundColor")
        rest, active = bg("deps"), bg("overview")
        page.hover('nav [data-tab="deps"]')
        page.wait_for_timeout(300)
        self.assertNotIn(bg("deps"), (rest, active), "a tab under the mouse does not light up, or lights up as the open one")
        page.hover('nav [data-tab="overview"]')
        page.wait_for_timeout(300)
        self.assertEqual((bg("deps"), bg("overview")), (rest, active), "the light stays behind, or the open tab loses its own")
        prop, dur = page.eval_on_selector('nav [data-tab="deps"]', "b => [getComputedStyle(b).transitionProperty, getComputedStyle(b).transitionDuration]")
        self.assertEqual(prop, "background-color")
        self.assertGreater(float(dur.rstrip("s")), 0, "the light snaps on instead of fading in")

    def test_the_mac_app_keeps_the_page_intro_shut_to_one_line_until_it_is_opened(self):
        page = self.mac_app_page({"width": 900, "height": 600})
        page.click('nav [data-tab="requested"]')
        what, todo = "#intro summary p", "#intro .intro > p"
        turn = lambda: page.eval_on_selector("#intro summary", "s => getComputedStyle(s, '::before').transform")
        self.assertFalse(page.is_visible(todo), "the intro opens by itself")
        self.assertTrue(page.eval_on_selector(what, "p => p.scrollWidth > p.clientWidth"), "the text is not cut to one line")
        line, shut = self.box(page, what), self.box(page, "#intro .intro")
        self.assertAlmostEqual(line["y"] - shut["y"], shut["bottom"] - line["bottom"], delta=1, msg="the shut intro's line is off its middle")
        one, closed = line["height"], turn()
        page.click("#intro summary")
        page.wait_for_timeout(300)
        self.assertTrue(page.is_visible(todo))
        self.assertGreater(self.box(page, what)["height"], one, "the opened intro still cuts its text to one line")
        self.assertEqual(closed, "none")
        self.assertRegex(turn(), r"^matrix\([-0-9.e]+, 1, -1, ", "the arrow does not turn down when the intro opens")
        arrow, text = page.eval_on_selector("#intro summary", "s => [getComputedStyle(s, '::before').height, "
                                            "getComputedStyle(s.querySelector('p')).lineHeight].map(parseFloat)")
        self.assertAlmostEqual(arrow, text, delta=1, msg="the arrow stretches with the text and turns off its line")
        self.assertAlmostEqual(self.box(page, todo)["x"], self.box(page, what)["x"], delta=0.5,
                               msg="what to do does not line up with the text above it")
        page.click('nav [data-tab="health"]')
        page.evaluate("renderAll()")
        self.assertTrue(page.is_visible(todo), "the intro shuts again on another page")
        page.click("#intro summary")
        page.click('nav [data-tab="shell"]')
        self.assertFalse(page.is_visible(todo), "the intro opens again on another page")

    def test_in_a_browser_the_header_is_one_row_with_the_tabs_in_a_row_under_it(self):
        box = lambda s: self.box(self.page, s)
        self.tab("casks")
        header, nav = box("header"), box("nav")
        self.assertEqual((header["x"], header["y"], header["width"]), (0, 0, 1280))
        self.assertEqual(nav["y"], header["bottom"])
        tabs = [box(f'nav [data-tab="{k}"]') for k in self.page.eval_on_selector_all("nav [data-tab]", "bs => bs.map((b) => b.dataset.tab)")]
        self.assertGreater(len(tabs), 10)
        self.assertEqual(len({t["y"] for t in tabs}), 1, "the tabs are one row")
        self.assertEqual(set(self.page.eval_on_selector_all("nav hr", "hs => hs.map((h) => h.getClientRects().length)")), {0},
                         "the Mac app's group lines show in the browser's tab row")
        self.assertEqual([t["x"] for t in tabs], sorted(t["x"] for t in tabs))
        for part in (".brand", "#stats", "#btn-refresh", "#btn-theme", "#lang"):
            self.assertAlmostEqual(box(part)["middle"][1], header["middle"][1], delta=1.5, msg=part)
        refresh, update, theme, lang = box("#btn-refresh"), box("#btn-update"), box("#btn-theme"), box("#lang")
        self.assertEqual(lang["x"] - theme["right"], update["x"] - refresh["right"], "theme and language sit apart like the other buttons")
        self.assertGreater(box("#stats")["x"], box(".brand")["right"])
        self.assertFalse(self.page.is_visible("#page-title"))
        self.assertTrue(self.page.is_visible("#intro h2"))
        self.page.evaluate("S.pkgs.get('f:py').missing_deps = ['gone']; renderHeader()")
        self.assertEqual(self.page.eval_on_selector(".stat.bad", "s => getComputedStyle(s).borderTopColor"), "rgb(192, 57, 43)")

    def test_the_mac_app_window_hears_every_theme_and_its_panel_colour(self):
        page = self.mac_app_page()
        sent = lambda: [(m["theme"], m["panel"].strip()) for m in page.evaluate("window.sent") if "theme" in m]
        page.click("#btn-theme")
        page.click("#btn-theme")
        page.click("#btn-theme")
        self.assertEqual(sent()[-4:], [("auto", "#161a21"), ("light", "#ffffff"), ("dark", "#161a21"),
                                       ("auto", "#161a21")])
        page.emulate_media(color_scheme="light")
        page.wait_for_function("window.sent.at(-1).panel.trim() === '#ffffff'")
        self.assertEqual(sent()[-1], ("auto", "#ffffff"))

    def test_the_mac_app_saves_the_brewfile_through_its_own_dialog_instead_of_a_download(self):
        page = self.mac_app_page()
        downloads = []
        page.on("download", lambda d: downloads.append(d))
        page.evaluate("downloadBrewfile()")
        page.wait_for_function("window.sent.some((m) => m.save)")
        saved = [m for m in page.evaluate("window.sent") if "save" in m]
        self.assertEqual([m["save"] for m in saved], ["Brewfile"])
        self.assertTrue(saved[0]["text"].startswith("# Brewfile from brewdash."), saved[0]["text"])
        self.assertEqual(downloads, [])


# How brewdash's own server could come back into a text. Chinese writes a Latin word with no space around it
# ("新token"), so a word's edge is "no Latin letter next to it", not \b.
SERVER_WORDS = r"127\.0\.0\.1|localhost|(?<![a-z])(?:server|sunucu|servidor|token|api(?![a-z]))|服务器|令牌"


@unittest.skipUnless(sync_playwright, "Playwright for Python is not installed")
class ProjectPageInstallTest(unittest.TestCase):
    """docs/index.html, the page that hands out the install command."""
    FILE = Path(brewdash.__file__).resolve().parent / "docs" / "index.html"
    PAGE = FILE.as_uri()
    COMMAND = "brew trust lemiorhan/brewdash && brew install --cask lemiorhan/brewdash/brewdash"
    ZIP = "https://github.com/lemiorhan/brewdash/releases/latest/download/brewdash-macos.zip"

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_every_language_hands_out_the_cask_and_the_latest_zip_and_no_script(self):
        page = self.browser.new_page(locale="en-US")
        self.addCleanup(page.close)
        page.goto(self.PAGE)
        for lang in ("en", "tr", "es", "zh-CN"):
            with self.subTest(lang=lang):
                page.select_option("select", lang)
                self.assertEqual(page.inner_text("#install-command"), self.COMMAND)
                self.assertEqual(page.locator("#install a").evaluate_all("as => as.map((a) => a.href)"), [self.ZIP])
                self.assertEqual(page.inner_text("#details .snippet >> nth=0"), "brew uninstall --cask brewdash")
                # Homebrew refuses to install over a brewdash.app the install script built until it is given --force.
                self.assertIn("--force", page.inner_text("[data-i18n='app.install.body']"))
        self.assertEqual(re.findall(r"install\.sh|curl |wget ", self.FILE.read_text()), [])

    def test_no_language_talks_about_a_browser_or_a_server_outside_the_developer_snippets(self):
        page = self.browser.new_page(locale="en-US")
        self.addCleanup(page.close)
        page.goto(self.PAGE)
        app = {
            "en": ("brewdash.app is signed by its developer and notarized by Apple. It opens in its own window, with a menu bar and a Dock icon.",
                   "Press ⌘ Q to quit brewdash."),
            "tr": ("brewdash.app'i geliştiricisi imzaladı, Apple da notarize etti. Uygulama kendi penceresinde, menü bar'ı ve Dock ikonuyla açılıyor.",
                   "brewdash'ten çıkmak için ⌘ Q'ya bas."),
            "es": ("brewdash.app está firmada por su desarrollador y notarizada por Apple. Se abre en su propia ventana, con barra de menús e icono en el Dock.",
                   "Pulsa ⌘ Q para salir de brewdash."),
            "zh-CN": ("brewdash.app 由开发者签名，并经过 Apple 公证。它在自己的窗口中打开，带菜单栏和程序坞图标。",
                      "按 ⌘ Q 退出 brewdash。"),
        }
        words = re.compile(SERVER_WORDS + r"|--browser|(?<![a-z])browser|(?<!güvenlik )tarayıcı|navegador|浏览器", re.I)
        for lang, texts in app.items():
            with self.subTest(lang=lang):
                page.select_option("select", lang)
                self.assertEqual((page.inner_text("[data-i18n='app.intro']"), page.inner_text("[data-i18n-html='app.quit.body']")),
                                 texts)
        # The file, not the rendered page: crawlers and link previews read the head as written, before any language applies.
        source = re.sub(r'<pre class="snippet">.*?</pre>', "", self.FILE.read_text(), flags=re.S)
        self.assertEqual([intro in source for intro, _ in app.values()], [True] * len(app))
        self.assertEqual(words.findall(source), [])


LANGS = ["en", "tr", "es", "zh"]
WEB = Path(brewdash.__file__).resolve().parent / "web"


@unittest.skipUnless(sync_playwright, "Playwright for Python is not installed")
class I18nTest(UiFixture):
    def keys(self, lang):
        return set(self.page.evaluate(f"Object.keys(I18N.{lang})"))

    def choose(self, lang):
        self.page.select_option("#lang", lang)

    def test_every_language_has_the_english_keys_and_every_server_message(self):
        en = self.keys("en")
        self.assertGreater(len(en), 300)
        self.assertEqual({k for k in en if k.startswith("srv.")}, set())
        srv = {"srv." + k for k in brewdash.MESSAGES}
        for lang in LANGS[1:]:
            with self.subTest(lang=lang):
                self.assertEqual(self.keys(lang), en | srv)
                empty = self.page.evaluate(f"""Object.entries(I18N.{lang}).filter(([, v]) =>
                    typeof v === "string" ? !v.trim() : !v.other?.trim()).map(([k]) => k)""")
                self.assertEqual(empty, [])

    def test_no_text_tells_the_user_about_brewdashs_own_server(self):
        # The Services tab is about the user's brew services, which do run servers and can be bound to 127.0.0.1.
        about_brew_services = {"intro.services.what", "intro.services.todo"}
        words = re.compile(SERVER_WORDS, re.I)
        for lang in LANGS:
            with self.subTest(lang=lang):
                texts = dict(self.page.evaluate(f"""Object.entries(I18N.{lang}).map(([k, v]) =>
                    [k, typeof v === "string" ? v : Object.values(v).join("\\n")])"""))
                self.assertLessEqual(about_brew_services | {"error.server", "hero.how_text"}, set(texts))
                self.assertEqual({k: words.findall(v) for k, v in texts.items()
                                  if k not in about_brew_services and words.search(v)}, {})

    def test_every_placeholder_survives_translation(self):
        bad = self.page.evaluate("""() => {
            const vars = (v) => new Set(((typeof v === "string" ? v : v.other).match(/\\{\\w+\\}/g)) || []);
            const out = [];
            for (const lang of ["tr", "es", "zh"]) for (const [k, v] of Object.entries(I18N[lang])) {
                if (k.startsWith("srv.")) continue;
                const want = vars(I18N.en[k]);
                for (const form of typeof v === "string" ? [v] : Object.values(v)) {
                    const got = vars(form);
                    if ([...want].some((x) => !got.has(x)) || [...got].some((x) => !want.has(x))) out.push(`${lang} ${k}`);
                }
            }
            return out;
        }""")
        self.assertEqual(bad, [])
        for lang in LANGS[1:]:
            for key, english in brewdash.MESSAGES.items():
                value = self.page.evaluate(f"I18N.{lang}[{json.dumps('srv.' + key)}]")
                forms = [value] if isinstance(value, str) else list(value.values())
                want = set(re.findall(r"\{\w+\}", english))
                for form in forms:
                    self.assertEqual(set(re.findall(r"\{\w+\}", form)), want, f"{lang} srv.{key}")

    def test_every_key_the_code_names_is_in_the_english_catalog(self):
        source = "".join((WEB / f).read_text() for f in ("app.js", "theme.js", "index.html"))
        named = set(re.findall(r'\bt\("([\w.]+)"[,)]', source)) | set(re.findall(r'data-i18n(?:-title)?="([\w.]+)"', source))
        self.assertGreater(len(named), 200)
        self.assertEqual(named - self.keys("en"), set())

    def test_every_key_built_at_runtime_is_in_the_english_catalog(self):
        source = "".join((WEB / f).read_text() for f in ("app.js", "theme.js"))
        prefixes = set(re.findall(r'\bt\("([\w.]+\.)" \+', source)) | set(re.findall(r'\bt\(`([\w.]+\.)\$\{', source))
        server = (WEB.parent / "brewdash.py").read_text()
        numbers = source[source.index("const counted"):source.index('t("num.title")')]
        source_kinds = re.search(r'source = "(\w+)" if .*?else \\\s*"(\w+)" if .*? else "(\w+)"', server, re.S)
        js = lambda expr: self.page.evaluate(expr)
        domains = {
            "theme.": js("Object.keys(THEME_ICON)"),
            "tab.": js("TABS"),
            "intro.": [f"{tab}.{part}" for tab in js("TABS") if tab != "overview" for part in ("what", "todo")],
            "flag.": js("FLAGS.map((f) => f[0])"),
            "col.": js("COLS.map((c) => c[0])"),
            "level.": js("Object.keys(LEVEL)"),
            "export.group.": js("LIST_GROUPS"),
            "svc.": js("SVC_OPS"),
            "sev.": js("SEVERITIES"),
            "plugin.view.": js("PLUGIN_VIEWS"),
            "plugin.show.": js("PLUGIN_TOGGLES"),
            "job.status.": js("JOB_STATUSES"),
            "num.": [k + s for k in re.findall(r'(?:\[|counted\()"(\w+)", ', numbers) for s in ("", "_help")],
            "sig.": re.findall(r'\{"kind": "(\w+)", "signer"', server),
            "source.": list(source_kinds.groups()) if source_kinds else [],
            "issue.": re.findall(r'advice = "(\w+)", ', server),
            "settings.channel.": [c + s for c in js("CHANNELS") for s in ("", "_help")],
            "settings.rel.": js("RELATIONS"),
        }
        self.assertEqual((js("CHANNELS"), set(js("RELATIONS"))), (list(brewdash.CHANNELS), set(brewdash.RELATION.values())))
        self.assertEqual(prefixes, set(domains))
        en = self.keys("en")
        for prefix, values in domains.items():
            with self.subTest(prefix=prefix):
                self.assertGreaterEqual(len(values), 2)
                self.assertEqual({prefix + v for v in values} - en, set())

    def test_no_key_is_missing_while_every_tab_renders_in_every_language(self):
        self.page.evaluate("""() => {
            window.missing = new Set();
            window.looked = 0;
            for (const lang of Object.keys(LANGS)) {
                const cat = I18N[lang];
                I18N[lang] = new Proxy(cat, { get: (o, k) => {
                    looked++;
                    if (!(k in o) && (lang !== "en" || !String(k).startsWith("srv."))) missing.add(`${lang} ${String(k)}`);
                    return o[k];
                } });
            }
        }""")
        tabs = self.page.evaluate("tabs()")
        self.assertIn(SECURE_IT, tabs)
        for lang in LANGS:
            self.choose(lang)
            for tab in tabs:
                self.page.click(f'nav button[data-tab="{tab}"]')
                self.page.wait_for_function("!document.querySelector('#content .loading')")
            self.page.click(f'nav button[data-tab="{SECURE_IT}"]')
            for view in self.page.evaluate("PLUGIN_VIEWS"):
                self.page.click(f'#content [data-pview="{view}"]')
            self.page.click('#content [data-pview="alerts"]')
            self.page.check('[data-pf="accepted"]')
            self.page.check('[data-pf="info"]')
            self.open("requested", "f:php@8.0")
            self.page.click("[data-advice]")
            self.page.wait_for_selector("#m-cancel")
            self.page.click("#m-cancel")
            self.page.click('nav button[data-tab="overview"]')
        self.assertGreater(self.page.evaluate("looked"), 1000)
        self.assertEqual(self.page.evaluate("[...missing].sort()"), [])

    def test_choosing_a_language_translates_the_page_and_the_server_messages_and_is_remembered(self):
        self.choose("tr")
        tr = lambda k: self.page.evaluate(f"I18N.tr[{json.dumps(k)}]")
        self.assertEqual(self.page.evaluate("document.documentElement.lang"), "tr")
        self.assertEqual(self.page.inner_text('nav button[data-tab="overview"]'), tr("tab.overview"))
        self.assertEqual(self.page.inner_text("#btn-refresh"), tr("header.refresh"))
        self.page.click('nav button[data-tab="requested"]')
        self.page.click('tr.row[data-id="f:php@8.0"] td:nth-child(2)')
        title = self.page.inner_text("#detail .detail-section .warn b")
        want = tr("srv.advice.deprecated.title_why").replace("{why}", tr("srv.reason.versioned_formula"))
        self.assertEqual(title, want)
        self.page.reload()
        self.page.wait_for_selector('tr.row[data-id="f:php@8.0"]')
        self.assertEqual(self.page.inner_text('nav button[data-tab="overview"]'), tr("tab.overview"))
        self.choose("en")
        self.assertEqual(self.page.inner_text('nav button[data-tab="overview"]'), "Overview")

    def tr_render(self, m):
        """The Turkish text of a server message, filled in here rather than by the page's own tm()."""
        if not isinstance(m, list):
            return m
        key, args = m
        value = self.page.evaluate(f"I18N.tr[{json.dumps('srv.' + key)}]")
        self.assertIsInstance(value, str, key)
        filled = {k: self.tr_render(v) for k, v in (args or {}).items()}
        return re.sub(r"\{(\w+)\}", lambda g: filled[g.group(1)], value)

    def test_every_server_message_on_screen_is_in_the_chosen_language(self):
        self.choose("tr")
        self.open("requested", "f:php@8.0")
        advice = self.page.evaluate("S.pkgs.get('f:php@8.0').advice.find((a) => a.level === 'warn')")
        warn = self.page.locator("#detail .detail-section .warn").first
        self.assertEqual(warn.locator("> div").first.inner_text(), " ".join(self.tr_render(p) for p in advice["msg"]["text"]))
        buttons = warn.locator("[data-advice]")
        self.assertEqual(buttons.all_inner_texts(), [self.tr_render(a["msg"]) for a in advice["actions"]])

        with self.page.expect_response(lambda r: "/api/plan" in r.url) as plan:
            buttons.first.click()
        title_msg = plan.value.json()["title_msg"]
        self.assertEqual(self.page.inner_text("#modal-card h2"), self.tr_render(title_msg))
        self.page.click("#m-ok")
        self.page.wait_for_function("document.querySelector('.job-head b')?.textContent")
        self.assertEqual(self.page.inner_text(".job-head b"), self.tr_render(title_msg))
        job_time = self.page.evaluate("new Date(S.jobs.at(-1).created * 1000).toLocaleTimeString('tr')")
        self.assertEqual(self.page.locator(".job-head .muted").first.inner_text(), job_time)

        self.page.click('nav button[data-tab="history"]')
        self.page.wait_for_function("S.history?.length")
        created = self.page.evaluate("S.history[0].created")
        cells = self.page.locator("#view tbody tr").first.locator("td").all_inner_texts()
        self.assertEqual(cells[:2], [self.page.evaluate(f"new Date({created} * 1000).toLocaleString('tr')"), self.tr_render(title_msg)])

        self.page.click('nav button[data-tab="shell"]')
        row = self.page.locator('#view tbody tr:has-text("/nope/bin")')
        self.assertEqual(row.locator("td").nth(3).inner_text(), self.tr_render(["path.missing", {}]))

        self.page.click('nav button[data-tab="overview"]')
        self.assertIn("Pazartesi", self.page.inner_text("#content"))

    def test_advice_text_falls_back_to_english_whole_when_one_part_lacks_a_translation(self):
        self.choose("tr")
        self.page.evaluate("delete I18N.tr['srv.advice.unused_requested']")
        self.open("requested", "f:php@8.0")
        advice = self.page.evaluate("S.pkgs.get('f:php@8.0').advice.find((a) => a.level === 'warn')")
        warn = self.page.locator("#detail .detail-section .warn").first
        self.assertEqual(warn.locator("> div").first.inner_text(), advice["text"])

    def test_a_server_message_the_language_lacks_stays_in_english(self):
        self.choose("tr")
        self.page.evaluate("delete I18N.tr['srv.reason.versioned_formula']")
        self.open("requested", "f:php@8.0")
        self.assertEqual(self.page.inner_text("#detail .detail-section .warn b"),
                         "Deprecated by Homebrew: it is an old versioned formula")

    def test_the_browser_language_picks_the_first_supported_language(self):
        for locale, want in (("tr-TR", "tr"), ("es-MX", "es"), ("zh-TW", "zh"), ("de-DE", "en")):
            ctx = self.browser.new_context(locale=locale)
            page = ctx.new_page()
            page.goto(self.url)
            with self.subTest(locale=locale):
                self.assertEqual(page.evaluate("LANG"), want)
                self.assertEqual(page.input_value("#lang"), want)
            ctx.close()


if __name__ == "__main__":
    unittest.main()
