"""Browser smoke tests for web/app.js, against a server backed by FakeBrew.

Needs Playwright for Python: `python3 -m venv .venv && .venv/bin/pip install playwright`,
then run the suite with `.venv/bin/python -m unittest discover -s tests`. Skipped without it.
"""
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
        return super().json(*args)


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
        (root / "home").mkdir()
        (root / "home" / ".zshrc").write_text("export PATH=/nope/bin:$PATH\n")
        cls.list_file = root / "state" / "list.json"
        (root / "prefix" / "Cellar" / "jq").mkdir(parents=True)
        (root / "prefix" / "Cellar" / "jq" / "bin").write_bytes(b"x" * 65536)
        cls.brew = UiBrew(info=INFO, cache=str(root / "cache"), casks=APP_CASKS, prefix=str(root / "prefix"))
        cls.server, cls.url, _ = brewdash.serve(port=0, open_browser=False, brew=cls.brew, state_dir=str(root / "state"),
                                                home=str(root / "home"), app_dirs=[str(root / "Applications")])
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
        self.assertEqual(self.attention(), [
            ("critical", "health", "3 deprecated or disabled"),
            ("warning", "health", "brew doctor has warnings"),
            ("warning", "requested:upgradable", "1 can be upgraded in Installed by you"),
            ("warning", "requested:outdated,pinned", "1 outdated but pinned in Installed by you"),
            ("warning", "update", "brew update is stale"),
            ("warning", "shell", "1 PATH problems"),
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
            ["9", "things to look at", "worst: critical"],
            ["0/0", "services running"],
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

    def uninstall_ssl(self):
        self.open("deps", "f:ssl@1")
        self.page.click('#detail [data-act="uninstall"]')
        self.page.wait_for_selector("#add-blockers")

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


if __name__ == "__main__":
    unittest.main()
