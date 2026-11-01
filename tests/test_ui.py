"""Browser smoke tests for web/app.js, against a server backed by FakeBrew.

Needs Playwright for Python: `python3 -m venv .venv && .venv/bin/pip install playwright`,
then run the suite with `.venv/bin/python -m unittest discover -s tests`. Skipped without it.
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brewdash  # noqa: E402
from test_brewdash import APP_CASKS, FakeBrew, cask, formula, make_app, wait_for  # noqa: E402

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


FP = "0123456789abcdef" * 4
REPORT = {"schema": 1, "generated_at": "2026-11-01T09:00:00", "alerts": [
    {"fingerprint": "f" * 64, "check_id": "persistence.path_shadow", "kind": "shadowed", "severity": "low",
     "title": "~/bin shadows 1 system command", "detail": "git", "remediation": "Move it after $PATH.",
     "fix_command": None, "identity": {"dir": "~/bin"}},
    {"fingerprint": FP, "check_id": "exposure.listening", "kind": "brew_listener", "severity": "high",
     "title": "jq listens on *:8080", "detail": "pid 77", "remediation": "Stop the service.",
     "fix_command": "brew services stop jq", "identity": {"exe": "/opt/homebrew/Cellar/jq/1.7/bin/jq"}},
    {"fingerprint": "not-a-fingerprint", "check_id": "packages.brew_taps", "kind": "third_party_tap",
     "severity": "medium", "title": XSS, "detail": ATTR_XSS, "remediation": None,
     "fix_command": f"brew untap {ATTR_XSS}", "identity": {"tap": "me/tap"}},
]}


@unittest.skipUnless(sync_playwright, "Playwright for Python is not installed")
class UiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "cache" / "api").mkdir(parents=True)
        (root / "cache" / "api" / "cask_names.txt").write_text("zed\nshop\n")
        cls.zed = make_app(root / "Applications", "Zed", "0.8")
        make_app(root / "Applications", "Shop", "1.0", app_store=True)
        make_app(root / "Applications", "Bare", "1.0")
        (root / "home" / "secure-it").mkdir(parents=True)
        cls.report = root / "home" / "secure-it" / "report.json"
        cls.report.write_text(json.dumps(REPORT))
        (root / "home" / ".zshrc").write_text("export PATH=/nope/bin:$PATH\nexport PATH=$HOME/Dropbox/bin:$PATH\n")
        cls.list_file = root / "state" / "list.json"
        (root / "prefix" / "Cellar" / "jq").mkdir(parents=True)
        (root / "prefix" / "Cellar" / "jq" / "bin").write_bytes(b"x" * 65536)
        cls.brew = UiBrew(info=INFO, cache=str(root / "cache"), casks=APP_CASKS, prefix=str(root / "prefix"))
        cls.server, cls.url, _ = brewdash.serve(port=0, open_browser=False, brew=cls.brew, state_dir=str(root / "state"),
                                                home=str(root / "home"), app_dirs=[str(root / "Applications")],
                                                sysrun=ui_sysrun)
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

    def test_overview_is_the_start_page_and_lists_what_needs_attention_worst_first(self):
        self.page.wait_for_selector('.att:has-text("cleanup")')
        self.page.wait_for_selector('.att:has-text("apps without a real signature")')
        self.assertEqual(self.attention(), [
            ("critical", "health", "3 deprecated or disabled"),
            ("critical", "security", "1 secure-it findings"),
            ("warning", "health", "brew doctor has warnings"),
            ("warning", "requested:upgradable", "1 can be upgraded in Installed by you"),
            ("warning", "requested:outdated,pinned", "1 outdated but pinned in Installed by you"),
            ("warning", "update", "brew update is stale"),
            ("warning", "shell", "1 synced folders ahead in PATH"),
            ("warning", "shell", "1 PATH problems"),
            ("warning", "security", "1 secure-it findings"),
            ("warning", "services", "1 services reachable from the network"),
            ("warning", "apps", "1 apps without a real signature"),
            ("info", "security", "1 secure-it findings"),
            ("info", "health", "cleanup"),
            ("info", "apps", "1 apps brew could manage"),
            ("info", "taps", "1 unused taps"),
        ])

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
            "uninstall": ("Uninstall (2)", False, None), "list": ("Add to list (2)", False, None), "clear": ("Clear", False, None)})
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

    def test_a_tap_and_an_app_alone_offer_only_the_list(self):
        self.page.click('nav button[data-tab="taps"]')
        self.page.check('[data-sel="t:me/tap"]')
        b = self.bulk()
        self.assertEqual(b["list"], ("Add to list (1)", False, None))
        self.assertEqual(b["uninstall"], ("Uninstall (0)", True, "Only installed formulae and casks; the selection has none."))

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
        self.assertEqual(rows["Shop"].locator("input, button").count(), 0)
        self.assertIn("updates through the App Store", rows["Shop"].inner_text())
        self.assertEqual(rows["Bare"].locator("button").inner_text(), "Search cask")
        self.assertEqual(self.page.locator("[data-adopt]").count(), 1)
        rows["Zed"].locator("[data-adopt]").click()
        self.page.wait_for_selector("#modal-card pre")
        self.assertEqual(self.page.inner_text("#modal-card pre"), "brew install --cask --adopt zed")
        self.page.click("#m-ok")
        wait_for(lambda: ["brew", "install", "--cask", "--adopt", "zed"] in self.brew.popened)

    def test_shell_tab_shows_the_issue_and_what_to_do(self):
        self.page.click('nav button[data-tab="shell"]')
        row = self.page.locator('#view tbody tr:has-text("/nope/bin")')
        self.assertEqual(row.inner_text().split("\t"), [".zshrc:1", "/nope/bin", "MISSING", "The directory does not exist; this line can go."])
        synced = self.page.locator('#view tbody tr:has-text("Dropbox")').inner_text().split("\t")
        self.assertEqual(synced[:3], [".zshrc:2", "$HOME/Dropbox/bin", "SYNCED FOLDER FIRST"])

    def test_sources_brewdash_could_not_read_are_said_and_escaped(self):
        self.page.evaluate("""S.snap.ports = {error: 'could not run lsof: <b>x</b>', by_id: {}};
            S.snap.security = {...S.snap.security, error: 'could not read it: <i>y</i>', alerts: []}; renderAll()""")
        self.assertIn(("warning", "security", "secure-it report unreadable"), self.attention())
        self.tab("services")
        self.assertEqual(self.page.inner_text("#content .warn"), "could not run lsof: <b>x</b>")
        self.tab("security")
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

    def test_security_tab_lists_findings_worst_first_and_escapes_them(self):
        self.tab("security")
        self.page.wait_for_selector("#content .alert")
        cards = self.page.locator("#content .alert").all()
        self.assertEqual([c.locator(".sev").inner_text().lower() for c in cards], ["high", "medium", "low"])
        self.assertIn(XSS, cards[1].inner_text())
        self.assertEqual(self.page.locator("#content img").count(), 0)
        self.assertEqual(self.page.locator("#content [onmouseover]").count(), 0)
        self.assertIsNone(self.page.evaluate("window.pwned"))
        self.assertEqual(self.page.locator('nav button[data-tab="security"] .count').inner_text(), "3")

    def test_a_finding_offers_its_fix_and_accept_commands_to_copy_never_to_run(self):
        self.tab("security")
        high = self.page.locator("#content .alert.sev-high")
        self.assertEqual([p.inner_text() for p in high.locator("pre").all()],
                         ["brew services stop jq", f'sudo secure-it accept {FP[:12]} --note "why this is fine"'])
        self.assertEqual(self.page.locator("#content .alert.sev-medium pre").count(), 1)
        self.assertEqual(self.page.locator("#content [data-act], #content [data-svc], #content [data-install]").count(), 0)
        before = list(self.brew.popened)
        for b in self.page.locator("#content [data-copytext]").all():
            b.click()
        self.assertEqual(self.brew.popened, before)

    def test_a_finding_links_to_its_package_and_the_package_shows_it(self):
        self.tab("security")
        self.page.click('#content .alert.sev-high [data-goto="f:jq"]')
        self.page.wait_for_selector("#detail h2.detail-title")
        self.assertIn("jq listens on *:8080", self.page.inner_text("#detail .detail-section:has(h4:text-is('secure-it findings'))"))
        self.assertIn("TCP *:8080", self.page.inner_text("#detail"))
        self.tab("security")
        self.page.click('#content .alert.sev-low [data-tab="shell"]')
        self.assertEqual(self.page.inner_text("#intro h2"), "Shell")
        self.tab("security")
        self.page.click('#content .alert.sev-medium button:text-is("Open me/tap in Taps")')
        self.assertEqual(self.page.inner_text("#intro h2"), "Taps")
        self.assertEqual(self.page.locator('#content tr:has-text("me/tap") .badge:text-is("secure-it")').count(), 1)

    def test_without_a_report_the_tab_says_how_to_get_one(self):
        self.page.evaluate("S.snap.security = {found: false, path: '/h/secure-it/report.json', alerts: []}; S.tab = 'security'; renderAll()")
        self.assertIn("sudo secure-it scan", self.page.inner_text("#content"))
        self.assertEqual(self.page.locator('nav button[data-tab="security"] .count').count(), 0)
        self.page.evaluate("S.tab = 'overview'; renderAll()")
        self.assertNotIn("security", [go for _, go, _ in self.attention()])

    def test_a_new_report_shows_up_without_a_brew_change(self):
        try:
            self.cls_report({**REPORT, "alerts": REPORT["alerts"][:1]})
            self.page.wait_for_function("document.querySelector('nav button[data-tab=\"security\"] .count').textContent === '1'")
        finally:
            self.cls_report(REPORT)
        self.page.wait_for_function("document.querySelector('nav button[data-tab=\"security\"] .count').textContent === '3'")

    def cls_report(self, data):
        self.report.write_text(json.dumps(data) + " " * len(data["alerts"]))

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

    def test_contact_page_names_the_author_and_links_the_repo(self):
        self.tab("contact")
        self.assertIn("Lemi Orhan Ergin", self.page.inner_text("#content"))
        hrefs = self.page.eval_on_selector_all("#content a", "as => as.map(a => a.href)")
        self.assertEqual(hrefs, ["https://github.com/lemiorhan", "https://github.com/lemiorhan/brewdash",
                                 "https://github.com/lemiorhan/brewdash/issues",
                                 "https://github.com/lemiorhan/brewdash/security/advisories/new"])

    def test_contact_page_shows_while_brew_cannot_be_read(self):
        self.page.evaluate("S.snap = { error: 'boom' }; S.tab = 'contact'; renderView()")
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


if __name__ == "__main__":
    unittest.main()
