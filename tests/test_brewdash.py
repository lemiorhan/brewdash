import contextlib
import http.client
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brewdash  # noqa: E402


def formula(name, deps=(), requested=False, aliases=(), oldnames=(), indirect=(), pinned=False, **extra):
    runtime = [{"full_name": d, "declared_directly": True} for d in deps]
    runtime += [{"full_name": d, "declared_directly": False} for d in indirect]
    base = {
        "name": name, "full_name": name, "desc": f"{name} desc", "homepage": f"https://{name}.example",
        "tap": "homebrew/core", "versions": {"stable": "1.0"}, "pinned": pinned, "outdated": False,
        "deprecated": False, "disabled": False, "caveats": None, "license": "MIT", "keg_only": False,
        "aliases": list(aliases), "oldnames": list(oldnames),
        "installed": [{"version": "1.0", "time": 1700000000, "installed_on_request": requested,
                       "runtime_dependencies": runtime}],
    }
    base.update(extra)
    return base


def cask(token, depends_on=None, tap="homebrew/cask", artifacts=(), **extra):
    base = {
        "token": token, "full_token": token, "name": [token.title()], "desc": f"{token} app",
        "homepage": None, "tap": tap, "installed": "2.0", "version": "2.0", "installed_time": 1700000001,
        "outdated": False, "pinned": False, "deprecated": False, "disabled": False, "caveats": None,
        "depends_on": depends_on if depends_on is not None else {}, "artifacts": list(artifacts),
    }
    base.update(extra)
    return base


# app depends on libA and libB; libA depends on libC; libB on old name "icu" (alias of icu@2)
# tool is requested and depends on libC; gui cask depends on formula python
INFO = {
    "formulae": [
        formula("app", deps=["libA", "libB"], requested=True, indirect=["libC"]),
        formula("libA", deps=["libC"]),
        formula("libB", deps=["icu", "gone@1"]),
        formula("libC"),
        formula("icu@2", aliases=["icu"]),
        formula("tool", deps=["libC"], requested=True, pinned=True),
        formula("python"),
        formula("stray"),
        formula("old", disabled=True, disable_date="2026-09-01", disable_reason="unmaintained",
                disable_replacement_formula="new"),
    ],
    "casks": [
        cask("gui", depends_on={"formula": ["python"], "macos": {">=": ["14"]}},
             artifacts=[{"app": ["Gui.app"], "target": "/Applications/Gui.app"}, {"binary": ["gui"]}]),
        cask("font", depends_on={"macos": {}}, tap=None),
    ],
}
OUTDATED = {"formulae": [{"name": "libA", "installed_versions": ["1.0"], "current_version": "1.5", "pinned": False}],
            "casks": [{"name": "gui", "installed_versions": ["2.0"], "current_version": "3.0", "pinned": False}]}
SERVICES = [{"name": "tool", "status": "started", "running": True, "loaded": True, "pid": 42, "user": "me",
             "file": "/x.plist", "log_path": "/x.log", "extra": "ignored"}]
TAPS = [{"name": "homebrew/core", "remote": "https://github.com/Homebrew/homebrew-core", "official": True,
         "private": False, "last_commit": "1 day ago", "formula_names": ["a", "b", "c"], "cask_tokens": []},
        {"name": "me/tap", "remote": "https://github.com/me/homebrew-tap", "official": False,
         "private": False, "last_commit": "2 days ago", "formula_names": [], "cask_tokens": None}]


def model():
    return brewdash.build_model(INFO, OUTDATED, SERVICES, TAPS, ["stray"])


def by_id():
    return {p["id"]: p for p in model()["packages"]}


class BuildModelTest(unittest.TestCase):
    def test_every_installed_package_has_one_record(self):
        self.assertEqual(sorted(by_id()), sorted([
            "f:app", "f:libA", "f:libB", "f:libC", "f:icu@2", "f:tool", "f:python", "f:stray", "f:old",
            "c:gui", "c:font"]))

    def test_direct_runtime_deps_become_edges_and_indirect_ones_do_not(self):
        p = by_id()
        self.assertEqual(p["f:app"]["deps"], ["f:libA", "f:libB"])
        self.assertEqual(p["f:libA"]["deps"], ["f:libC"])
        self.assertEqual(p["f:libC"]["dependents"], ["f:libA", "f:tool"])

    def test_dependency_recorded_under_an_alias_resolves_to_the_installed_formula(self):
        p = by_id()
        self.assertEqual(p["f:libB"]["deps"], ["f:icu@2"])
        self.assertEqual(p["f:icu@2"]["dependents"], ["f:libB"])

    def test_dependency_recorded_under_an_old_name_resolves(self):
        info = {"formulae": [formula("x", deps=["renamed-from"]), formula("y", oldnames=["renamed-from"])], "casks": []}
        p = {r["id"]: r for r in brewdash.build_model(info, {}, [], [], [])["packages"]}
        self.assertEqual(p["f:x"]["deps"], ["f:y"])
        self.assertEqual(p["f:x"]["missing_deps"], [])

    def test_a_real_name_wins_over_another_formulas_alias(self):
        info = {"formulae": [formula("x", deps=["icu"]), formula("icu"), formula("icu@2", aliases=["icu"])], "casks": []}
        p = {r["id"]: r for r in brewdash.build_model(info, {}, [], [], [])["packages"]}
        self.assertEqual(p["f:x"]["deps"], ["f:icu"])

    def test_dependency_that_is_not_installed_is_reported_missing(self):
        p = by_id()
        self.assertEqual(p["f:libB"]["missing_deps"], ["gone@1"])
        self.assertEqual(p["f:app"]["missing_deps"], [])

    def test_cask_formula_dependency_is_an_edge_and_macos_requirement_is_not(self):
        p = by_id()
        self.assertEqual(p["c:gui"]["deps"], ["f:python"])
        self.assertEqual(p["f:python"]["dependents"], ["c:gui"])
        self.assertEqual(p["c:font"]["deps"], [])
        self.assertEqual(p["c:font"]["missing_deps"], [])

    def test_cask_dependency_on_uninstalled_cask_is_missing(self):
        info = {"formulae": [], "casks": [cask("a", depends_on={"cask": ["b", "c"]}), cask("b")]}
        p = {r["id"]: r for r in brewdash.build_model(info, {}, [], [], [])["packages"]}
        self.assertEqual(p["c:a"]["deps"], ["c:b"])
        self.assertEqual(p["c:a"]["missing_deps"], ["c"])

    def test_outdated_marks_packages_and_takes_the_current_version(self):
        p = by_id()
        self.assertTrue(p["f:libA"]["outdated"])
        self.assertEqual(p["f:libA"]["latest"], "1.5")
        self.assertTrue(p["c:gui"]["outdated"])
        self.assertEqual(p["c:gui"]["latest"], "3.0")
        self.assertEqual([i for i, r in p.items() if r["outdated"]], ["c:gui", "f:libA"])

    def test_requested_comes_from_install_record_and_casks_are_always_requested(self):
        p = by_id()
        self.assertEqual(sorted(i for i, r in p.items() if r["requested"]), ["c:font", "c:gui", "f:app", "f:tool"])

    def test_service_is_attached_with_known_fields_only(self):
        p = by_id()
        self.assertEqual(p["f:tool"]["service"], {"status": "started", "running": True, "loaded": True, "pid": 42,
                                                  "user": "me", "file": "/x.plist", "log_path": "/x.log"})
        self.assertEqual([i for i, r in p.items() if r["service"]], ["f:tool"])

    def test_autoremove_names_become_orphans(self):
        self.assertEqual([i for i, r in by_id().items() if r["orphan"]], ["f:stray"])

    def test_cask_display_name_and_app_paths(self):
        p = by_id()
        self.assertEqual(p["c:gui"]["display_name"], "Gui")
        self.assertEqual(p["c:gui"]["app_paths"], ["/Applications/Gui.app"])
        self.assertEqual(p["c:gui"]["version"], "2.0")

    def test_tap_formula_is_matched_by_the_full_name_brew_prints(self):
        info = {"formulae": [formula("tf", full_name="me/tap/tf", tap="me/tap"), formula("x", deps=["me/tap/tf"], requested=True)],
                "casks": [cask("tc", full_token="me/tap/tc"), cask("y", depends_on={"cask": ["me/tap/tc"]})]}
        outdated = {"formulae": [{"name": "me/tap/tf", "current_version": "2.0"}],
                    "casks": [{"name": "tc", "current_version": "3.0"}]}
        p = {r["id"]: r for r in brewdash.build_model(info, outdated, [], [], ["me/tap/tf"])["packages"]}
        self.assertEqual((p["f:tf"]["outdated"], p["f:tf"]["latest"], p["f:tf"]["orphan"]), (True, "2.0", True))
        self.assertEqual(p["f:x"]["deps"], ["f:tf"])
        self.assertTrue(p["c:tc"]["outdated"])
        self.assertEqual(p["c:y"]["deps"], ["c:tc"])

    def test_taps_list_their_installed_members_and_tolerate_null_lists(self):
        taps = {t["name"]: t for t in model()["taps"]}
        self.assertEqual(taps["homebrew/core"]["installed"],
                         ["f:app", "f:icu@2", "f:libA", "f:libB", "f:libC", "f:old", "f:python", "f:stray", "f:tool"])
        self.assertEqual(taps["homebrew/core"]["formula_count"], 3)
        self.assertEqual(taps["me/tap"]["installed"], [])
        self.assertEqual(taps["me/tap"]["cask_count"], 0)

    def test_cask_with_null_tap_is_in_no_tap(self):
        taps = model()["taps"]
        self.assertFalse(any("c:font" in t["installed"] for t in taps))
        self.assertIsNone(by_id()["c:font"]["tap"])


def advice_of(formulae=(), casks=(), outdated=None):
    info = {"formulae": list(formulae), "casks": list(casks)}
    pkgs = {p["id"]: p for p in brewdash.build_model(info, outdated or {}, [], [], [])["packages"]}
    return pkgs


def reqs(entry):
    return [a["req"] for a in entry["actions"]]


def out(*names, kind="formulae"):
    return {kind: [{"name": n, "current_version": "9.0"} for n in names]}


DEPRECATED = {"deprecated": True, "deprecation_reason": "versioned_formula"}


def only(pkgs, pid):
    [entry] = pkgs[pid]["advice"]
    return entry


class AdviceTest(unittest.TestCase):
    def test_healthy_package_gets_no_advice(self):
        self.assertEqual(advice_of([formula("ok")])["f:ok"]["advice"], [])

    def test_outdated_package_is_offered_an_upgrade(self):
        a = only(advice_of([formula("x")], outdated=out("x")), "f:x")
        self.assertEqual((a["title"], a["text"]), ("Update available: 1.0 → 9.0", "Nothing blocks the upgrade."))
        self.assertEqual(reqs(a), [{"action": "upgrade", "targets": ["f:x"]}])

    def test_pinned_outdated_package_is_offered_an_unpin_not_an_upgrade(self):
        a = only(advice_of([formula("x", pinned=True)], outdated=out("x")), "f:x")
        self.assertEqual((a["title"], a["text"]),
                         ("Pinned at 1.0; brew upgrade skips it", "Unpin it first to upgrade to 9.0."))
        self.assertEqual(reqs(a), [{"action": "unpin", "targets": ["f:x"]}])

    def test_disabled_outdated_cask_gets_one_entry_without_an_upgrade(self):
        c = cask("drv", disabled=True, disable_date="2026-09-01", disable_reason="fails_gatekeeper_check")
        a = only(advice_of(casks=[c], outdated=out("drv", kind="casks")), "c:drv")
        self.assertEqual(a["title"], "Disabled by Homebrew: it fails the macOS Gatekeeper check")
        self.assertEqual(a["text"],
                         "Homebrew disabled it on 2026-09-01: brew will not install or upgrade it any more. "
                         "The installed copy stays until you remove it. "
                         "Version 9.0 exists, but not through brew. "
                         "Nothing installed depends on it; remove it if you no longer use the app.")
        self.assertEqual(reqs(a), [{"action": "uninstall", "targets": ["c:drv"]}])

    def test_disabled_up_to_date_package_without_date_says_neither(self):
        a = only(advice_of([formula("x", requested=True, disabled=True, disable_reason="some_new_reason")]), "f:x")
        self.assertEqual(a["title"], "Disabled by Homebrew: some new reason")
        self.assertEqual(a["text"],
                         "Homebrew disabled it: brew will not install or upgrade it any more. "
                         "The installed copy stays until you remove it. "
                         "Nothing installed depends on it, so it can be removed once nothing of yours "
                         "needs this exact version.")

    def test_deprecated_outdated_package_does_not_claim_it_gets_no_updates(self):
        p = advice_of([formula("x", requested=True, deprecated=True, disable_date="2027-01-01")], outdated=out("x"))
        upgrade, dep = p["f:x"]["advice"]
        self.assertEqual(reqs(upgrade), [{"action": "upgrade", "targets": ["f:x"]}])
        self.assertEqual(dep["title"], "Deprecated by Homebrew")
        self.assertEqual(dep["text"],
                         "It still works, and Homebrew plans to drop it: it will be disabled on 2027-01-01. "
                         "Nothing installed depends on it, so it can be removed once nothing of yours "
                         "needs this exact version.")

    def test_newer_installed_versions_are_alternatives_and_nothing_is_installed(self):
        p = advice_of([
            formula("py@3.10", requested=True, versioned_formulae=["py@3.12", "py@3.9", "py@3.11", "py@dev"], **DEPRECATED),
            formula("py@3.12"), formula("py@3.9"), formula("py@3.14", aliases=["py"]),
        ])
        a = only(p, "f:py@3.10")
        self.assertEqual(a["alternatives"], ["f:py@3.12", "f:py@3.14"])
        self.assertEqual(a["text"],
                         "It still works, and Homebrew plans to drop it. "
                         "A newer or replacement package is already installed, so nothing needs to be installed. "
                         "Nothing installed depends on it, so it can be removed once nothing of yours "
                         "needs this exact version.")
        self.assertEqual(reqs(a), [{"action": "uninstall", "targets": ["f:py@3.10"]}])

    def test_base_name_counts_only_when_it_is_another_newer_formula_listed_once(self):
        older_base = advice_of([formula("q@2", versioned_formulae=["q@3"], **DEPRECATED), formula("q@1", aliases=["q"])])
        self.assertEqual(only(older_base, "f:q@2")["alternatives"], [])
        self_base = advice_of([formula("r@2", aliases=["r"], **DEPRECATED)])
        self.assertEqual(only(self_base, "f:r@2")["alternatives"], [])
        listed_twice = advice_of([formula("s@1", versioned_formulae=["s@2"], **DEPRECATED), formula("s@2", aliases=["s"])])
        self.assertEqual(only(listed_twice, "f:s@1")["alternatives"], ["f:s@2"])

    def test_without_a_newer_install_the_newest_version_is_offered(self):
        a = only(advice_of([formula("php@8.0", versioned_formulae=["php@8.10", "php@8.4", "php@7.4"], **DEPRECATED)]),
                 "f:php@8.0")
        self.assertEqual(a["alternatives"], [])
        self.assertEqual(a["title"], "Deprecated by Homebrew: it is an old versioned formula")
        self.assertEqual(a["msg"]["title"], ["advice.deprecated.title_why", {"why": ["reason.versioned_formula", {}]}])
        self.assertEqual(a["text"],
                         "It still works, and Homebrew plans to drop it. "
                         "No supported newer version is installed; the newest versioned one is php@8.10. "
                         "Nothing installed depends on it and it was installed only as a dependency, "
                         "so it is safe to remove.")
        self.assertEqual(reqs(a), [{"action": "install", "kind": "formula", "name": "php@8.10"},
                                   {"action": "uninstall", "targets": ["f:php@8.0"]}])

    def test_deprecated_newer_install_is_not_an_alternative(self):
        p = advice_of([formula("php@8.0", versioned_formulae=["php@8.1", "php@8.4"], **DEPRECATED),
                       formula("php@8.1", **DEPRECATED)])
        a = only(p, "f:php@8.0")
        self.assertEqual(a["alternatives"], [])
        self.assertEqual(a["text"],
                         "It still works, and Homebrew plans to drop it. "
                         "The newer versions installed are deprecated or disabled too. "
                         "No supported newer version is installed; the newest versioned one is php@8.4. "
                         "Nothing installed depends on it and it was installed only as a dependency, "
                         "so it is safe to remove.")
        self.assertEqual(reqs(a)[0], {"action": "install", "kind": "formula", "name": "php@8.4"})

    def test_disabled_newer_install_is_not_an_alternative(self):
        p = advice_of([formula("php@8.0", versioned_formulae=["php@8.1"], **DEPRECATED),
                       formula("php@8.1", disabled=True)])
        self.assertEqual(p["f:php@8.0"]["advice"][0]["alternatives"], [])

    def test_no_version_is_offered_that_is_older_than_an_installed_newer_one(self):
        p = advice_of([formula("php@8.0", versioned_formulae=["php@8.1", "php@8.3"], **DEPRECATED),
                       formula("php@8.3", **DEPRECATED)])
        self.assertEqual([r["action"] for r in reqs(p["f:php@8.0"]["advice"][0])], ["uninstall"])

    def test_unversioned_base_formula_counts_as_the_newer_version(self):
        p = advice_of([formula("php@8.0", versioned_formulae=["php@8.4"], **DEPRECATED), formula("php")])
        a = only(p, "f:php@8.0")
        self.assertEqual(a["alternatives"], ["f:php"])
        self.assertEqual([r["action"] for r in reqs(a)], ["uninstall"])

    def test_installed_replacement_is_an_alternative(self):
        a = only(advice_of([formula("a", deprecated=True, deprecation_replacement_formula="b"), formula("b")]), "f:a")
        self.assertEqual(a["alternatives"], ["f:b"])
        self.assertEqual([r["action"] for r in reqs(a)], ["uninstall"])

    def test_missing_replacement_is_offered_and_never_called_unneeded(self):
        p = advice_of([
            formula("php@8.0", versioned_formulae=["php@8.4", "php@8.5"], deprecated=True,
                    deprecation_replacement_cask="frankenphp"),
            formula("php@8.4"),
        ])
        a = only(p, "f:php@8.0")
        self.assertEqual(a["alternatives"], ["f:php@8.4"])
        self.assertEqual(a["text"],
                         "It still works, and Homebrew plans to drop it. "
                         "Homebrew suggests frankenphp instead, and it is not installed. "
                         "A newer or replacement package is already installed. "
                         "Nothing installed depends on it and it was installed only as a dependency, "
                         "so it is safe to remove.")
        self.assertEqual(reqs(a), [{"action": "install", "kind": "cask", "name": "frankenphp"},
                                   {"action": "uninstall", "targets": ["f:php@8.0"]}])

    def test_missing_replacement_is_offered_instead_of_the_newest_version(self):
        a = only(advice_of([formula("php@8.0", versioned_formulae=["php@8.4"], deprecated=True,
                                    deprecation_replacement_formula="php")]), "f:php@8.0")
        self.assertEqual([r.get("name") for r in reqs(a)], ["php", None])

    def test_deprecated_cask_is_not_matched_against_formula_versions(self):
        a = only(advice_of(casks=[cask("app@1", versioned_formulae=["app@2"], **DEPRECATED)]), "c:app@1")
        self.assertEqual(a["text"], "It still works, and Homebrew plans to drop it. "
                                    "Nothing installed depends on it; remove it if you no longer use the app.")

    def test_deprecated_package_needed_only_indirectly_is_not_called_unused(self):
        p = advice_of([formula("ssl@1", **DEPRECATED), formula("ytdl", requested=True, indirect=["ssl@1"])])
        self.assertIn("1 installed package(s) still need it", only(p, "f:ssl@1")["text"])

    def test_deprecated_package_needed_indirectly_says_so_and_still_offers_the_uninstall(self):
        p = advice_of([formula("ssl@1", **DEPRECATED), formula("py", deps=["ssl@1"]),
                       formula("ytdl", requested=True, deps=["py"], indirect=["ssl@1"])])
        a = only(p, "f:ssl@1")
        self.assertEqual(a["text"], "It still works, and Homebrew plans to drop it. 2 installed package(s) still "
                                    "need it, directly or through another dependency. Uninstall offers to remove "
                                    "them with it, or to remove it anyway and break them.")
        self.assertEqual(reqs(a), [{"action": "uninstall", "targets": ["f:ssl@1"]}])

    def test_missing_dependency_names_other_installed_versions_and_offers_its_install(self):
        a = only(advice_of([formula("tool", deps=["jdk@21"]), formula("jdk"), formula("jdk@25")]), "f:tool")
        self.assertEqual(a["title"], "Missing dependency: jdk@21")
        self.assertEqual(a["text"], "tool was installed against jdk@21, which is not installed now. "
                                    "Another version of it is installed, but this package needs exactly that one. "
                                    "Installing it should repair tool; if brew no longer has it, remove tool instead.")
        self.assertEqual(a["alternatives"], ["f:jdk", "f:jdk@25"])
        self.assertEqual(reqs(a), [{"action": "install", "kind": "formula", "name": "jdk@21"}])

    def test_missing_dependency_of_an_upgradable_package_offers_the_upgrade_first(self):
        p = advice_of([formula("tool", deps=["gone"])], outdated=out("tool"))
        missing = [a for a in p["f:tool"]["advice"] if a["title"].startswith("Missing")]
        self.assertEqual(missing[0]["text"], "tool was installed against gone, which is not installed now. "
                                             "Upgrading brings in whatever the new version needs; otherwise install it.")
        self.assertEqual(reqs(missing[0]), [{"action": "upgrade", "targets": ["f:tool"]},
                                            {"action": "install", "kind": "formula", "name": "gone"}])

    def test_missing_dependency_of_a_pinned_or_disabled_package_offers_no_upgrade(self):
        p = advice_of([formula("pin", deps=["gone"], pinned=True), formula("dis", deps=["gone"], disabled=True)],
                      outdated=out("pin", "dis"))
        for pid in ("f:pin", "f:dis"):
            missing = [a for a in p[pid]["advice"] if a["title"].startswith("Missing")]
            self.assertEqual(reqs(missing[0]), [{"action": "install", "kind": "formula", "name": "gone"}], pid)
            self.assertNotIn("upgrad", missing[0]["text"].lower(), pid)

    def test_missing_cask_dependency_is_installed_as_a_cask(self):
        p = advice_of(casks=[cask("a", depends_on={"cask": ["b"], "formula": ["f"]})])
        kinds = {a["title"]: reqs(a)[-1]["kind"] for a in p["c:a"]["advice"]}
        self.assertEqual(kinds, {"Missing dependency: b": "cask", "Missing dependency: f": "formula"})

    def test_every_sentence_advise_can_write_appears_in_an_advice_test(self):
        import ast
        import re
        fragments = {f.strip() for k, v in brewdash.MESSAGES.items() if k.startswith(("advice.", "action."))
                     for f in re.split(r"\{\w+\}", v) if " " in f.strip()}
        test_tree = ast.parse(Path(__file__).read_text())
        [cls] = [n for n in test_tree.body if isinstance(n, ast.ClassDef) and n.name == "AdviceTest"]
        expected = [n.value for n in ast.walk(cls) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertGreaterEqual(len(fragments), 20)
        unpinned = sorted(f for f in fragments if not any(f in e for e in expected))
        self.assertEqual(unpinned, [])

    def test_every_advice_action_is_a_request_plan_job_accepts(self):
        p = advice_of([
            formula("x", pinned=True), formula("y"), formula("tool", deps=["gone"]),
            formula("php@8.0", versioned_formulae=["php@8.4"], **DEPRECATED),
            formula("c", disabled=True, disable_replacement_cask="c-app"),
        ], casks=[cask("drv", disabled=True)], outdated={**out("x", "y", "tool"), **out("drv", kind="casks")})
        all_reqs = [r for rec in p.values() for a in rec["advice"] for r in reqs(a)]
        self.assertGreaterEqual(len({r["action"] for r in all_reqs}), 4)
        for r in all_reqs:
            brewdash.plan_job(r, p)

    def test_every_advice_carries_its_text_as_messages_too(self):
        p = advice_of([
            formula("x", pinned=True), formula("y"), formula("tool", deps=["gone"]),
            formula("php@8.0", versioned_formulae=["php@8.4"], **DEPRECATED),
            formula("c", disabled=True, disable_replacement_cask="c-app"),
        ], casks=[cask("drv", disabled=True)], outdated={**out("x", "y", "tool"), **out("drv", kind="casks")})
        entries = [a for rec in p.values() for a in rec["advice"]]
        actions = [x for a in entries for x in a["actions"]]
        self.assertGreaterEqual(len(entries), 6)
        self.assertGreaterEqual(len(actions), 6)
        for a in entries:
            self.assertEqual(brewdash.render(a["msg"]["title"]), a["title"])
            self.assertEqual(brewdash.render_all(a["msg"]["text"]), a["text"])
        for x in actions:
            self.assertEqual(brewdash.render(x["msg"]), x["label"])


class RemovalImpactTest(unittest.TestCase):
    def test_installed_dependents_outside_the_selection_block_removal(self):
        self.assertEqual(brewdash.removal_impact(by_id(), ["f:libC"])["blockers"], ["f:app", "f:libA", "f:tool"])

    def test_an_indirect_runtime_dependent_blocks_removal_like_brew_does(self):
        info = {"formulae": [formula("ssl@1"), formula("py", deps=["ssl@1"]),
                             formula("ytdl", requested=True, deps=["py"], indirect=["ssl@1"])], "casks": []}
        p = {r["id"]: r for r in brewdash.build_model(info, {}, [], [], [])["packages"]}
        self.assertEqual(p["f:ssl@1"]["dependents"], ["f:py"])
        self.assertEqual(p["f:ssl@1"]["required_by"], ["f:py", "f:ytdl"])
        self.assertEqual(brewdash.removal_impact(p, ["f:ssl@1"])["blockers"], ["f:py", "f:ytdl"])

    def test_a_dependency_still_needed_indirectly_is_not_a_new_orphan(self):
        info = {"formulae": [formula("ssl@1"), formula("py", deps=["ssl@1"]),
                             formula("ytdl", requested=True, deps=["py"], indirect=["ssl@1"])], "casks": []}
        p = {r["id"]: r for r in brewdash.build_model(info, {}, [], [], [])["packages"]}
        self.assertEqual(brewdash.removal_impact(p, ["f:py"])["new_orphans"], [])

    def test_blockers_include_what_blocks_the_blockers(self):
        info = {"formulae": [formula("a"), formula("b", deps=["a"]), formula("c", requested=True, deps=["b"])],
                "casks": []}
        p = {r["id"]: r for r in brewdash.build_model(info, {}, [], [], [])["packages"]}
        self.assertEqual(brewdash.removal_impact(p, ["f:a"])["blockers"], ["f:b", "f:c"])

    def test_a_package_never_requires_itself(self):
        info = {"formulae": [formula("a", indirect=["a"])], "casks": []}
        [rec] = brewdash.build_model(info, {}, [], [], [])["packages"]
        self.assertEqual(rec["required_by"], [])

    def test_dependents_inside_the_selection_do_not_block(self):
        self.assertEqual(brewdash.removal_impact(by_id(), ["f:libC", "f:libA", "f:tool"])["blockers"], ["f:app"])

    def test_removing_the_only_dependent_orphans_its_deps_transitively(self):
        # libA and libB lose their only dependent; icu@2 follows libB. libC keeps tool.
        impact = brewdash.removal_impact(by_id(), ["f:app"])
        self.assertEqual(impact, {"blockers": [], "new_orphans": ["f:icu@2", "f:libA", "f:libB"]})

    def test_transitive_orphan_once_every_dependent_is_gone(self):
        impact = brewdash.removal_impact(by_id(), ["f:app", "f:tool"])
        self.assertEqual(impact["new_orphans"], ["f:icu@2", "f:libA", "f:libB", "f:libC"])

    def test_requested_formulae_and_existing_orphans_are_never_new_orphans(self):
        self.assertEqual(brewdash.removal_impact(by_id(), ["c:gui"])["new_orphans"], ["f:python"])
        info = {"formulae": [formula("a", deps=["b"], requested=True), formula("b", requested=True)], "casks": []}
        p = {r["id"]: r for r in brewdash.build_model(info, {}, [], [], [])["packages"]}
        self.assertEqual(brewdash.removal_impact(p, ["f:a"])["new_orphans"], [])
        self.assertNotIn("f:stray", brewdash.removal_impact(by_id(), ["f:app"])["new_orphans"])


class ParseTest(unittest.TestCase):
    AUTOREMOVE = """Warning: Calling string comparison format for `depends_on macos:` is deprecated!
  /opt/homebrew/Library/Taps/x/Casks/y.rb:10

==> Would autoremove 3 unneeded formulae:
assimp
gumbo-parser
hunspell
"""

    def test_autoremove_lists_names_after_the_header_only(self):
        self.assertEqual(brewdash.parse_autoremove(self.AUTOREMOVE), ["assimp", "gumbo-parser", "hunspell"])

    def test_autoremove_single_formula_header(self):
        self.assertEqual(brewdash.parse_autoremove("==> Would autoremove 1 unneeded formula:\nx\n"), ["x"])

    def test_autoremove_stops_at_next_section(self):
        self.assertEqual(brewdash.parse_autoremove("==> Would autoremove 1 unneeded formula:\nx\n==> Other\ny\n"), ["x"])

    def test_autoremove_with_nothing_to_remove(self):
        self.assertEqual(brewdash.parse_autoremove(""), [])

    def test_cleanup_reclaim(self):
        text = "Would remove: /a (1MB)\n==> This operation would free approximately 599MB of disk space.\n"
        self.assertEqual(brewdash.parse_cleanup(text), "599MB")
        self.assertIsNone(brewdash.parse_cleanup("nothing here"))

    def test_du_maps_paths_to_ids_and_sums_app_bundles(self):
        text = "100\t/p/Cellar/a\n7\t/p/Caskroom/g\n900\t/Applications/G.app\n5\t/unknown\n"
        ids = {"/p/Cellar/a": "f:a", "/p/Caskroom/g": "c:g", "/Applications/G.app": "c:g"}
        self.assertEqual(brewdash.parse_du(text, ids), {"f:a": 100, "c:g": 907})


class PlanJobTest(unittest.TestCase):
    def test_uninstall_can_ignore_dependents_for_formulae_only(self):
        title, cmds, snap = brewdash.plan_job({"action": "uninstall", "targets": ["f:libC", "c:gui"],
                                               "ignore_dependencies": True}, by_id())
        self.assertEqual(title, "uninstall libC, gui, ignoring dependents")
        self.assertEqual(cmds, [["brew", "uninstall", "--ignore-dependencies", "--formula", "libC"],
                                ["brew", "uninstall", "--cask", "gui"]])
        self.assertTrue(snap)
        with self.assertRaises(ValueError):
            brewdash.plan_job({"action": "uninstall", "targets": ["f:libC"], "ignore_dependencies": "yes"}, by_id())

    def test_uninstall_splits_formulae_and_casks_and_snapshots(self):
        title, cmds, snap = brewdash.plan_job({"action": "uninstall", "targets": ["f:libA", "c:gui", "f:tool"]}, by_id())
        self.assertEqual(cmds, [["brew", "uninstall", "--formula", "libA", "tool"], ["brew", "uninstall", "--cask", "gui"]])
        self.assertTrue(snap)
        self.assertEqual(title, "uninstall libA, tool, gui")

    def test_single_uninstall_still_snapshots(self):
        self.assertTrue(brewdash.plan_job({"action": "uninstall", "targets": ["f:libA"]}, by_id())[2])

    def test_pin_one_does_not_snapshot_pin_many_does(self):
        _, cmds, snap = brewdash.plan_job({"action": "pin", "targets": ["c:gui"]}, by_id())
        self.assertEqual((cmds, snap), ([["brew", "pin", "--cask", "gui"]], False))
        self.assertTrue(brewdash.plan_job({"action": "unpin", "targets": ["f:libA", "f:tool"]}, by_id())[2])

    def test_upgrade_targets(self):
        self.assertEqual(brewdash.plan_job({"action": "upgrade", "targets": ["f:libA"]}, by_id())[1],
                         [["brew", "upgrade", "--formula", "libA"]])

    def test_global_actions(self):
        p = by_id()
        self.assertEqual(brewdash.plan_job({"action": "upgrade_all"}, p)[1:], ([["brew", "upgrade"]], True))
        self.assertEqual(brewdash.plan_job({"action": "update"}, p)[1:], ([["brew", "update"]], False))
        self.assertEqual(brewdash.plan_job({"action": "autoremove"}, p)[1:], ([["brew", "autoremove"]], True))
        self.assertEqual(brewdash.plan_job({"action": "cleanup"}, p)[1:], ([["brew", "cleanup"]], False))

    def test_unknown_or_empty_targets_are_rejected(self):
        for req in ({"action": "uninstall", "targets": ["f:nope"]}, {"action": "uninstall", "targets": []},
                    {"action": "uninstall", "targets": "f:libA"}, {"action": "rm -rf"}):
            with self.subTest(req=req), self.assertRaises(ValueError):
                brewdash.plan_job(req, by_id())

    def test_install_validates_name_and_kind(self):
        self.assertEqual(brewdash.plan_job({"action": "install", "kind": "cask", "name": "iterm2"}, by_id())[1],
                         [["brew", "install", "--cask", "iterm2"]])
        self.assertEqual(brewdash.plan_job({"action": "install", "kind": "formula", "name": "me/tap/x@1.2"}, by_id())[1],
                         [["brew", "install", "--formula", "me/tap/x@1.2"]])
        for req in ({"name": "--force", "kind": "formula"}, {"name": "a b", "kind": "formula"},
                    {"name": "a;rm", "kind": "formula"}, {"name": "", "kind": "formula"},
                    {"name": "jq", "kind": "bottle"}):
            with self.subTest(req=req), self.assertRaises(ValueError):
                brewdash.plan_job({"action": "install", **req}, by_id())

    def test_service_needs_one_formula_with_a_service_and_a_known_op(self):
        p = by_id()
        self.assertEqual(brewdash.plan_job({"action": "service", "op": "stop", "targets": ["f:tool"]}, p)[1],
                         [["brew", "services", "stop", "tool"]])
        self.assertEqual(brewdash.plan_job_msg({"action": "service", "op": "stop", "targets": ["f:tool"]}, p)[0],
                         ["plan.service", {"op": ["service.stop", {}], "name": "tool"}])
        self.assertEqual(brewdash.plan_job({"action": "service", "op": "stop", "targets": ["f:tool"]}, p)[0],
                         "service stop tool")
        self.assertGreaterEqual(len(brewdash.SERVICE_OPS), 3)
        for op in brewdash.SERVICE_OPS:
            with self.subTest(op=op):
                self.assertEqual(brewdash.plan_job({"action": "service", "op": op, "targets": ["f:tool"]}, p)[0],
                                 f"service {op} tool")
        for req in ({"op": "kill", "targets": ["f:tool"]}, {"op": "start", "targets": ["f:libA"]},
                    {"op": "start", "targets": ["c:gui"]}, {"op": "start", "targets": ["f:tool", "f:libA"]}):
            with self.subTest(req=req), self.assertRaises(ValueError):
                brewdash.plan_job({"action": "service", **req}, p)


class AuthorizeTest(unittest.TestCase):
    def test_api_needs_matching_host_and_token(self):
        ok = {"Host": "127.0.0.1:9000", "X-Brewdash-Token": "s3cret"}
        self.assertTrue(brewdash.authorize("/api/state", ok, "s3cret", 9000))
        self.assertTrue(brewdash.authorize("/api/state", {**ok, "Host": "localhost:9000"}, "s3cret", 9000))
        self.assertFalse(brewdash.authorize("/api/state", {**ok, "X-Brewdash-Token": "wrong"}, "s3cret", 9000))
        self.assertFalse(brewdash.authorize("/api/state", {"Host": "127.0.0.1:9000"}, "s3cret", 9000))
        self.assertFalse(brewdash.authorize("/api/state", {**ok, "Host": "evil.test:9000"}, "s3cret", 9000))
        self.assertFalse(brewdash.authorize("/api/state", {**ok, "Host": "127.0.0.1:9001"}, "s3cret", 9000))

    def test_static_files_need_host_but_not_token(self):
        self.assertTrue(brewdash.authorize("/app.js", {"Host": "127.0.0.1:9000"}, "s3cret", 9000))
        self.assertFalse(brewdash.authorize("/app.js", {"Host": "rebind.test:9000"}, "s3cret", 9000))


class FakeBrew:
    """Answers brew calls from the fixtures; `scripts` maps a command prefix to a shell script for popen."""

    def __init__(self, scripts=None, prefix="/nonexistent", info=INFO, raises=(), cache="/nonexistent", casks=()):
        self.scripts = scripts or {}
        self.cache = cache
        self.casks = {c["token"]: c for c in casks}
        self.popened = []
        self.prefix = prefix
        self.info = info
        self.raises = raises

    def run(self, *args, timeout=300):
        out = {("autoremove", "--dry-run"): ParseTest.AUTOREMOVE, ("--repository",): "/nonexistent",
               ("--prefix",): self.prefix, ("--cache",): self.cache, ("cleanup", "-n"): "free approximately 1MB of disk space",
               ("doctor",): "Warning: something", ("search", "--formula", "jq"): "jq\njq-lsp\nme/tap/tool\n",
               ("search", "--cask", "jq"): "jqbook\n", ("deps", "--formula", "newpkg"): "libC\nfresh\n",
               ("search", "--formula", "gui"): "gui\n", ("search", "--cask", "gui"): "gui\nme/tap/font\n"}
        rc = 1 if args == ("doctor",) else 0
        return rc, out.get(args, ""), ""

    def json(self, *args):
        # Like brew: a tap-qualified argument comes back with a short name/token.
        if args[0] == "info" and "--formula" in args:
            return {"formulae": [formula(args[-1].split("/")[-1], full_name=args[-1], versions={"stable": "4.2"})], "casks": []}
        if args[0] == "info" and "--cask" in args and self.casks:
            tokens = args[args.index("--cask") + 1:]
            if any(t not in self.casks for t in tokens):
                raise RuntimeError(f"Cask '{tokens[0]}' is unavailable")
            return {"formulae": [], "casks": [self.casks[t] for t in tokens]}
        if args[0] == "info" and "--cask" in args:
            return {"formulae": [], "casks": [cask(args[-1].split("/")[-1], full_token=args[-1], version="9.0")]}
        if args[0] == "info":
            return self.info
        return {"outdated": OUTDATED, "services": SERVICES, "tap-info": TAPS}[args[0]]

    def popen(self, cmd):
        self.popened.append(cmd)
        if tuple(cmd[1:2]) in self.raises:
            raise OSError(f"cannot start {cmd}")
        script = next((s for prefix, s in self.scripts.items() if tuple(cmd[1:1 + len(prefix)]) == prefix), "true")
        return subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True)


NOT_SIGNED = "/A: code object is not signed at all\n"


def no_listeners_unsigned(argv, timeout=60):
    """Stands in for lsof and codesign, so no test reads this Mac's processes or apps."""
    return (1, "", NOT_SIGNED if argv[0] == brewdash.CODESIGN else "")


EMPTY_HOME = tempfile.TemporaryDirectory()


def store_of(*args, **kw):
    """A Store that reads none of this Mac's home, apps, processes or signatures."""
    return brewdash.Store(*args, **{"sysrun": no_listeners_unsigned, "home": EMPTY_HOME.name, "app_dirs": [], **kw})


def wait_for(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_job(self, brew, title, cmds, snapshot):
        store = store_of(brew)
        runner = brewdash.Runner(brew, store, self.state)
        job = runner.submit(title, cmds, snapshot)
        return wait_for(lambda: job if job["status"] in ("succeeded", "failed") else None), runner

    def test_snapshot_is_written_before_the_action_and_recorded(self):
        brew = FakeBrew()
        job, runner = self.run_job(brew, "uninstall x", [["brew", "uninstall", "--formula", "x"]], True)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual([c[:3] for c in brew.popened], [["brew", "bundle", "dump"], ["brew", "uninstall", "--formula"]])
        self.assertTrue(job["snapshot"].startswith(str(self.state / "brewfiles" / "Brewfile-")))
        self.assertIn(f"--file={job['snapshot']}", brew.popened[0])
        self.assertEqual(runner.history()[0]["snapshot"], job["snapshot"])

    def test_history_is_newest_first(self):
        brew = FakeBrew()
        store = store_of(brew)
        runner = brewdash.Runner(brew, store, self.state)
        runner.submit("first", [["brew", "a"]], False)
        last = runner.submit("second", [["brew", "b"]], False)
        wait_for(lambda: last["status"] == "succeeded" and len(runner.history()) == 2)
        self.assertEqual([h["title"] for h in runner.history()], ["second", "first"])

    def test_failed_snapshot_stops_the_action(self):
        brew = FakeBrew({("bundle", "dump"): "exit 3"})
        job, _ = self.run_job(brew, "uninstall x", [["brew", "uninstall", "--formula", "x"]], True)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(len(brew.popened), 1)

    def test_output_is_streamed_into_the_log_and_failure_stops_later_commands(self):
        brew = FakeBrew({("upgrade", "--formula"): "echo one; echo two; exit 1"})
        job, runner = self.run_job(brew, "upgrade", [["brew", "upgrade", "--formula", "a"], ["brew", "upgrade", "--cask", "b"]], False)
        self.assertEqual(job["log"], ["$ brew upgrade --formula a", "one", "two"])
        self.assertEqual((job["status"], job["exit_code"], job["needs_terminal"]), ("failed", 1, False))
        self.assertEqual(len(brew.popened), 1)
        self.assertEqual(runner.history()[0]["status"], "failed")

    def test_sudo_prompt_failure_asks_for_a_terminal(self):
        brew = FakeBrew({("uninstall",): "echo 'sudo: a terminal is required to read the password'; exit 1"})
        job, _ = self.run_job(brew, "uninstall", [["brew", "uninstall", "--cask", "a"]], False)
        self.assertTrue(job["needs_terminal"])

    def test_jobs_run_one_at_a_time_in_order(self):
        brew = FakeBrew({("a",): "sleep 0.3; echo a", ("b",): "echo b"})
        store = store_of(brew)
        runner = brewdash.Runner(brew, store, self.state)
        ja = runner.submit("a", [["brew", "a"]], False)
        jb = runner.submit("b", [["brew", "b"]], False)
        self.assertEqual((ja["id"], jb["id"]), (1, 2))
        wait_for(lambda: ja["status"] == "running")
        self.assertEqual(jb["status"], "queued")
        wait_for(lambda: jb["status"] == "succeeded")
        self.assertLess(ja["finished"], jb["finished"])

    def test_view_returns_the_log_only_for_requested_jobs(self):
        brew = FakeBrew({("x",): "echo l1; echo l2"})
        job, runner = self.run_job(brew, "x", [["brew", "x"]], False)
        [v] = runner.view({job["id"]})
        self.assertEqual((v["log"], v["log_len"]), (["$ brew x", "l1", "l2"], 3))
        [v] = runner.view(set())
        self.assertEqual((v["log"], v["log_len"]), ([], 3))

    def test_worker_survives_an_exception_and_runs_the_next_job(self):
        brew = FakeBrew(raises={("boom",)})
        store = store_of(brew)
        runner = brewdash.Runner(brew, store, self.state)
        bad = runner.submit("bad", [["brew", "boom"]], False)
        good = runner.submit("good", [["brew", "fine"]], False)
        wait_for(lambda: good["status"] in ("succeeded", "failed"))
        self.assertEqual((bad["status"], good["status"]), ("failed", "succeeded"))
        self.assertTrue(any(l.startswith("brewdash error: OSError") for l in bad["log"]))

    def test_unwritable_history_fails_neither_the_job_nor_the_worker(self):
        blocked = self.state / "not-a-dir"
        blocked.write_text("")
        brew = FakeBrew()
        runner = brewdash.Runner(brew, store_of(brew), blocked)
        first = runner.submit("a", [["brew", "a"]], False)
        second = runner.submit("b", [["brew", "b"]], False)
        wait_for(lambda: second["status"] == "succeeded")
        self.assertEqual(first["status"], "succeeded")
        self.assertTrue(any(l.startswith("could not write history") for l in first["log"]))

    def test_store_is_refreshed_after_every_job(self):
        brew = FakeBrew()
        store = store_of(brew)
        runner = brewdash.Runner(brew, store, self.state)
        self.assertIsNone(store.model)
        runner.submit("x", [["brew", "x"]], False)
        wait_for(lambda: store.model)
        self.assertIn("f:libA", store.by_id())

    def test_snapshot_name_carries_the_job_id_and_state_dir_is_private(self):
        self.state = self.state / "existing"
        (self.state / "brewfiles").mkdir(parents=True)
        self.state.chmod(0o755)
        (self.state / "brewfiles").chmod(0o755)
        job, _ = self.run_job(FakeBrew(), "u", [["brew", "uninstall", "--formula", "x"]], True)
        self.assertTrue(job["snapshot"].endswith(f"-job{job['id']}"))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "brewfiles").stat().st_mode & 0o777, 0o700)


class BrewProcessTest(unittest.TestCase):
    def test_commands_run_in_their_own_session_without_the_launching_terminal(self):
        brew = brewdash.Brew(exe=sys.executable)
        with brew.popen(["brew", "-c", "import os; print(os.getsid(0) == os.getpid())"]) as proc:
            out = proc.stdout.read()
        self.assertEqual(out.strip(), "True")

    def test_run_decodes_non_utf8_output_with_replacement(self):
        brew = brewdash.Brew(exe=sys.executable)
        rc, out, _ = brew.run("-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9')")
        self.assertEqual((rc, out), (0, "caf�"))

    def test_non_utf8_output_does_not_raise(self):
        brew = brewdash.Brew(exe=sys.executable)
        with brew.popen(["brew", "-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9\\n')"]) as proc:
            out = proc.stdout.read()
        self.assertEqual(out, "caf�\n")


class StoreTest(unittest.TestCase):
    def test_a_refresh_requested_mid_read_reads_again(self):
        gate = threading.Event()
        calls = []

        class SlowBrew(FakeBrew):
            def json(self, *args):
                if args[0] == "info":
                    calls.append(1)
                    if len(calls) == 1:
                        gate.wait(5)
                        return {"formulae": [formula("before")], "casks": []}
                return super().json(*args)

        store = store_of(SlowBrew())
        first = threading.Thread(target=store.refresh)
        first.start()
        wait_for(lambda: calls)
        store.refresh()
        self.assertTrue(store.refreshing)
        gate.set()
        first.join(5)
        self.assertEqual(len(calls), 2)
        self.assertIn("f:libA", store.by_id())
        self.assertNotIn("f:before", store.by_id())
        self.assertFalse(store.refreshing)

    def test_sizes_and_health_are_computed_after_a_refresh(self):
        with tempfile.TemporaryDirectory() as prefix:
            (Path(prefix) / "Cellar" / "libA").mkdir(parents=True)
            (Path(prefix) / "Cellar" / "libA" / "f").write_bytes(b"x" * 8192)
            (Path(prefix) / "Caskroom" / "gui").mkdir(parents=True)
            store = store_of(FakeBrew(prefix=prefix))
            store.refresh()
            wait_for(lambda: store.health)
            snap = store.snapshot()
        self.assertEqual(sorted(snap["sizes"]), ["c:gui", "f:libA"])
        self.assertGreaterEqual(snap["sizes"]["f:libA"], 8)
        self.assertEqual(snap["health"], {"doctor_ok": False, "doctor": "Warning: something",
                                          "cleanup_reclaim": "1MB", "cleanup_preview": []})

    def test_slow_part_failure_is_reported_not_swallowed(self):
        class BrokenDoctor(FakeBrew):
            def run(self, *args, timeout=300):
                if args == ("doctor",):
                    raise subprocess.TimeoutExpired("brew doctor", 300)
                return super().run(*args)

        with tempfile.TemporaryDirectory() as prefix:
            (Path(prefix) / "Cellar" / "libA").mkdir(parents=True)
            store = store_of(BrokenDoctor(prefix=prefix))
            store.refresh()
            health = wait_for(lambda: store.health)
        self.assertFalse(health["doctor_ok"])
        self.assertIn("could not run brew doctor", health["doctor"])
        self.assertIn("f:libA", store.sizes)

    def test_slow_result_of_an_older_read_never_replaces_a_newer_one(self):
        first_doctor = threading.Event()
        doctors = []

        class SlowFirstDoctor(FakeBrew):
            def run(self, *args, timeout=300):
                if args == ("doctor",):
                    doctors.append(1)
                    if len(doctors) == 1:
                        first_doctor.wait(5)
                        return 0, "old", ""
                    return 0, "new", ""
                return super().run(*args)

        store = store_of(SlowFirstDoctor())
        store.refresh()
        wait_for(lambda: doctors)
        store.refresh()
        wait_for(lambda: store.health and store.health["doctor"] == "new")
        first_doctor.set()
        time.sleep(0.3)
        self.assertEqual(store.health["doctor"], "new")

    def test_stale_slow_result_does_not_overwrite_a_newer_one(self):
        store = store_of(FakeBrew())
        store.generation = 2
        store._slow({"packages": []}, 1)
        self.assertIsNone(store.health)


class InstallHelpersTest(unittest.TestCase):
    def test_search_marks_installed_by_name_and_by_tap_qualified_name(self):
        pkgs = by_id()
        pkgs["f:tool"] = {**pkgs["f:tool"], "tap": "me/tap"}
        results = brewdash.search(FakeBrew(), "jq", pkgs)
        self.assertEqual(results, [
            {"kind": "formula", "name": "jq", "installed": False},
            {"kind": "formula", "name": "jq-lsp", "installed": False},
            {"kind": "formula", "name": "me/tap/tool", "installed": True},
            {"kind": "cask", "name": "jqbook", "installed": False},
        ])

    def test_search_installed_flag_respects_kind_and_ignores_null_taps(self):
        # gui is installed only as a cask; font is a cask whose tap is null.
        self.assertEqual(brewdash.search(FakeBrew(), "gui", by_id()), [
            {"kind": "formula", "name": "gui", "installed": False},
            {"kind": "cask", "name": "gui", "installed": True},
            {"kind": "cask", "name": "me/tap/font", "installed": False},
        ])
        self.assertNotIn("None/font", brewdash.installed_names(by_id(), "cask"))

    def test_preview_id_is_the_record_id_even_for_a_tap_qualified_name(self):
        self.assertEqual(brewdash.preview(FakeBrew(), "cask", "me/tap/gui", by_id())["id"], "c:gui")
        self.assertEqual(brewdash.preview(FakeBrew(), "formula", "me/tap/libA", by_id())["id"], "f:libA")

    def test_search_rejects_flags_and_shell_text(self):
        for q in ("--open", "a b", "", "x;y"):
            with self.subTest(q=q), self.assertRaises(ValueError):
                brewdash.search(FakeBrew(), q, by_id())

    def test_preview_lists_only_dependencies_not_installed(self):
        pv = brewdash.preview(FakeBrew(), "formula", "newpkg", by_id())
        self.assertEqual((pv["version"], pv["new_deps"]), ("4.2", ["fresh"]))

    def test_preview_cask_uses_cask_version_and_has_no_deps(self):
        pv = brewdash.preview(FakeBrew(), "cask", "newapp", by_id())
        self.assertEqual((pv["version"], pv["new_deps"]), ("9.0", []))

    def test_preview_rejects_bad_input(self):
        for kind, name in (("formula", "--x"), ("bottle", "jq")):
            with self.subTest(kind=kind, name=name), self.assertRaises(ValueError):
                brewdash.preview(FakeBrew(), kind, name, by_id())


def make_app(folder, name, version=None, bundle_id=None, app_store=False, plist=True):
    contents = Path(folder) / f"{name}.app" / "Contents"
    contents.mkdir(parents=True)
    if plist:
        data = {k: v for k, v in (("CFBundleShortVersionString", version), ("CFBundleIdentifier", bundle_id)) if v}
        with open(contents / "Info.plist", "wb") as f:
            plistlib.dump(data, f)
    if app_store:
        (contents / "_MASReceipt").mkdir()
    return str(contents.parent)


UNSIGNED = {"kind": "unsigned", "signer": None}


def app_record(path, name, source, version=None, bundle_id=None, brew_id=None, **extra):
    rec = {"id": "a:" + path, "name": name, "path": path, "version": version, "bundle_id": bundle_id,
           "source": source, "brew_id": brew_id, "cask": None, "cask_version": None, "auto_updates": None}
    rec.update(extra)
    return rec


APP_CASKS = [
    cask("zed", full_token="zed", version="0.9", auto_updates=True, artifacts=[{"app": ["Zed.app"]}, {"binary": ["zed"]}]),
    cask("other", artifacts=[{"app": ["Something Else.app"]}]),
    cask("my-tool", full_token="me/tap/my-tool", version="3.1,42", artifacts=[{"app": ["My Tool.app"]}]),
]


class AppsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_reads_version_and_tells_brew_app_store_and_hand_installs_apart(self):
        zed = make_app(self.dir, "Zed", "0.8", "dev.zed")
        shop = make_app(self.dir, "Shop", "2.0", "com.shop", app_store=True)
        bare = make_app(self.dir, "Bare", plist=False)
        gui = make_app(self.dir, "Gui", "2.0")
        (Path(self.dir) / "notes.txt").write_text("")
        apps = brewdash.scan_apps([self.dir, self.dir + "/missing"], {gui: "c:gui"})
        self.assertEqual(apps, [
            app_record(bare, "Bare", "manual"),
            app_record(gui, "Gui", "brew", "2.0", brew_id="c:gui"),
            app_record(shop, "Shop", "appstore", "2.0", "com.shop"),
            app_record(zed, "Zed", "manual", "0.8", "dev.zed"),
        ])

    def test_a_cask_matches_only_when_it_installs_exactly_that_app(self):
        brew = FakeBrew(casks=APP_CASKS)
        apps = [app_record("/A/Zed.app", "Zed", "manual"), app_record("/A/Other.app", "Other", "manual"),
                app_record("/A/My Tool.app", "My Tool", "manual"), app_record("/A/Unknown.app", "Unknown", "manual"),
                app_record("/A/Gui.app", "Gui", "brew", brew_id="c:gui"), app_record("/A/Shop.app", "Shop", "appstore")]
        calls = []
        real = brew.json
        brew.json = lambda *a: calls.append(a) or real(*a)
        brewdash.match_casks(apps, {"zed", "other", "my-tool", "gui", "shop"}, brew)
        self.assertEqual(calls, [("info", "--json=v2", "--cask", "my-tool", "other", "zed")])
        self.assertEqual([(a["cask"], a["cask_version"], a["auto_updates"]) for a in apps], [
            ("zed", "0.9", True), (None, None, None), ("me/tap/my-tool", "3.1,42", None),
            (None, None, None), (None, None, None), (None, None, None)])

    def test_no_brew_call_when_no_app_name_is_a_cask(self):
        brew = FakeBrew(casks=APP_CASKS)
        brew.json = lambda *a: self.fail(f"unexpected brew call {a}")
        brewdash.match_casks([app_record("/A/Unknown.app", "Unknown", "manual")], {"zed"}, brew)

    def test_store_lists_apps_even_when_cask_matching_fails(self):
        cache = Path(self.dir) / "cache"
        (cache / "api").mkdir(parents=True)
        (cache / "api" / "cask_names.txt").write_text("zed\nghost\n")
        apps_dir = Path(self.dir) / "Applications"
        zed = make_app(apps_dir, "Zed", "0.8")
        ghost = make_app(apps_dir, "Ghost", "1.0")
        store = store_of(FakeBrew(cache=str(cache), casks=APP_CASKS), home=self.dir, app_dirs=[str(apps_dir)])
        store.refresh()
        env = wait_for(lambda: store.apps)
        self.assertEqual(env, {"error": "could not match apps to casks: Cask 'ghost' is unavailable",
                               "apps": [app_record(ghost, "Ghost", "manual", "1.0", signature=UNSIGNED),
                                        app_record(zed, "Zed", "manual", "0.8", signature=UNSIGNED)],
                               "names_known": True})
        self.assertEqual(store.snapshot()["shell"], {"path": [], "tools": []})

    def test_store_matches_casks_and_marks_apps_a_cask_installed(self):
        cache = Path(self.dir) / "cache"
        (cache / "api").mkdir(parents=True)
        (cache / "api" / "cask_names.txt").write_text("zed\ngui\n")
        apps_dir = Path(self.dir) / "Applications"
        zed = make_app(apps_dir, "Zed", "0.8")
        gui = make_app(apps_dir, "Gui", "2.0")
        info = {"formulae": [], "casks": [cask("gui", artifacts=[{"app": ["Gui.app"], "target": gui}])]}
        store = store_of(FakeBrew(cache=str(cache), casks=APP_CASKS, info=info), home=self.dir, app_dirs=[str(apps_dir)])
        store.refresh()
        # gui is in cask_names but not in APP_CASKS: asking brew about it would set an error.
        self.assertEqual(wait_for(lambda: store.apps), {"error": None, "names_known": True, "apps": [
            app_record(gui, "Gui", "brew", "2.0", brew_id="c:gui", signature=UNSIGNED),
            app_record(zed, "Zed", "manual", "0.8", cask="zed", cask_version="0.9", auto_updates=True, signature=UNSIGNED)]})


class ShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(Path(self.tmp.name) / "home")
        self.prefix = str(Path(self.tmp.name) / "brew")
        for d in ("home/bin", "home/.jbang/bin", "brew/opt/helm@2/bin"):
            (Path(self.tmp.name) / d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rc, *lines):
        (Path(self.home) / rc).write_text("\n".join(lines) + "\n")

    def scan(self, names=None, installed=None):
        names = names or {"formula": {"jbang", "nvm", "fzf"}, "cask": set()}
        installed = installed or {"formula": {"fzf"}, "cask": set()}
        return brewdash.scan_shell(self.home, self.prefix, names, installed)

    def entry(self, rc, line, raw, d, exists, issue=None, suggestion=None, alternative=None):
        return {"file": rc, "line": line, "raw": raw, "dir": d, "exists": exists, "issue": issue,
                "suggestion": suggestion, "suggestion_msg": mock.ANY if suggestion else None,
                "alternative": alternative}

    def test_path_entries_get_an_issue_and_a_fix(self):
        h, pre = self.home, self.prefix
        self.write(".zshrc",
                   'export PATH="$HOME/bin:$PATH"',
                   "export API_TOKEN=secret-value",
                   "PATH=/nope/bin:$PATH  # old tool",
                   'export PATH="${HOME}/bin:$PATH"',
                   f'export PATH="{pre}/Cellar/php@8.0/8.0.30/bin:$PATH"',
                   'export PATH="/usr/local/opt/helm@2/bin:/usr/local/opt/gone/bin:$PATH"',
                   "export PATH=~/.jbang/bin:$FOO/bin:$PATH")
        self.write(".bashrc", 'export PATH="$HOME/bin:$PATH"')
        self.assertEqual(self.scan()["path"], [
            self.entry(".zshrc", 1, "$HOME/bin", f"{h}/bin", True),
            self.entry(".zshrc", 3, "/nope/bin", "/nope/bin", False, "missing", "The directory does not exist; this line can go."),
            self.entry(".zshrc", 4, "${HOME}/bin", f"{h}/bin", True, "duplicate", ".zshrc already adds this directory earlier; this line can go."),
            self.entry(".zshrc", 5, f"{pre}/Cellar/php@8.0/8.0.30/bin", f"{pre}/Cellar/php@8.0/8.0.30/bin", False, "pinned",
                       f"Points at one installed version of php@8.0 and breaks on every upgrade. That version is no longer installed. Use {pre}/opt/php@8.0/bin instead."),
            self.entry(".zshrc", 6, "/usr/local/opt/helm@2/bin", "/usr/local/opt/helm@2/bin", False, "old_prefix",
                       f"Homebrew lives in {pre} on this Mac. Use {pre}/opt/helm@2/bin instead."),
            self.entry(".zshrc", 6, "/usr/local/opt/gone/bin", "/usr/local/opt/gone/bin", False, "old_prefix",
                       f"Homebrew lives in {pre} on this Mac. Nothing is installed there either; this line can go."),
            self.entry(".zshrc", 7, "~/.jbang/bin", f"{h}/.jbang/bin", True, alternative={"kind": "formula", "name": "jbang"}),
            self.entry(".zshrc", 7, "$FOO/bin", "$FOO/bin", None),
            self.entry(".bashrc", 1, "$HOME/bin", f"{h}/bin", True),
        ])
        flagged = [e for e in self.scan()["path"] if e["issue"]]
        self.assertEqual(len(flagged), 5)
        for e in flagged:
            self.assertEqual(brewdash.render(e["suggestion_msg"]), e["suggestion"])

    def test_a_synced_folder_ahead_of_the_system_is_a_warning(self):
        moved = Path(self.tmp.name) / "Volumes" / "Dropbox"
        (moved / "bin").mkdir(parents=True)
        (Path(self.home) / "Dropbox").symlink_to(moved)
        (Path(self.home) / "Library" / "Mobile Documents").mkdir(parents=True)
        lower = self.home.replace("/home", "/HOME")
        self.write(".zshrc",
                   'export PATH="$HOME/Dropbox/bin:$PATH"',
                   f'export PATH="{lower}/Dropbox/gone:$PATH"',
                   'export PATH="$HOME/Library/Mobile Documents/x:$PATH"',
                   'export PATH="$PATH:$HOME/Dropbox/bin"',
                   'export PATH="/usr/bin:$HOME/Dropbox/late:/bin"',
                   'export PATH="$HOME/Library/Mobile DocumentsNot/bin:$PATH"',
                   'export PATH="$HOME/Dropbox/bin:$PATH"',
                   'export PATH="$HOME/Library/CloudStorage/GoogleDrive/bin:$PATH"')
        self.assertEqual([(e["line"], e["issue"]) for e in self.scan()["path"]], [
            (1, "synced"), (2, "synced"), (3, "synced"), (4, "duplicate"),
            (5, None), (5, "missing"), (5, None), (6, "missing"), (7, "duplicate"), (8, "synced")])
        self.assertIn("runs in place of the system command", self.scan()["path"][0]["suggestion"])

    def test_a_synced_root_itself_on_the_path_is_a_warning(self):
        (Path(self.home) / "Dropbox").mkdir()
        self.write(".zshrc", 'export PATH="$HOME/Dropbox:$HOME/Library/CloudStorage:$PATH"')
        self.assertEqual([e["issue"] for e in self.scan()["path"]], ["synced", "synced"])

    def test_an_existing_pinned_version_still_gets_the_opt_path(self):
        cellar = Path(self.prefix) / "Cellar" / "node@20" / "20.1" / "bin"
        cellar.mkdir(parents=True)
        self.write(".zprofile", f"export PATH={cellar}:$PATH")
        [e] = self.scan()["path"]
        self.assertEqual((e["issue"], e["exists"], e["suggestion"]), ("pinned", True,
                         f"Points at one installed version of node@20 and breaks on every upgrade. Use {self.prefix}/opt/node@20/bin instead."))

    def test_brew_alternative_only_for_what_brew_has_and_is_not_installed(self):
        self.write(".zshrc", "export PATH=$HOME/.jbang/bin:$PATH")
        names = {"formula": {"jbang"}, "cask": set()}
        self.assertIsNone(self.scan(names, {"formula": {"jbang"}, "cask": set()})["path"][0]["alternative"])
        self.assertIsNone(self.scan({"formula": set(), "cask": set()})["path"][0]["alternative"])

    def test_tools_started_by_the_shell_and_whether_brew_provides_them(self):
        self.write(".zshrc",
                   f'eval "$({self.prefix}/bin/brew shellenv)"',
                   'export NVM_DIR="$HOME/.nvm"',
                   '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"',
                   "source $ZSH/oh-my-zsh.sh",
                   "source <(fzf --zsh)",
                   "# source ~/.pyenv/pyenv init",
                   'alias nvm.sh="echo"')
        self.assertEqual(self.scan()["tools"], [
            {"name": "Homebrew", "file": ".zshrc", "line": 1, "via_brew": True, "alternative": None},
            {"name": "nvm", "file": ".zshrc", "line": 3, "via_brew": False, "alternative": "nvm"},
            {"name": "Oh My Zsh", "file": ".zshrc", "line": 4, "via_brew": False, "alternative": None},
            {"name": "fzf", "file": ".zshrc", "line": 5, "via_brew": True, "alternative": None},
        ])

    def test_no_rc_files_means_nothing_to_show(self):
        self.assertEqual(self.scan(), {"path": [], "tools": []})


PKGS_WITH_TAP = {**by_id(), "f:tool2": {"full_name": "me/tap/tool2", "kind": "formula"}}


class ExportListTest(unittest.TestCase):
    def test_list_item_maps_each_kind_to_its_brewfile_entry(self):
        apps = {"a:/A/Zed.app": {"cask": "zed"}, "a:/A/Bare.app": {"cask": None}}
        taps = {"me/tap"}
        self.assertEqual([brewdash.list_item(i, PKGS_WITH_TAP, apps, taps) for i in
                          ("f:libA", "f:tool2", "c:gui", "a:/A/Zed.app", "t:me/tap")], [
            {"type": "brew", "name": "libA"}, {"type": "brew", "name": "me/tap/tool2"}, {"type": "cask", "name": "gui"},
            {"type": "cask", "name": "zed"}, {"type": "tap", "name": "me/tap"}])
        for bad in ("f:nope", "a:/A/Bare.app", "a:/A/Nope.app", "t:other/tap", "x:libA", "libA"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                brewdash.list_item(bad, PKGS_WITH_TAP, apps, taps)

    def test_brewfile_puts_taps_first_and_trusts_tap_packages(self):
        items = [{"type": "cask", "name": "zed"}, {"type": "brew", "name": "jq"}, {"type": "brew", "name": "me/tap/tool2"},
                 {"type": "tap", "name": "other/tap"}, {"type": "tap", "name": "homebrew/core"},
                 {"type": "cask", "name": "homebrew/cask/iterm2"}, {"type": "brew", "name": "abc"}]
        self.assertEqual(brewdash.brewfile(items), (
            "# Brewfile from brewdash. On the new machine: brew bundle --file=Brewfile\n"
            "\n"
            'tap "me/tap"\n'
            'tap "other/tap"\n'
            'brew "abc"\n'
            'brew "jq"\n'
            'brew "me/tap/tool2", trusted: true\n'
            'cask "homebrew/cask/iterm2", trusted: true\n'
            'cask "zed"\n'))

    def test_list_persists_skips_duplicates_and_removes(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            jq, zed, tap = {"type": "brew", "name": "jq"}, {"type": "cask", "name": "zed"}, {"type": "tap", "name": "me/tap"}
            self.assertEqual(brewdash.ExportList(state).items(), [])
            self.assertEqual(brewdash.ExportList(state).change(add=[jq, zed]), [jq, zed])
            self.assertEqual(brewdash.ExportList(state).change(add=[zed, tap, tap]), [jq, zed, tap])
            self.assertEqual(brewdash.ExportList(state).change(remove=[zed]), [jq, tap])
            self.assertEqual(brewdash.ExportList(state).items(), [jq, tap])
            self.assertEqual(json.loads((state / "list.json").read_text()), {"items": [jq, tap]})

    def test_list_rejects_what_would_break_the_brewfile(self):
        with tempfile.TemporaryDirectory() as d:
            export = brewdash.ExportList(d)
            for bad in ({"type": "mas", "name": "x"}, {"type": "brew", "name": 'x", args: ["evil"]'},
                        {"type": "brew", "name": "-x"}, {"type": "brew"}, {"type": "cask", "name": "a b"}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    export.change(add=[bad])
                with self.subTest(bad=bad, op="remove"), self.assertRaises(ValueError):
                    export.change(remove=[bad])
            self.assertFalse((Path(d) / "list.json").exists())


class AdoptTest(unittest.TestCase):
    APPS = {"a:/A/Zed.app": {"name": "Zed", "source": "manual", "cask": "zed"},
            "a:/A/Shop.app": {"name": "Shop", "source": "appstore", "cask": "shop"},
            "a:/A/Bare.app": {"name": "Bare", "source": "manual", "cask": None},
            "a:/A/Gui.app": {"name": "Gui", "source": "brew", "cask": None}}

    def test_adopt_hands_a_verified_hand_installed_app_to_brew(self):
        self.assertEqual(brewdash.plan_job({"action": "adopt", "app": "a:/A/Zed.app"}, by_id(), self.APPS),
                         ("bring Zed into brew", [["brew", "install", "--cask", "--adopt", "zed"]], False))

    def test_adopt_refuses_app_store_unmatched_brew_and_unknown_apps(self):
        for app in ("a:/A/Shop.app", "a:/A/Bare.app", "a:/A/Gui.app", "a:/A/Nope.app", None):
            with self.subTest(app=app), self.assertRaises(ValueError):
                brewdash.plan_job({"action": "adopt", "app": app}, by_id(), self.APPS)
        with self.assertRaises(ValueError):
            brewdash.plan_job({"action": "adopt", "app": "a:/A/Zed.app"}, by_id())


class TrashTest(unittest.TestCase):
    APPS = {"a:/A/Zed.app": {"name": "Zed", "path": "/A/Zed.app", "source": "manual", "cask": "zed"},
            "a:/A/Shop.app": {"name": "Shop", "path": "/A/Shop.app", "source": "appstore", "cask": None},
            "a:/A/Gui.app": {"name": "Gui", "path": "/A/Gui.app", "source": "brew", "cask": None}}
    SCRIPT = ["osascript", "-e", "on run argv", "-e", "set f to POSIX file (item 1 of argv) as alias",
              "-e", 'tell application "Finder" to delete f', "-e", "end run"]

    def test_trash_hands_a_hand_installed_or_app_store_app_to_finder_by_path(self):
        for app, name in (("a:/A/Zed.app", "Zed"), ("a:/A/Shop.app", "Shop")):
            with self.subTest(app=app):
                self.assertEqual(brewdash.plan_job({"action": "trash", "app": app}, by_id(), self.APPS),
                                 (f"move {name} to the Trash", [[*self.SCRIPT, f"/A/{name}.app"]], False))

    def test_trash_refuses_a_brew_app_and_unknown_apps(self):
        for app in ("a:/A/Gui.app", "a:/A/Nope.app", None):
            with self.subTest(app=app), self.assertRaises(ValueError):
                brewdash.plan_job({"action": "trash", "app": app}, by_id(), self.APPS)


class UntapTest(unittest.TestCase):
    TAPS = {"me/tap", "you/tap", "homebrew/core"}

    def test_untap_runs_one_brew_untap_for_every_named_tap(self):
        self.assertEqual(brewdash.plan_job({"action": "untap", "taps": ["me/tap", "you/tap"]}, by_id(), taps=self.TAPS),
                         ("untap me/tap, you/tap", [["brew", "untap", "me/tap", "you/tap"]], True))

    def test_untap_refuses_unknown_taps_and_malformed_requests(self):
        for taps in (["me/tap", "nope/tap"], [], "me/tap", None, [1]):
            with self.subTest(taps=taps), self.assertRaises(ValueError):
                brewdash.plan_job({"action": "untap", "taps": taps}, by_id(), taps=self.TAPS)
        with self.assertRaises(ValueError):
            brewdash.plan_job({"action": "untap", "taps": ["me/tap"]}, by_id())


class AskpassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_askpass_is_a_private_executable_that_asks_in_a_hidden_dialog(self):
        path = Path(brewdash.write_askpass(self.state))
        self.assertEqual((path, path.stat().st_mode & 0o777, self.state.stat().st_mode & 0o777),
                         (self.state / "askpass", 0o700, 0o700))
        self.assertEqual(path.read_text(), brewdash.ASKPASS)

    def test_askpass_makes_an_existing_open_state_dir_private(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        brewdash.write_askpass(self.state)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def run_askpass(self, osascript_body, *args):
        """Runs the askpass with /usr/bin/osascript swapped for a fake, which no test may run for real."""
        fake = Path(self.tmp.name) / "osascript"
        fake.write_text("#!/bin/sh\n" + osascript_body)
        fake.chmod(0o700)
        self.assertEqual(brewdash.ASKPASS.count("/usr/bin/osascript"), 1)
        script = brewdash.ASKPASS.replace("/usr/bin/osascript", str(fake))
        return subprocess.run(["/bin/sh", "-c", script, "askpass", *args], capture_output=True, text=True)

    def test_askpass_shows_sudos_prompt_and_prints_only_the_typed_text(self):
        p = self.run_askpass('for a in "$@"; do printf "[%s]\\n" "$a"; done', "Password for me:")
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertEqual(p.stdout.splitlines(), [
            "[-e]", "[on run argv]", "[-e]",
            '[text returned of (display dialog (item 1 of argv) default answer "" with hidden answer '
            'with title "brewdash" with icon caution)]',
            "[-e]", "[end run]", "[Password for me:]"])
        self.assertEqual(self.run_askpass('printf "%s" "$7"').stdout, "brewdash needs your password for sudo")

    def test_a_cancelled_dialog_fails_with_the_line_that_shows_the_terminal_hint(self):
        p = self.run_askpass("exit 1", "Password:")
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertRegex(p.stderr, brewdash.SUDO_RE)

    def test_brew_hands_sudo_the_askpass_unless_the_user_set_one(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("SUDO_ASKPASS", None)
            self.assertEqual(brewdash.Brew("/b", askpass="/s/askpass").env["SUDO_ASKPASS"], "/s/askpass")
            self.assertNotIn("SUDO_ASKPASS", brewdash.Brew("/b").env)
            os.environ["SUDO_ASKPASS"] = "/mine"
            self.assertEqual(brewdash.Brew("/b", askpass="/s/askpass").env["SUDO_ASKPASS"], "/mine")

    def test_serve_writes_the_askpass_into_its_state_dir_for_the_real_brew(self):
        made = []
        with mock.patch.object(brewdash, "Brew", side_effect=lambda **kw: made.append(kw) or FakeBrew()):
            server, _, _ = brewdash.serve(port=0, open_browser=False, state_dir=str(self.state), refresh=False)
        server.server_close()
        self.assertEqual(made, [{"askpass": str(self.state / "askpass")}])
        self.assertTrue((self.state / "askpass").exists())


SHA_A, SHA_B, SHA_C, SHA_D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
API = "https://api.github.com/repos/lemiorhan/brewdash"
APP_ROOT = Path("/Applications/brewdash.app/Contents/Resources/brewdash")


def zip_url(tag, name="brewdash-macos.zip"):
    return f"https://github.com/lemiorhan/brewdash/releases/download/{tag}/{name}"


def release(tag, *assets):
    names = assets or ("brewdash.zip.sig", "brewdash-macos.zip")
    return {"tag_name": tag, "assets": [{"name": n, "browser_download_url": zip_url(tag, n)} for n in names]}


def not_found(url):
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class FakeGitHub:
    """Answers the GitHub calls the updater makes: the latest release, the beta prerelease, a tag's commit and a
    compare from SHA_A. A release that is None is not there; `compare` maps a target sha to GitHub's compare status."""
    def __init__(self, stable=release("v0.10.0"), beta=release("beta"), shas=None, compare=None, fail=None):
        self.releases = {f"{API}/releases/latest": stable, f"{API}/releases/tags/beta": beta}
        self.shas = shas or {"v0.10.0": SHA_B, "beta": SHA_C}
        self.compare, self.fail = compare or {}, fail
        self.calls = []

    def __call__(self, url, accept="application/vnd.github+json"):
        self.calls.append((url, accept))
        if self.fail:
            raise self.fail
        if url in self.releases:
            if self.releases[url] is None:
                raise not_found(url)
            return json.dumps(self.releases[url])
        tag = url.removeprefix(f"{API}/commits/")
        if accept == "application/vnd.github.sha" and tag in self.shas:
            return self.shas[tag] + "\n"
        base, _, head = url.removeprefix(f"{API}/compare/").partition("...")
        if url.startswith(f"{API}/compare/") and base == SHA_A:
            return json.dumps({"status": self.compare[head]})
        raise AssertionError(f"unexpected GitHub call {url} {accept}")


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.spawned = []

    def tearDown(self):
        self.tmp.cleanup()

    def updater(self, github=None, installed=None, root=APP_ROOT, spawn=None, mode="browser"):
        self.github = github or FakeGitHub()
        return brewdash.Updater(self.state, installed or {"sha": SHA_A, "tag": "v0.9.0"}, root=root, fetch=self.github,
                                spawn=spawn or (lambda *a: self.spawned.append(a)), mode=mode)

    def test_read_mode_takes_app_or_browser_and_falls_back_to_browser(self):
        d = Path(self.tmp.name)
        self.assertEqual(brewdash.read_mode(d), "browser")
        for written, want in (("app\n", "app"), ("browser\n", "browser"), ("window\n", "browser")):
            (d / "mode").write_text(written)
            self.assertEqual(brewdash.read_mode(d), want, written)

    def test_the_mac_app_installs_the_release_zip_and_the_browser_kind_builds_the_releases_commit(self):
        log = self.state / "update.log"
        for channel, target, zip_, ref in (("stable", SHA_B, zip_url("v0.10.0"), "v0.10.0"),
                                           ("beta", SHA_C, zip_url("beta"), SHA_C)):
            for mode, source in (("app", {"BREWDASH_ZIP": zip_}), ("browser", {"BREWDASH_REF": ref})):
                with self.subTest(channel=channel, mode=mode):
                    self.spawned.clear()
                    u = self.updater(FakeGitHub(compare={target: "ahead"}), mode=mode)
                    u.save({"channel": channel})
                    u.update(manual=False)
                    self.assertEqual(self.spawned, [(APP_ROOT / "install.sh", {**source, "BREWDASH_APP_DIR": "/Applications",
                                                                              "BREWDASH_MODE": mode}, log)])

    def test_read_installed_takes_only_a_full_sha_and_a_vxyz_tag(self):
        root = Path(self.tmp.name)
        cases = [({"sha": SHA_A, "tag": "v1.2.3"}, {"sha": SHA_A, "tag": "v1.2.3"}),
                 ({"sha": SHA_A, "tag": None}, {"sha": SHA_A, "tag": None}),
                 ({"sha": SHA_A, "tag": "master"}, {"sha": SHA_A, "tag": None}),
                 ({"sha": "abc", "tag": "v1.2.3"}, None), (["x"], None)]
        for written, want in cases:
            with self.subTest(written=written):
                (root / "version.json").write_text(json.dumps(written))
                self.assertEqual(brewdash.read_installed(root), want)
        (root / "version.json").write_text("{")
        self.assertIsNone(brewdash.read_installed(root))
        (root / "version.json").unlink()
        self.assertIsNone(brewdash.read_installed(root))

    def test_settings_default_to_stable_and_auto_and_keep_only_valid_values(self):
        u = self.updater()
        self.assertEqual(u.settings(), {"channel": "stable", "auto": True})
        self.state.mkdir()
        (self.state / "settings.json").write_text(json.dumps({"channel": "nightly", "auto": "yes"}))
        self.assertEqual(u.settings(), {"channel": "stable", "auto": True})
        self.assertEqual({k: u.save({"channel": "beta"})[k] for k in ("channel", "auto")}, {"channel": "beta", "auto": True})
        self.assertEqual(u.save({"auto": False})["auto"], False)
        self.assertEqual(json.loads((self.state / "settings.json").read_text()), {"channel": "beta", "auto": False})
        for bad in ({"channel": "nightly"}, {"auto": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                u.save(bad)
        self.assertEqual(brewdash.Updater(self.state, None).settings(), {"channel": "beta", "auto": False})

    def test_stable_is_githubs_latest_release_with_its_tags_commit_and_zip(self):
        u = self.updater(FakeGitHub(compare={SHA_B: "ahead"}))
        view = u.check()
        self.assertEqual((view["latest"], view["relation"], view["error"]),
                         ({"sha": SHA_B, "tag": "v0.10.0", "zip": zip_url("v0.10.0")}, "newer", None))
        self.assertIsNotNone(view["checked"])
        self.assertEqual(self.github.calls[:2], [(f"{API}/releases/latest", "application/vnd.github+json"),
                                                 (f"{API}/commits/v0.10.0", "application/vnd.github.sha")])

    def test_a_new_channel_drops_the_last_check_and_a_new_auto_keeps_it(self):
        u = self.updater(FakeGitHub(compare={SHA_B: "ahead"}))
        u.check()
        self.assertEqual(u.save({"auto": False, "channel": "stable"})["relation"], "newer")
        view = u.save({"channel": "beta"})
        self.assertEqual((view["latest"], view["relation"], view["checked"]), (None, None, None))

    def test_beta_is_the_beta_prerelease_and_names_no_version(self):
        u = self.updater(FakeGitHub(compare={SHA_C: "ahead"}))
        u.save({"channel": "beta"})
        self.assertEqual(u.check()["latest"], {"sha": SHA_C, "tag": None, "zip": zip_url("beta")})
        self.assertEqual(self.github.calls[:2], [(f"{API}/releases/tags/beta", "application/vnd.github+json"),
                                                 (f"{API}/commits/beta", "application/vnd.github.sha")])

    def test_check_reads_githubs_compare_status_and_asks_nothing_for_the_installed_commit(self):
        for status, rel in (("ahead", "newer"), ("behind", "older"), ("diverged", "diverged"), ("identical", "same"),
                            ("odd", "diverged")):
            with self.subTest(status=status):
                self.assertEqual(self.updater(FakeGitHub(compare={SHA_B: status})).check()["relation"], rel)
        u = self.updater(FakeGitHub(shas={"v0.10.0": SHA_A}))
        self.assertEqual(u.check()["relation"], "same")
        self.assertFalse(any("/compare/" in url for url, _ in self.github.calls))

    def test_a_channel_without_a_release_that_has_the_zip_has_nothing_to_install(self):
        cases = {"no release": ("stable", FakeGitHub(stable=None)),
                 "no beta": ("beta", FakeGitHub(beta=None)),
                 "made by hand, no zip": ("stable", FakeGitHub(stable=release("v1.0.0", "notes.txt"))),
                 "a zip by another name": ("beta", FakeGitHub(beta=release("beta", "brewdash-v1.0.0-macos.zip"))),
                 "a tag that is no version": ("stable", FakeGitHub(stable=release("v0.1"))),
                 "a tag that is a path": ("stable", FakeGitHub(stable=release("../../compare/x")))}
        for name, (channel, github) in cases.items():
            with self.subTest(name):
                u = self.updater(github)
                u.save({"channel": channel})
                view = u.update(manual=True)
                self.assertEqual((view["latest"], view["relation"], view["error"], self.spawned), (None, None, None, []))
                self.assertEqual(len(github.calls), 1, github.calls)

    def test_a_github_failure_is_reported_not_raised(self):
        for err in (OSError("offline"), ValueError("bad json"), http.client.IncompleteRead(b"x"),
                    http.client.HTTPException("bad status line"),
                    urllib.error.HTTPError(f"{API}/releases/latest", 403, "rate limit exceeded", {}, None)):
            with self.subTest(err=err):
                view = self.updater(FakeGitHub(fail=err)).update(manual=False)
                self.assertEqual((view["error"], view["latest"], self.spawned), (str(err), None, []))
        u = self.updater(FakeGitHub(shas={"v0.10.0": SHA_B, "beta": "<html>"}))
        u.save({"channel": "beta"})
        self.assertEqual(u.check()["error"], "GitHub did not answer with a commit for beta")
        u = self.updater(FakeGitHub(shas={"v0.10.0": "../../evil"}))
        u.save({"channel": "stable"})
        self.assertEqual(u.check()["error"], "GitHub did not answer with a commit for v0.10.0")
        self.assertFalse(any("/compare/" in url for url, _ in self.github.calls))

    def test_a_failed_installer_is_reported_and_frees_the_update_button(self):
        class Proc:
            code = None
            def poll(self):
                return self.code
            returncode = property(lambda self: self.code)
        procs = []

        def spawn(*a):
            self.spawned.append(a)
            a[2].write_text("==> Downloading x\n\x1b[31mbrewdash: building the app failed.\x1b[0m\n\n")
            procs.append(Proc())
            return procs[-1]
        u = self.updater(FakeGitHub(compare={SHA_B: "ahead"}), spawn=spawn)
        self.state.mkdir()
        self.assertIsNotNone(u.update(manual=True)["started"])
        self.assertIsNotNone(u.update(manual=True)["started"])
        self.assertEqual(len(self.spawned), 1, "a second click spawned a second installer while the first runs")
        procs[0].code = 1
        view = u.view()
        self.assertEqual((view["started"], view["error"]), (None, "install.sh stopped: brewdash: building the app failed."))
        self.assertIsNotNone(u.update(manual=True)["started"])
        self.assertEqual(len(self.spawned), 2)

    def test_a_check_that_outlives_a_channel_switch_keeps_nothing_and_installs_nothing(self):
        github = FakeGitHub(compare={SHA_B: "ahead"})

        def fetch(url, accept="application/vnd.github+json"):
            if url.endswith("/releases/latest"):
                u.save({"channel": "beta"})
            return github(url, accept)
        u = self.updater(fetch)
        view = u.update(manual=False)
        self.assertEqual((view["channel"], view["latest"], view["relation"], view["checked"], self.spawned),
                         ("beta", None, None, None, []))

    def test_an_update_already_under_way_is_not_started_again(self):
        u = self.updater(FakeGitHub(compare={SHA_B: "ahead"}))
        with u.installing:
            view = u.update(manual=True)
        self.assertEqual((view["started"], self.spawned, self.github.calls), (None, [], []))

    def test_the_hourly_check_outlives_a_failure(self):
        with mock.patch.object(brewdash, "UPDATE_FIRST_CHECK_SECONDS", 0), \
                mock.patch.object(brewdash, "UPDATE_CHECK_SECONDS", 0.05):
            u = self.updater(FakeGitHub(compare={SHA_B: "ahead"}),
                             root=Path("/Applications/Mine.app/Contents/Resources/brewdash"))
            u.run_hourly()
            wait_for(lambda: "install.sh replaces brewdash.app" in (u.view()["error"] or ""))
            rounds = lambda: sum(url.endswith("/releases/latest") for url, _ in self.github.calls)
            wait_for(lambda: rounds() >= 3)
            self.assertEqual(self.spawned, [])

    def test_auto_installs_only_newer_and_a_click_also_goes_back_or_aside(self):
        for status, auto, manual in (("ahead", True, True), ("behind", False, True), ("diverged", False, True),
                                     ("identical", False, False)):
            for is_manual, want in ((False, auto), (True, manual)):
                with self.subTest(status=status, manual=is_manual):
                    self.spawned.clear()
                    view = self.updater(FakeGitHub(compare={SHA_B: status})).update(manual=is_manual)
                    self.assertEqual([a[1]["BREWDASH_REF"] for a in self.spawned], ["v0.10.0"] if want else [])
                    self.assertEqual(view["started"] is not None, want)

    def test_update_refuses_a_checkout_an_app_with_another_name_and_an_app_run_where_it_was_downloaded(self):
        with self.assertRaises(ValueError):
            brewdash.Updater(self.state, None).update(manual=True)
        with self.assertRaises(ValueError):
            brewdash.Updater(self.state, None).check()
        for root in (Path("/Applications/Mine.app/Contents/Resources/brewdash"),
                     Path("/private/var/folders/x1/abc/T/AppTranslocation/0A1B-2C3D/d/brewdash.app/Contents/Resources/brewdash")):
            with self.subTest(root=root):
                u = self.updater(FakeGitHub(compare={SHA_B: "ahead"}), root=root, mode="app")
                with self.assertRaises(ValueError):
                    u.update(manual=True)
                self.assertEqual(self.spawned, [])

    def test_an_installer_that_cannot_start_is_reported(self):
        def boom(*a):
            raise OSError("no bash")
        view = self.updater(FakeGitHub(compare={SHA_B: "ahead"}), spawn=boom).update(manual=True)
        self.assertEqual((view["started"], view["error"]), (None, "could not start install.sh: no bash"))

    def test_hourly_check_installs_newer_when_auto_is_on_and_stays_off_for_a_checkout(self):
        with mock.patch.object(brewdash, "UPDATE_FIRST_CHECK_SECONDS", 0), \
                mock.patch.object(brewdash, "UPDATE_CHECK_SECONDS", 0.05):
            off = self.updater(FakeGitHub(compare={SHA_B: "ahead"}))
            off.save({"auto": False})
            off.run_hourly()
            checkout = brewdash.Updater(self.state, None, fetch=lambda *a: self.fail("a checkout asked GitHub"))
            with mock.patch.object(threading, "Thread", side_effect=lambda **kw: self.fail("thread for a checkout")):
                checkout.run_hourly()
            time.sleep(0.2)
            self.assertEqual(self.spawned, [])
            off.save({"auto": True})
            wait_for(lambda: self.spawned)
            self.assertEqual(self.spawned[0][1]["BREWDASH_REF"], "v0.10.0")

    def test_spawn_installer_runs_it_detached_with_the_env_it_is_given(self):
        d = Path(self.tmp.name)
        installer = d / "install.sh"
        installer.write_text('echo "$BREWDASH_ZIP $BREWDASH_APP_DIR $BREWDASH_MODE $HOME $$ $(ps -o pgid= -p $$ | tr -d " ") stdin:$(cat)"\n')
        proc = brewdash.spawn_installer(installer, {"BREWDASH_ZIP": "https://x/brewdash-macos.zip", "BREWDASH_APP_DIR": "/Apps",
                                                    "BREWDASH_MODE": "app"}, d / "update.log")
        self.assertEqual(proc.wait(timeout=10), 0)
        out = wait_for(lambda: (d / "update.log").read_text().strip())
        zip_, app_dir, mode, home, pid, pgid, stdin = out.split(" ")
        self.assertEqual((zip_, app_dir, mode, home, stdin),
                         ("https://x/brewdash-macos.zip", "/Apps", "app", os.environ["HOME"], "stdin:"))
        self.assertEqual(pgid, pid)
        self.assertNotEqual(int(pgid), os.getpgrp())

    def test_main_starts_the_hourly_check_on_the_updater_it_serves(self):
        server = mock.Mock()
        with mock.patch.object(brewdash, "serve", return_value=(server, "http://x", "t")) as serve, \
                mock.patch.object(brewdash, "Updater") as updater, \
                mock.patch.object(brewdash, "read_installed", return_value={"sha": SHA_A, "tag": None}), \
                mock.patch.object(brewdash, "read_mode", return_value="app"), \
                mock.patch.object(sys, "argv", ["brewdash", "--no-browser"]), \
                contextlib.redirect_stdout(io.StringIO()):
            brewdash.main()
        updater.assert_called_once_with(brewdash.STATE_DIR, {"sha": SHA_A, "tag": None}, mode="app")
        self.assertIs(serve.call_args.kwargs["updater"], updater.return_value)
        updater.return_value.run_hourly.assert_called_once_with()


class ReleaseWiringTest(unittest.TestCase):
    """The ZIP's name and the beta's tag, read independently from each place that names them."""
    ROOT = Path(brewdash.__file__).resolve().parent

    def test_the_workflow_publishes_the_zip_and_beta_the_updater_and_the_page_ask_for(self):
        workflow = (self.ROOT / ".github" / "workflows" / "macos-release.yml").read_text()
        page = (self.ROOT / "docs" / "index.html").read_text()
        self.assertEqual(re.findall(r'print\("ARTIFACT=([^"]+)"\)', workflow), ["brewdash-macos.zip"])
        self.assertEqual(set(re.findall(r"gh release (?:view|delete|create) ([a-z]+)\b", workflow)), {"beta"})
        self.assertEqual((brewdash.RELEASE_ASSET, brewdash.BETA_TAG), ("brewdash-macos.zip", "beta"))
        links = re.findall(r'https://github\.com/lemiorhan/brewdash/releases/[^"]+', page)
        self.assertEqual(set(links), {"https://github.com/lemiorhan/brewdash/releases/latest/download/brewdash-macos.zip"})
        self.assertEqual(len(links), 2, "the hero's link and the one every language's text takes")


class PublishStepTest(unittest.TestCase):
    """The workflow's own bash for naming the build and publishing it, run as the workflow runs it, with gh faked."""
    WORKFLOW = Path(brewdash.__file__).resolve().parent / ".github" / "workflows" / "macos-release.yml"
    ZIP = "/runner/dist/brewdash-macos.zip"
    TAGS_API = "api --paginate repos/lemiorhan/brewdash/tags?per_page=100 --jq .[].name"
    BETA_REF = "api repos/lemiorhan/brewdash/git/ref/tags/beta"
    GH = """#!/bin/bash
printf '%s\\n' "$*" >> "$GH_LOG"
case "$*" in
  "release view "*) [ "$3" = "$EXISTING_RELEASE" ] ;;
  "api --paginate repos/lemiorhan/brewdash/tags?per_page=100 --jq .[].name") printf '%s\\n' $TAGS ;;
  "api repos/lemiorhan/brewdash/git/ref/tags/beta") [ -n "$BETA_TAG_LEFT" ] ;;
esac
"""

    def step(self, name):
        """The step's lines above its `run: |`, the block under it unindented as YAML reads it, and every key the
        step has, above the block or below it."""
        lines = self.WORKFLOW.read_text().splitlines()
        start = lines.index(f"      - name: {name}")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("      - ")), len(lines))
        run = lines.index("        run: |", start, end)
        body = []
        for line in lines[run + 1:end]:
            if line.strip() and not line.startswith(" " * 10):
                break
            body.append(line[10:])
        keys = ["name"] + [m.group(1) for m in (re.match(r"        ([a-z-]+):", line) for line in lines[start:end]) if m]
        return "\n".join(lines[start:run]), "\n".join(body).strip("\n") + "\n", keys

    def run_step(self, name, **env):
        shell = re.search(r"(?m)^defaults:\n  run:\n    shell: (.+) \{0\}$", self.WORKFLOW.read_text()).group(1)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "gh").write_text(self.GH)
        (d / "gh").chmod(0o755)
        (d / "step.sh").write_text(self.step(name)[1])
        for f in ("gh.log", "env", "summary.md"):
            (d / f).write_text("")
        inherited = {k: v for k, v in os.environ.items() if k != "RELEASE_TAG"}
        r = subprocess.run([*shell.split(), str(d / "step.sh")], capture_output=True, text=True, timeout=60,
                           env=dict(inherited, PATH=f"{d}:{os.environ['PATH']}", GH_LOG=str(d / "gh.log"),
                                    GITHUB_ENV=str(d / "env"), GITHUB_STEP_SUMMARY=str(d / "summary.md"), **env))
        self.assertEqual(r.returncode, 0, r.stderr)
        return {f: (d / f).read_text() for f in ("gh.log", "env", "summary.md")}

    def publish(self, release_tag, existing="", tags="", beta_tag_left=""):
        """`release_tag` None leaves RELEASE_TAG unset, as a runner may when "Name the build" wrote it empty."""
        tag = {} if release_tag is None else {"RELEASE_TAG": release_tag}
        out = self.run_step("Publish the release", RUNNER_TEMP="/runner", ARTIFACT="brewdash-macos.zip",
                            GH_REPO="lemiorhan/brewdash", GITHUB_SHA=SHA_C, GITHUB_REF_NAME="some-branch",
                            EXISTING_RELEASE=existing, TAGS=tags, BETA_TAG_LEFT=beta_tag_left, **tag)
        return out["gh.log"].splitlines(), out["summary.md"]

    def test_the_build_is_named_for_a_version_tag_and_for_beta_otherwise(self):
        for ref_type, ref, tag in (("tag", "v1.2.3", "v1.2.3"), ("tag", "v0.1", None), ("branch", "master", None),
                                   ("branch", "v1.2.3", None)):
            with self.subTest(f"{ref_type} {ref}"):
                env = self.run_step("Name the build", GITHUB_SHA=SHA_A, GITHUB_REF_TYPE=ref_type, GITHUB_REF_NAME=ref)["env"]
                self.assertEqual(dict(line.split("=", 1) for line in env.splitlines()),
                                 {"BREWDASH_VERSION": json.dumps({"sha": SHA_A, "tag": tag}),
                                  "RELEASE_TAG": tag or "", "ARTIFACT": "brewdash-macos.zip"})

    def test_a_new_version_gets_its_release_and_only_the_highest_version_becomes_latest(self):
        tags = "v0.9.0 v0.10.0 v0.9.1 beta v0.1 v1.0"
        for tag, latest in (("v0.10.0", "true"), ("v0.9.1", "false")):
            with self.subTest(tag):
                calls, summary = self.publish(tag, tags=tags)
                self.assertEqual(calls, [f"release view {tag}", self.TAGS_API,
                                         f"release create {tag} {self.ZIP} --verify-tag --title {tag} --generate-notes "
                                         f"--latest={latest}"])
                self.assertEqual(summary, f"- release: https://github.com/lemiorhan/brewdash/releases/tag/{tag}\n")

    def test_a_version_whose_release_exists_gets_its_zip_replaced(self):
        calls, _ = self.publish("v0.10.0", existing="v0.10.0", tags="v0.10.0")
        self.assertEqual(calls, ["release view v0.10.0", f"release upload v0.10.0 {self.ZIP} --clobber"])

    def test_any_other_ref_replaces_the_one_beta_prerelease_at_its_commit(self):
        create = (f"release create beta {self.ZIP} --target {SHA_C} --prerelease --latest=false --title beta --notes "
                  f"A test build of some-branch at {SHA_C}. The next run from anything but a vX.Y.Z tag replaces it.")
        for name, release_tag, existing, left, between in (
                ("beta there", "", "beta", "", ["release delete beta --cleanup-tag --yes", self.BETA_REF]),
                ("no beta", "", "", "", [self.BETA_REF]),
                ("beta deleted by hand, its tag left", "", "", "1",
                 [self.BETA_REF, "api -X DELETE repos/lemiorhan/brewdash/git/refs/tags/beta"]),
                ("RELEASE_TAG unset", None, "", "", [self.BETA_REF])):
            with self.subTest(name):
                calls, summary = self.publish(release_tag, existing=existing, beta_tag_left=left)
                self.assertEqual(calls, ["release view beta", *between, create])
                self.assertEqual(summary, "- release: https://github.com/lemiorhan/brewdash/releases/tag/beta\n")

    def test_the_publish_step_always_runs_after_the_zip_and_fails_the_job_with_a_token_that_may_write_releases(self):
        workflow = self.WORKFLOW.read_text()
        self.assertEqual(re.findall(r"(?m)^( *)permissions:", workflow), [""])
        self.assertRegex(workflow, r"(?m)^permissions:\n  contents: write$")
        jobs = workflow.split("\njobs:\n", 1)[1]
        self.assertEqual(re.findall(r"(?m)^  ([a-z-]+):", jobs), ["release"])
        self.assertEqual(re.findall(r"(?m)^    ([a-z-]+):", jobs), ["runs-on", "timeout-minutes", "env", "steps"])
        head, _, keys = self.step("Publish the release")
        self.assertEqual(keys, ["name", "env", "run"])
        self.assertIn("          GH_TOKEN: ${{ github.token }}\n          GH_REPO: ${{ github.repository }}", head)
        names = re.findall(r"(?m)^      - name: (.+)$", workflow)
        self.assertLess(names.index("Name the build"), names.index("Staple the ticket and pack the ZIP"))
        self.assertLess(names.index("Staple the ticket and pack the ZIP"), names.index("Publish the release"))


class ServerWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "cache" / "api").mkdir(parents=True)
        (root / "cache" / "api" / "cask_names.txt").write_text("zed\n")
        cls.zed = make_app(root / "Applications", "Zed", "0.8")
        (root / "home").mkdir()
        (root / "home" / ".zshrc").write_text("export PATH=/nope/bin:$PATH\n")
        brew = FakeBrew(cache=str(root / "cache"), casks=APP_CASKS)
        cls.server, url, cls.token = brewdash.serve(port=0, open_browser=False, brew=brew, state_dir=str(root / "state"),
                                                    home=str(root / "home"), app_dirs=[str(root / "Applications")],
                                                    sysrun=no_listeners_unsigned)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, token=True, host=None, response=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Brewdash-Token"] = self.token
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return (r, data) if response else (r.status, data)

    def state(self):
        status, data = self.request("GET", "/api/state")
        snap = json.loads(data)
        return snap if snap["model"] else None

    def test_every_web_file_is_served_with_its_type(self):
        types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                 ".png": "image/png"}
        files = [f for f in brewdash.WEB.rglob("*") if f.is_file() and not f.name.startswith(".")]
        self.assertLessEqual({"index.html", "app.js", "logo.svg", "favicon-32.png"}, {f.name for f in files})
        for f in files:
            r, data = self.request("GET", "/" + f.relative_to(brewdash.WEB).as_posix(), token=False, response=True)
            self.assertEqual((r.status, r.getheader("Content-Type"), data), (200, types[f.suffix], f.read_bytes()), f)

    def test_a_checkout_serves_its_settings_and_refuses_to_update_itself(self):
        status, data = self.request("GET", "/api/settings")
        self.assertEqual((status, json.loads(data)["installed"], json.loads(data)["channel"]), (200, None, "stable"))
        status, data = self.request("POST", "/api/settings", {"auto": False})
        self.assertEqual((status, json.loads(data)["auto"]), (200, False))
        self.assertEqual(json.loads(self.request("GET", "/api/settings")[1])["auto"], False)
        for op in ("check", "now", "later"):
            with self.subTest(op=op):
                status, data = self.request("POST", "/api/update", {"op": op})
                self.assertEqual(status, 400)
        self.assertIn("no version.json from install.sh", json.loads(self.request("POST", "/api/update", {"op": "now"})[1])["error"])
        self.assertEqual(self.request("GET", "/api/settings", token=False)[0], 403)

    def test_it_listens_on_the_loopback_interface_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_api_rejects_missing_token_and_foreign_host(self):
        self.assertEqual(self.request("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/state", host=f"evil.test:{self.port}")[0], 403)
        self.assertEqual(self.request("POST", "/api/jobs", {"action": "update"}, token=False)[0], 403)

    def test_state_serves_the_model_built_from_brew(self):
        snap = wait_for(self.state)
        self.assertEqual(len(snap["model"]["packages"]), 11)
        self.assertIn("f:libA", {p["id"] for p in snap["model"]["packages"]})

    def test_plan_and_impact_endpoints(self):
        wait_for(self.state)
        status, data = self.request("POST", "/api/plan", {"action": "uninstall", "targets": ["f:libC"]})
        self.assertEqual((status, json.loads(data)), (200, {"title": "uninstall libC", "title_msg": ["plan.uninstall", {"names": "libC"}],
                                                                "commands": ["brew uninstall --formula libC"], "snapshot": True}))
        status, data = self.request("POST", "/api/impact", {"targets": ["f:libC"]})
        self.assertEqual(json.loads(data)["blockers"], ["f:app", "f:libA", "f:tool"])
        self.assertEqual(self.request("POST", "/api/plan", {"action": "install", "kind": "formula", "name": "-x"})[0], 400)

    def test_a_job_submitted_over_http_runs_and_shows_up_in_jobs_and_history(self):
        wait_for(self.state)
        status, data = self.request("POST", "/api/jobs", {"action": "pin", "targets": ["f:libA"]})
        self.assertEqual(status, 201)
        job_id = json.loads(data)["id"]

        def finished():
            jobs = json.loads(self.request("GET", f"/api/jobs?log={job_id}")[1])
            job = next(j for j in jobs if j["id"] == job_id)
            return job if job["status"] == "succeeded" else None

        job = wait_for(finished)
        self.assertEqual(job["log"], ["$ brew pin --formula libA"])
        self.assertEqual((job["title"], job["title_msg"]), ("pin libA", ["plan.pin", {"names": "libA"}]))
        history = json.loads(self.request("GET", "/api/history")[1])
        self.assertEqual(history[0]["commands"], ["brew pin --formula libA"])
        self.assertEqual((history[0]["title"], history[0]["title_msg"]), ("pin libA", ["plan.pin", {"names": "libA"}]))

    def apps_ready(self):
        snap = self.state()
        return snap if snap and snap["apps"] else None

    def test_state_carries_apps_and_shell_from_the_given_home_and_app_dirs(self):
        snap = wait_for(self.apps_ready)
        self.assertEqual([(a["name"], a["cask"]) for a in snap["apps"]["apps"]], [("Zed", "zed")])
        self.assertEqual([(e["raw"], e["issue"]) for e in snap["shell"]["path"]], [("/nope/bin", "missing")])

    def test_adopt_is_planned_from_the_scanned_apps(self):
        wait_for(self.apps_ready)
        status, data = self.request("POST", "/api/plan", {"action": "adopt", "app": "a:" + self.zed})
        self.assertEqual((status, json.loads(data)["commands"]), (200, ["brew install --cask --adopt zed"]))

    def test_export_list_add_download_and_remove(self):
        wait_for(self.apps_ready)
        status, data = self.request("POST", "/api/list", {"op": "add", "ids": ["t:me/tap", "f:libA", "a:" + self.zed]})
        items = [{"type": "tap", "name": "me/tap"}, {"type": "brew", "name": "libA"}, {"type": "cask", "name": "zed"}]
        text = ('# Brewfile from brewdash. On the new machine: brew bundle --file=Brewfile\n\n'
                'tap "me/tap"\nbrew "libA"\ncask "zed"\n')
        self.assertEqual((status, json.loads(data)), (200, {"items": items, "brewfile": text}))
        self.assertEqual(json.loads(self.request("GET", "/api/list")[1]), {"items": items, "brewfile": text})
        r, body = self.request("GET", "/api/brewfile", response=True)
        self.assertEqual((r.status, body.decode(), r.getheader("Content-Disposition"), r.getheader("Content-Type")),
                         (200, text, 'attachment; filename="Brewfile"', "text/plain; charset=utf-8"))
        self.assertEqual(json.loads((Path(self.tmp.name) / "state" / "list.json").read_text())["items"], items)
        status, data = self.request("POST", "/api/list", {"op": "remove", "items": [items[1]]})
        self.assertEqual((status, json.loads(data)["items"]), (200, [items[0], items[2]]))
        self.assertEqual(self.request("GET", "/api/brewfile", token=False)[0], 403)
        for body in ({"op": "add", "ids": ["f:nope"]}, {"op": "add", "ids": "f:libA"}, {"op": "drop"},
                     {"op": "remove", "items": ["libA"]}, {"op": "remove", "items": [{"type": "x", "name": "y"}]}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/list", body)[0], 400)
        self.assertEqual(json.loads(self.request("GET", "/api/list")[1])["items"], [items[0], items[2]])

    def test_refresh_endpoint_bumps_the_version(self):
        before = wait_for(self.state)["version"]
        self.assertEqual(self.request("POST", "/api/refresh", {})[0], 202)
        wait_for(lambda: (self.state() or {}).get("version", 0) > before)

    def test_search_and_preview_endpoints(self):
        wait_for(self.state)
        status, data = self.request("GET", "/api/search?q=jq")
        self.assertEqual((status, [r["name"] for r in json.loads(data)]), (200, ["jq", "jq-lsp", "me/tap/tool", "jqbook"]))
        status, data = self.request("GET", "/api/preview?kind=formula&name=newpkg")
        self.assertEqual((status, json.loads(data)["new_deps"]), (200, ["fresh"]))
        self.assertEqual(self.request("GET", "/api/search?q=--open")[0], 400)

    def test_malformed_requests_get_400_not_a_dropped_connection(self):
        wait_for(self.state)
        for path, body in (("/api/plan", []), ("/api/plan", "x"), ("/api/impact", {"targets": [[1]]}),
                           ("/api/jobs", {"action": "pin", "targets": [{"a": 1}]})):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.request("POST", path, body)[0], 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("X-Brewdash-Token", "töken".encode("latin-1"))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_static_files_and_traversal(self):
        status, data = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"brewdash", data)
        self.assertEqual(self.request("GET", "/vendor/cytoscape.min.js", token=False)[0], 200)
        self.assertEqual(self.request("GET", "/../brewdash.py", token=False)[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/brewdash.py", token=False)[0], 404)


FP = "a" * 64


def alert(severity="high", check_id="exposure.listening", identity=None, **extra):
    return {"fingerprint": FP, "check_id": check_id, "kind": "open_port", "severity": severity, "title": "t",
            "detail": "d", "remediation": "r", "fix_command": None, "identity": identity or {}, **extra}


class SecurityReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "report.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))
        return brewdash.load_security(self.path)

    def test_no_file_is_not_an_error(self):
        r = brewdash.load_security(self.path)
        self.assertEqual((r["found"], r["error"], r["alerts"]), (False, None, []))

    def test_a_broken_or_foreign_file_is_reported(self):
        self.assertIn("could not read it", self.load("{nope")["error"])
        self.assertEqual(self.load({"schema": 2, "alerts": [alert()]})["error"],
                         "it has schema 2; this brewdash reads schema 1")
        r = self.load([alert()])
        self.assertEqual((r["found"], r["error"], r["alerts"]), (True, "it has schema None; this brewdash reads schema 1", []))
        self.assertEqual(self.load({"schema": True, "alerts": [alert()]})["error"],
                         "it has schema True; this brewdash reads schema 1")
        self.assertIn("could not read it", self.load("[" * 200000)["error"])

    def test_a_fingerprint_with_a_trailing_newline_is_not_one(self):
        [a] = self.load({"schema": 1, "alerts": [alert(fingerprint=FP + "\n")]})["alerts"]
        self.assertIsNone(a["fingerprint"])

    def test_an_identity_of_the_wrong_type_links_nowhere_and_does_not_raise(self):
        self.assertIsNone(brewdash.link_alert(alert(check_id="packages.brew_taps", identity={"tap": ["x"]}), set(), {"x"}))

    def test_test_stores_read_no_real_home_or_apps(self):
        store = store_of(FakeBrew())
        self.assertEqual((store.home, store.app_dirs), (EMPTY_HOME.name, []))
        self.assertEqual(os.listdir(EMPTY_HOME.name), [])

    def test_alerts_are_typed_sorted_and_cleaned(self):
        r = self.load({"schema": 1, "generated_at": "2026-11-01T10:00:00", "alerts": [
            alert("low", title="<img src=x onerror=alert(1)>"),
            alert("bogus"), "not a dict", alert("high", title=None),
            alert("critical", fingerprint="../../x", detail=7, identity=["x"]),
            alert("medium", fix_command="brew untap me/tap"),
        ]})
        self.assertEqual(r["generated_at"], "2026-11-01T10:00:00")
        self.assertEqual([a["severity"] for a in r["alerts"]], ["critical", "medium", "low"])
        crit, med, low = r["alerts"]
        self.assertEqual((crit["fingerprint"], crit["detail"], crit["identity"]), (None, None, {}))
        self.assertEqual((med["fingerprint"], med["fix_command"]), (FP, "brew untap me/tap"))
        self.assertEqual(low["title"], "<img src=x onerror=alert(1)>")
        self.assertEqual(sorted(low), sorted([*brewdash.ALERT_TEXT, "severity", "identity", "fingerprint"]))

    def test_identity_keeps_only_the_strings_links_are_made_from(self):
        [a] = self.load({"schema": 1, "alerts": [alert(identity={
            "exe": "/opt/homebrew/bin/x", "plist": 5, "tap": "me/tap", "shadowed": ["git"]})]})["alerts"]
        self.assertEqual(a["identity"], {"exe": "/opt/homebrew/bin/x", "tap": "me/tap"})

    def test_an_identity_too_deep_to_encode_cannot_break_the_state(self):
        deep = "[" * 120000 + "]" * 120000
        text = json.dumps({"schema": 1, "alerts": [alert(identity={"exe": "X"})]}).replace('"X"', deep)
        r = self.load(text)
        json.dumps({**r, "alerts": [{**a, "link": brewdash.link_alert(a, set(), set())} for a in r["alerts"]]})

    def test_alerts_link_to_where_brewdash_can_act(self):
        pkgs, taps = {"f:redis"}, {"me/tap"}
        link = lambda **kw: brewdash.link_alert(alert(**kw), pkgs, taps)
        self.assertEqual(link(identity={"exe": "/opt/homebrew/Cellar/redis/7.2/bin/redis-server"}),
                         {"type": "package", "id": "f:redis"})
        self.assertEqual(link(identity={"exe": "/usr/local/opt/redis/bin/redis-server"}), {"type": "package", "id": "f:redis"})
        self.assertIsNone(link(identity={"exe": "/opt/homebrew/Cellar/nginx/1/bin/nginx"}))
        self.assertIsNone(link(identity={"exe": "/Applications/Redis.app/redis"}))
        self.assertEqual(link(check_id="persistence.launchd", identity={"plist": "/Library/LaunchDaemons/homebrew.mxcl.redis.plist"}),
                         {"type": "package", "id": "f:redis"})
        self.assertIsNone(link(check_id="persistence.launchd", identity={"plist": "/x/com.redis.plist"}))
        self.assertEqual(link(check_id="packages.brew_taps", identity={"tap": "me/tap"}), {"type": "tap", "name": "me/tap"})
        self.assertIsNone(link(check_id="packages.brew_taps", identity={"tap": "gone/tap"}))
        self.assertEqual(link(check_id="persistence.path_shadow"), {"type": "tab", "tab": "shell"})
        self.assertEqual(link(check_id="persistence.shell_rc"), {"type": "tab", "tab": "shell"})
        self.assertIsNone(link(check_id="system.firewall"))
        self.assertIsNone(link(check_id="exposure.listening", identity={"exe": 5}))

    def test_store_reads_the_report_again_only_when_it_changes(self):
        home = Path(self.tmp.name)
        (home / "secure-it").mkdir()
        report = home / "secure-it" / "report.json"
        store = store_of(FakeBrew(), home=str(home))
        self.assertEqual((store.security()["found"], store.security()["seq"]), (False, 1))
        report.write_text(json.dumps({"schema": 1, "alerts": [alert(identity={"exe": "/opt/homebrew/opt/tool/bin/tool"})]}))
        first = store.security()
        self.assertEqual((first["found"], first["seq"], first["alerts"][0]["link"]), (True, 2, None))
        self.assertEqual(store.security()["seq"], 2)
        store.refresh()
        wait_for(lambda: store.model)
        self.assertEqual(store.security()["alerts"][0]["link"], {"type": "package", "id": "f:tool"})
        self.assertEqual(store.security()["seq"], 2)
        before = report.stat().st_mtime_ns
        report.write_text(json.dumps({"schema": 1, "alerts": []}))
        os.utime(report, ns=(before, before))
        self.assertEqual((store.security()["seq"], store.security()["alerts"]), (3, []))
        self.assertEqual(store.snapshot()["security"]["seq"], 3)


LSOF_TCP = "p42\nf7\nn*:6379\nf8\nn[::1]:6379\nf9\nn127.0.0.1:6380\np99\nf3\nn*:22\n"
LSOF_UDP = "p42\nf10\nn*:5353\nf11\nn10.0.0.2:5000->10.0.0.9:53\nf12\nn*:*\n"
CODESIGN_OUT = {
    "/bin/ls": "Executable=/bin/ls\nIdentifier=com.apple.ls\nAuthority=macOS Software Signing\n"
               "Authority=Apple Code Signing Certification Authority\nAuthority=Apple Root CA\n",
    "/A/1Password.app": "Authority=Developer ID Application: AgileBits Inc. (2BUA8C4S2C)\n"
                        "Authority=Developer ID Certification Authority\nAuthority=Apple Root CA\nNotarization Ticket=stapled\n",
    "/A/Store.app": "Authority=Apple Mac OS Application Signing\nAuthority=Apple Worldwide Developer Relations Certification Authority\n",
    "/A/Phone.app": "Authority=Apple iPhone OS Application Signing\n",
    "/A/Corp.app": "Authority=Corp Internal CA\n",
    "/opt/homebrew/bin/jq": "Executable=/opt/homebrew/bin/jq\nSignature=adhoc\nTotal signatures=1\n",
    "/A/Plain.app": "/A/Plain.app: code object is not signed at all\n",
    "/A/Odd.app": "something else\n",
}


class ListeningAndSignatureTest(unittest.TestCase):
    def test_lsof_output_becomes_listeners_by_pid(self):
        self.assertEqual(brewdash.parse_listening(LSOF_TCP), {
            42: [{"address": "*", "port": 6379, "exposed": True}, {"address": "[::1]", "port": 6379, "exposed": False},
                 {"address": "127.0.0.1", "port": 6380, "exposed": False}],
            99: [{"address": "*", "port": 22, "exposed": True}]})
        self.assertEqual(brewdash.parse_listening(LSOF_UDP), {42: [{"address": "*", "port": 5353, "exposed": True}]})
        self.assertEqual(brewdash.parse_listening("p1\nn*:80\nn*:80\n"), {1: [{"address": "*", "port": 80, "exposed": True}]})

    def test_service_ports_ask_lsof_for_running_services_only(self):
        calls = []

        def sysrun(argv, timeout=60):
            calls.append(argv)
            return 1, LSOF_TCP if "-iTCP" in argv else LSOF_UDP, "lsof: some pids hidden"
        model = {"packages": [{"id": "f:redis", "service": {"pid": 42}}, {"id": "f:off", "service": {"pid": None}},
                              {"id": "f:plain", "service": None}, {"id": "f:other", "service": {"pid": 7}}]}
        self.assertEqual(brewdash.service_ports(model, sysrun), {"f:redis": [
            {"proto": "TCP", "address": "*", "port": 6379, "exposed": True},
            {"proto": "TCP", "address": "[::1]", "port": 6379, "exposed": False},
            {"proto": "TCP", "address": "127.0.0.1", "port": 6380, "exposed": False},
            {"proto": "UDP", "address": "*", "port": 5353, "exposed": True}]})
        self.assertEqual(calls, [[brewdash.LSOF, "-nP", "-a", "-p", "7,42", "-iTCP", "-sTCP:LISTEN", "-F", "pn"],
                                 [brewdash.LSOF, "-nP", "-a", "-p", "7,42", "-iUDP", "-F", "pn"]])
        self.assertEqual(brewdash.service_ports({"packages": [{"id": "f:off", "service": {"pid": None}}]},
                                                lambda *a, **k: self.fail("lsof without a pid")), {})

    def test_codesign_output_says_who_signed(self):
        kinds = {p: brewdash.classify_signature(out) for p, out in CODESIGN_OUT.items()}
        self.assertEqual(kinds, {
            "/bin/ls": {"kind": "apple", "signer": "macOS Software Signing"},
            "/A/1Password.app": {"kind": "developer_id", "signer": "AgileBits Inc. (2BUA8C4S2C)"},
            "/A/Store.app": {"kind": "appstore", "signer": "Apple Mac OS Application Signing"},
            "/A/Phone.app": {"kind": "appstore", "signer": "Apple iPhone OS Application Signing"},
            "/A/Corp.app": {"kind": "other", "signer": "Corp Internal CA"},
            "/opt/homebrew/bin/jq": {"kind": "adhoc", "signer": None},
            "/A/Plain.app": {"kind": "unsigned", "signer": None},
            "/A/Odd.app": {"kind": "unknown", "signer": None}})
        self.assertEqual(brewdash.classify_signature("Authority=Software Signing\n")["kind"], "apple")

    def test_every_app_gets_a_signature_and_one_failure_stays_local(self):
        def sysrun(argv, timeout=60):
            if argv[-1] == "/A/Boom.app":
                raise OSError("codesign vanished")
            self.assertEqual(argv[:3], [brewdash.CODESIGN, "-dv", "--verbose=2"])
            return 0, "", CODESIGN_OUT[argv[-1]]
        apps = [{"path": "/A/1Password.app"}, {"path": "/A/Boom.app"}, {"path": "/A/Plain.app"}]
        brewdash.sign_apps(apps, sysrun)
        self.assertEqual([a["signature"] for a in apps], [
            {"kind": "developer_id", "signer": "AgileBits Inc. (2BUA8C4S2C)"},
            {"kind": "unknown", "signer": None, "error": "codesign vanished"},
            {"kind": "unsigned", "signer": None}])

    def test_snapshot_carries_the_ports_of_running_services(self):
        def sysrun(argv, timeout=60):
            return 1, LSOF_TCP if "-iTCP" in argv else "", ""
        store = brewdash.Store(FakeBrew(), home=tempfile.gettempdir() + "/brewdash-no-home", app_dirs=[], sysrun=sysrun)
        store.refresh()
        wait_for(lambda: store.ports)
        self.assertEqual(store.snapshot()["ports"], {"error": None, "by_id": {"f:tool": [
            {"proto": "TCP", "address": "*", "port": 6379, "exposed": True},
            {"proto": "TCP", "address": "[::1]", "port": 6379, "exposed": False},
            {"proto": "TCP", "address": "127.0.0.1", "port": 6380, "exposed": False}]}})

    def test_lsof_failure_is_reported(self):
        def sysrun(argv, timeout=60):
            raise OSError("no lsof")
        store = brewdash.Store(FakeBrew(), home=tempfile.gettempdir() + "/brewdash-no-home", app_dirs=[], sysrun=sysrun)
        store.refresh()
        wait_for(lambda: store.ports)
        self.assertEqual(store.ports, {"error": "brewdash could not run lsof: no lsof", "by_id": {}})


class StopWithParentTest(unittest.TestCase):
    class Server:
        def __init__(self):
            self.stopped = threading.Event()

        def shutdown(self):
            self.stopped.set()

    def test_the_server_stops_once_the_parent_is_gone_and_not_before(self):
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        server = self.Server()
        try:
            brewdash.stop_with(parent.pid, server, interval=0.02)
            self.assertFalse(server.stopped.wait(0.3))
        finally:
            parent.kill()
            parent.wait()
        self.assertTrue(server.stopped.wait(2))

    @unittest.skipIf(os.geteuid() == 0, "root may signal any process")
    def test_a_pid_that_now_belongs_to_another_user_counts_as_gone(self):
        server = self.Server()
        brewdash.stop_with(1, server, interval=0.02)
        self.assertTrue(server.stopped.wait(2))

    def main_with(self, *argv):
        server = mock.Mock()
        with mock.patch.object(brewdash, "serve", return_value=(server, "http://127.0.0.1:1/?t=x", "x")), \
                mock.patch.object(brewdash, "stop_with") as stop_with, \
                mock.patch.object(sys, "argv", ["brewdash.py", "--no-browser", *argv]), \
                contextlib.redirect_stdout(io.StringIO()):
            brewdash.main()
        return server, stop_with

    def test_main_watches_the_process_given_with_parent(self):
        server, stop_with = self.main_with("--parent", "4242")
        stop_with.assert_called_once_with(4242, server)
        server.serve_forever.assert_called_once()
        self.main_with()[1].assert_not_called()


if __name__ == "__main__":
    unittest.main()
