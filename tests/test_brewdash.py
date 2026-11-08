import contextlib
import http.client
import inspect
import io
import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

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
    exe = "/fake/bin/brew"

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


SCAN_REFUSED = "only a plugin from brewdash's catalog, installed where only root can change it, can be scanned"


def catalog(exe="/nonexistent/secure-it", **kw):
    """brewdash's catalog with the brewdash security plugin's paths moved, so a test that passes it does not read this Mac's install."""
    return ({**brewdash.PLUGIN_CATALOG[0], "exe": exe, "protect": (exe,), **kw},)


def store_of(*args, **kw):
    """A Store that reads none of this Mac's home, apps, processes, signatures or installed plugins."""
    return brewdash.Store(*args, **{"sysrun": no_listeners_unsigned, "home": EMPTY_HOME.name, "app_dirs": [],
                                    "catalog": catalog(), "unprotected": lambda path: None, **kw})


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
           "source": source, "brew_id": brew_id, "cask": None, "cask_version": None, "auto_updates": None,
           "newer_than_cask": None}
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
            app_record(zed, "Zed", "manual", "0.8", cask="zed", cask_version="0.9", auto_updates=True,
                       newer_than_cask=False, signature=UNSIGNED)]})

    def test_an_app_newer_than_its_cask_is_marked_and_cannot_be_adopted(self):
        cache = Path(self.dir) / "cache"
        (cache / "api").mkdir(parents=True)
        (cache / "api" / "cask_names.txt").write_text("zed\n")
        apps_dir = Path(self.dir) / "Applications"
        zed = make_app(apps_dir, "Zed", "0.10")
        store = store_of(FakeBrew(cache=str(cache), casks=APP_CASKS), home=self.dir, app_dirs=[str(apps_dir)])
        store.refresh()
        [app] = wait_for(lambda: store.apps)["apps"]
        self.assertEqual((app["version"], app["cask_version"], app["newer_than_cask"]), ("0.10", "0.9", True))
        with self.assertRaisesRegex(ValueError, r"^Zed 0\.10 is newer than the zed cask's 0\.9; brew would install"):
            brewdash.plan_job({"action": "adopt", "app": "a:" + zed}, by_id(), store.apps_by_id())


class NewerThanCaskTest(unittest.TestCase):
    def test_only_a_version_read_on_both_sides_and_higher_on_the_apps_is_newer(self):
        for version, cask_version, newer in (
                ("0.9", "0.8", True), ("0.8", "0.9", False), ("0.8", "0.8", False),
                ("1.10", "1.9", True), ("1.9", "1.10", False),
                ("2.0", "1.9,42", True), ("1.9", "1.9,42", False), ("1.9.1", "1.9,42", True),
                ("1.0", "1", False), ("1", "1.0.0", False), ("1.0.1", "1", True),
                ("6.2.11 (43613)", "6.2.11.43613", False), ("2.0 beta", "1.9", True),
                (None, "1.0", False), ("1.0", "latest", False), ("beta", "1.0", False), ("1.0", None, False),
                (5, "1.0", False), (5.1, "1.0", False)):
            with self.subTest(version=version, cask_version=cask_version):
                self.assertIs(brewdash.newer_than_cask(version, cask_version), newer)


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
    APPS = {"a:/A/Zed.app": {"name": "Zed", "path": "/A/Zed.app", "source": "manual", "cask": "zed",
                             "newer_than_cask": False},
            "a:/A/Ahead.app": {"name": "Ahead", "path": "/A/Ahead.app", "source": "manual", "cask": "ahead",
                               "version": "2.0", "cask_version": "1.9,42", "newer_than_cask": True},
            "a:/A/Shop.app": {"name": "Shop", "path": "/A/Shop.app", "source": "appstore", "cask": "shop"},
            "a:/A/Bare.app": {"name": "Bare", "path": "/A/Bare.app", "source": "manual", "cask": None},
            "a:/A/Gui.app": {"name": "Gui", "path": "/A/Gui.app", "source": "brew", "cask": None}}

    def test_adopt_hands_the_app_its_backup_brew_and_the_cask_to_the_script_as_arguments(self):
        self.assertEqual(brewdash.plan_job({"action": "adopt", "app": "a:/A/Zed.app"}, by_id(), self.APPS, brew="/b/brew"),
                         ("bring Zed into brew", [["/bin/sh", "-c", brewdash.ADOPT_SCRIPT, "sh",
                                                   "/A/Zed.app", "/A/.Zed.app.brewdash-backup", "/b/brew", "zed"]], False))

    def test_adopt_refuses_app_store_unmatched_brew_and_unknown_apps(self):
        for app in ("a:/A/Shop.app", "a:/A/Bare.app", "a:/A/Gui.app", "a:/A/Nope.app", None, ["a:/A/Zed.app"]):
            with self.subTest(app=app), self.assertRaises(ValueError):
                brewdash.plan_job({"action": "adopt", "app": app}, by_id(), self.APPS)
        with self.assertRaises(ValueError):
            brewdash.plan_job({"action": "adopt", "app": "a:/A/Zed.app"}, by_id())

    def test_adopt_refuses_an_app_newer_than_its_cask_and_names_both_versions(self):
        with self.assertRaisesRegex(ValueError, r"^Ahead 2\.0 is newer than the ahead cask's 1\.9,42; brew would install "
                                                r"the older one over it$"):
            brewdash.plan_job({"action": "adopt", "app": "a:/A/Ahead.app"}, by_id(), self.APPS)


class AdoptScriptTest(unittest.TestCase):
    """Runs the planned adopt command for real, on a hand-installed app in a temporary folder, with a fake brew
    that logs its arguments and then runs `body`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = self.home = Path(tmp.name)
        # Some tests take write access away to make a step fail; the cleanup cannot remove the folder without it.
        self.addCleanup(lambda: [p.chmod(0o755) for p in (root, *root.rglob("*")) if p.is_dir()])
        self.app = root / "Applications" / "Zed.app"
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents" / "version").write_text("0.8")
        self.backup = root / "Applications" / ".Zed.app.brewdash-backup"
        self.library = root / "Library"
        (self.library / "Preferences").mkdir(parents=True)
        (self.library / "Preferences" / "dev.zed.plist").write_text("theme=dark")
        (self.library / "Application Support" / "Zed").mkdir(parents=True)
        (self.library / "Application Support" / "Zed" / "settings.json").write_text('{"font": 14}')
        self.settings = self.files(self.library)
        self.brew, self.calls, self.seen_home = root / "brew", root / "calls", root / "seen_home"

    @staticmethod
    def files(folder):
        return {str(p.relative_to(folder)): p.read_text() for p in sorted(folder.rglob("*")) if p.is_file()}

    def adopt(self, body, brew=None):
        self.brew.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\necho "$HOME" > "{self.seen_home}"\n{body}\n')
        self.brew.chmod(0o755)
        apps = {"a:z": {"name": "Zed", "path": str(self.app), "source": "manual", "cask": "zed", "newer_than_cask": False}}
        _, [cmd], _ = brewdash.plan_job({"action": "adopt", "app": "a:z"}, by_id(), apps, brew=str(brew or self.brew))
        # Only a write made through $HOME or ~ is kept off the real home by this; one to an absolute path is not.
        p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env={**os.environ, "HOME": str(self.home)})
        self.assertEqual(self.files(self.library), self.settings)
        return p.returncode, p.stdout

    def test_brew_installs_with_the_app_out_of_the_way_and_the_backup_is_removed(self):
        install = f'test -e "{self.app}" && exit 9\nmkdir -p "{self.app}/Contents" && echo 0.9 > "{self.app}/Contents/version"'
        rc, _ = self.adopt(install)
        self.assertEqual((rc, self.calls.read_text()), (0, f"install --cask --appdir={self.app.parent} zed\n"))
        self.assertEqual(self.seen_home.read_text(), f"{self.home}\n")
        self.assertEqual(self.files(self.app), {"Contents/version": "0.9\n"})
        self.assertFalse(self.backup.exists())

    def test_the_app_comes_back_when_brew_succeeds_without_putting_an_app_at_its_path(self):
        rc, out = self.adopt("exit 0")
        self.assertEqual(rc, 1)
        self.assertEqual(self.files(self.app), {"Contents/version": "0.8"})
        self.assertFalse(self.backup.exists())
        self.assertIn(f"brew put no app at {self.app}", out)

    def test_an_app_that_cannot_be_moved_aside_stops_it_before_brew_runs(self):
        self.app.parent.chmod(0o555)
        rc, _ = self.adopt("exit 0")
        self.assertEqual(rc, 1)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.files(self.app), {"Contents/version": "0.8"})
        self.assertFalse(self.backup.exists())

    def test_a_backup_that_cannot_be_removed_fails_the_job_and_says_where_it_is(self):
        rc, out = self.adopt(f'mkdir -p "{self.app}/Contents" && echo 0.9 > "{self.app}/Contents/version"\n'
                             f'chmod 555 "{self.backup}/Contents"')
        self.assertEqual(rc, 1)
        self.assertEqual((self.files(self.app), self.files(self.backup)), ({"Contents/version": "0.9\n"}, {"Contents/version": "0.8"}))
        self.assertIn(f"zed is installed, but {self.backup} could not be removed", out)

    def test_a_restore_that_fails_says_where_the_backup_is(self):
        rc, out = self.adopt(f'chmod 555 "{self.app.parent}"; exit 3')
        self.assertEqual(rc, 3)
        self.assertFalse(self.app.exists())
        self.assertEqual(self.files(self.backup), {"Contents/version": "0.8"})
        self.assertIn(f"could not put your app back; it is at {self.backup}", out)

    def test_the_app_comes_back_when_brew_fails(self):
        rc, out = self.adopt("exit 3")
        self.assertEqual((rc, self.calls.read_text()), (3, f"install --cask --appdir={self.app.parent} zed\n"))
        self.assertEqual(self.files(self.app), {"Contents/version": "0.8"})
        self.assertFalse(self.backup.exists())
        self.assertIn("is back where it was", out)

    def test_the_app_comes_back_when_brew_cannot_be_started(self):
        rc, _ = self.adopt("", brew=self.brew.with_name("missing-brew"))
        self.assertEqual(rc, 127)
        self.assertEqual(self.files(self.app), {"Contents/version": "0.8"})
        self.assertFalse(self.backup.exists())

    def test_what_a_failed_brew_left_in_place_is_not_overwritten_and_the_backup_is_kept(self):
        rc, out = self.adopt(f'mkdir -p "{self.app}/Contents" && echo half > "{self.app}/Contents/version"; exit 4')
        self.assertEqual(rc, 4)
        self.assertEqual((self.files(self.app), self.files(self.backup)), ({"Contents/version": "half\n"}, {"Contents/version": "0.8"}))
        self.assertIn(f"your app is kept at {self.backup}", out)

    def test_a_kept_backup_is_not_listed_as_an_app(self):
        self.adopt(f'mkdir -p "{self.app}/Contents"; exit 4')
        self.assertEqual(len(list(self.app.parent.iterdir())), 2)
        self.assertEqual([a["path"] for a in brewdash.scan_apps([str(self.app.parent)], {})], [str(self.app)])

    def test_a_backup_left_from_an_earlier_try_stops_it_before_anything_moves(self):
        self.backup.mkdir()
        (self.backup / "earlier").write_text("x")
        rc, out = self.adopt("exit 0")
        self.assertEqual(rc, 1)
        self.assertFalse(self.calls.exists())
        self.assertEqual((self.files(self.app), self.files(self.backup)), ({"Contents/version": "0.8"}, {"earlier": "x"}))
        self.assertIn("move it away first", out)


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
        for app in ("a:/A/Gui.app", "a:/A/Nope.app", None, ["a:/A/Zed.app"]):
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


class ServeTest(unittest.TestCase):
    def test_the_server_starts_and_answers_without_asking_dns_for_its_own_name(self):
        lookup = AssertionError("reverse DNS lookup")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("socket.getfqdn", side_effect=lookup), mock.patch("socket.gethostbyaddr", side_effect=lookup):
            server, url, _ = brewdash.serve(port=0, open_browser=False, brew=FakeBrew(), state_dir=tmp, refresh=False)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            port = server.server_address[1]
            self.assertEqual(urlparse(url).port, port)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
            r = conn.getresponse()
            self.assertEqual((r.status, b"brewdash" in r.read()), (200, True))
            conn.close()


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

    def updater(self, github=None, installed=None, root=APP_ROOT, spawn=None):
        self.github = github or FakeGitHub()
        return brewdash.Updater(self.state, installed or {"sha": SHA_A, "tag": "v0.9.0"}, root=root, fetch=self.github,
                                spawn=spawn or (lambda *a: self.spawned.append(a)))

    def test_an_update_installs_the_channels_release_zip_into_the_apps_own_folder(self):
        log = self.state / "update.log"
        for channel, target, zip_ in (("stable", SHA_B, zip_url("v0.10.0")), ("beta", SHA_C, zip_url("beta"))):
            with self.subTest(channel=channel):
                self.spawned.clear()
                u = self.updater(FakeGitHub(compare={target: "ahead"}))
                u.save({"channel": channel})
                u.update(manual=False)
                self.assertEqual(self.spawned, [(APP_ROOT / "install.sh", {"BREWDASH_ZIP": zip_,
                                                                          "BREWDASH_APP_DIR": "/Applications"}, log)])

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
                    self.assertEqual([a[1]["BREWDASH_ZIP"] for a in self.spawned], [zip_url("v0.10.0")] if want else [])
                    self.assertEqual(view["started"] is not None, want)

    def test_update_refuses_a_checkout_an_app_with_another_name_and_an_app_run_where_it_was_downloaded(self):
        with self.assertRaises(ValueError):
            brewdash.Updater(self.state, None).update(manual=True)
        with self.assertRaises(ValueError):
            brewdash.Updater(self.state, None).check()
        for root in (Path("/Applications/Mine.app/Contents/Resources/brewdash"),
                     Path("/private/var/folders/x1/abc/T/AppTranslocation/0A1B-2C3D/d/brewdash.app/Contents/Resources/brewdash")):
            with self.subTest(root=root):
                u = self.updater(FakeGitHub(compare={SHA_B: "ahead"}), root=root)
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
            self.assertEqual(self.spawned[0][1]["BREWDASH_ZIP"], zip_url("v0.10.0"))

    def test_spawn_installer_runs_it_detached_with_the_env_it_is_given(self):
        d = Path(self.tmp.name)
        installer = d / "install.sh"
        installer.write_text('echo "$BREWDASH_ZIP $BREWDASH_APP_DIR $HOME $$ $(ps -o pgid= -p $$ | tr -d " ") stdin:$(cat)"\n')
        proc = brewdash.spawn_installer(installer, {"BREWDASH_ZIP": "https://x/brewdash-macos.zip", "BREWDASH_APP_DIR": "/Apps"},
                                        d / "update.log")
        self.assertEqual(proc.wait(timeout=10), 0)
        out = wait_for(lambda: (d / "update.log").read_text().strip())
        zip_, app_dir, home, pid, pgid, stdin = out.split(" ")
        self.assertEqual((zip_, app_dir, home, stdin),
                         ("https://x/brewdash-macos.zip", "/Apps", os.environ["HOME"], "stdin:"))
        self.assertEqual(pgid, pid)
        self.assertNotEqual(int(pgid), os.getpgrp())

    def test_main_starts_the_hourly_check_on_the_updater_it_serves(self):
        server = mock.Mock()
        with mock.patch.object(brewdash, "serve", return_value=(server, "http://x", "t")) as serve, \
                mock.patch.object(brewdash, "Updater") as updater, \
                mock.patch.object(brewdash, "read_installed", return_value={"sha": SHA_A, "tag": None}), \
                mock.patch.object(sys, "argv", ["brewdash", "--no-browser"]), \
                contextlib.redirect_stdout(io.StringIO()):
            brewdash.main()
        updater.assert_called_once_with(brewdash.STATE_DIR, {"sha": SHA_A, "tag": None})
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
        (root / "cache" / "api" / "cask_names.txt").write_text("zed\nmy-tool\n")
        cls.zed = make_app(root / "Applications", "Zed", "0.8")
        cls.tool = make_app(root / "Applications", "My Tool", "3.2")
        (root / "home").mkdir()
        (root / "home" / ".zshrc").write_text("export PATH=/nope/bin:$PATH\n")
        (root / "secure-it").write_text("")
        (root / "state" / "plugins").mkdir(parents=True)
        (root / "state" / "plugins" / "acme.json").write_text(json.dumps({"schema": 1, "name": "Acme", "report": "~/acme.json"}))
        (root / "home" / "acme.json").write_text(json.dumps(report(finding(subject={"kind": "tap", "name": "me/tap"}),
                                                                   finding("seen", "info", subject={"kind": "shell"}))))
        brew = FakeBrew(cache=str(root / "cache"), casks=APP_CASKS)
        cls.unprotected = {}
        cls.server, url, cls.token = brewdash.serve(port=0, open_browser=False, brew=brew, state_dir=str(root / "state"),
                                                    home=str(root / "home"), app_dirs=[str(root / "Applications")],
                                                    sysrun=no_listeners_unsigned,
                                                    catalog=catalog(str(root / "secure-it"), report="~/scans/secure-it.json"),
                                                    unprotected=cls.unprotected.get)
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
        self.assertEqual([(a["name"], a["cask"], a["newer_than_cask"]) for a in snap["apps"]["apps"]],
                         [("My Tool", "me/tap/my-tool", True), ("Zed", "zed", False)])
        self.assertEqual([(e["raw"], e["issue"]) for e in snap["shell"]["path"]], [("/nope/bin", "missing")])

    def test_adopt_is_planned_and_submitted_from_the_scanned_apps_with_the_servers_brew(self):
        wait_for(self.apps_ready)
        backup = str(Path(self.zed).with_name(".Zed.app.brewdash-backup"))
        cmd = shlex.join(["/bin/sh", "-c", brewdash.ADOPT_SCRIPT, "sh", self.zed, backup, "/fake/bin/brew", "zed"])
        for path, code in (("/api/plan", 200), ("/api/jobs", 201)):
            with self.subTest(path=path):
                status, data = self.request("POST", path, {"action": "adopt", "app": "a:" + self.zed})
                self.assertEqual((status, json.loads(data)["commands"]), (code, [cmd]))

    def test_an_app_newer_than_its_cask_is_neither_planned_nor_submitted(self):
        wait_for(self.apps_ready)
        job_ids = lambda: [j["id"] for j in json.loads(self.request("GET", "/api/jobs")[1])]
        jobs = job_ids()
        for path in ("/api/plan", "/api/jobs"):
            with self.subTest(path=path):
                status, data = self.request("POST", path, {"action": "adopt", "app": "a:" + self.tool})
                self.assertEqual((status, json.loads(data)), (400, {"error": "My Tool 3.2 is newer than the me/tap/my-tool "
                                                                             "cask's 3.1,42; brew would install the older one over it"}))
        self.assertEqual(job_ids(), jobs)

    def test_state_lists_the_catalog_then_the_manifests_in_the_state_dir(self):
        snap = wait_for(self.state)
        secure_it, acme = snap["plugins"]
        self.assertEqual((secure_it["id"], secure_it["installed"], secure_it["scan"], secure_it["found"], secure_it["path"]),
                         ("secure-it", True, True, False, str(Path(self.tmp.name) / "home" / "scans" / "secure-it.json")))
        self.assertEqual((acme["id"], acme["name"], acme["scan"], acme["found"], acme["alerts"]),
                         ("acme", "Acme", False, True, [{"fingerprint": "f1", "severity": "high", "title": finding()["title"],
                                                          "link": {"type": "tap", "name": "me/tap"}}]))
        self.assertNotIn("report", acme)

    def test_a_plugins_whole_report_is_served_by_its_id(self):
        wait_for(self.state)
        status, data = self.request("GET", "/api/plugin?id=acme")
        whole = json.loads(data)
        self.assertEqual((status, whole["id"], whole["seq"]), (200, "acme", 1))
        self.assertEqual([(f["fingerprint"], f["link"]) for f in whole["report"]["findings"]],
                         [("f1", {"type": "tap", "name": "me/tap"}), ("seen", {"type": "tab", "tab": "shell"})])
        self.assertEqual(json.loads(self.request("GET", "/api/plugin?id=secure-it")[1]), {"id": "secure-it", "seq": 1, "report": None})
        self.assertEqual(self.request("GET", "/api/plugin?id=nope"), (400, json.dumps({"error": "no plugin 'nope'"}).encode()))
        self.assertEqual(self.request("GET", "/api/plugin?id=acme", token=False)[0], 403)

    def test_a_scan_is_planned_and_run_only_from_the_catalog(self):
        wait_for(self.state)
        status, data = self.request("POST", "/api/plan", {"action": "plugin_scan", "plugin": "secure-it"})
        self.assertEqual((status, json.loads(data)["commands"], json.loads(data)["title_msg"]),
                         (200, ["/usr/bin/sudo -A /usr/local/bin/secure-it scan --exit-zero"], ["plan.plugin_scan", {"name": "brewdash security plugin"}]))
        self.assertEqual(self.request("POST", "/api/plan", {"action": "plugin_scan", "plugin": "acme"})[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", {"action": "plugin_scan", "plugin": "acme"})[0], 400)
        status, data = self.request("POST", "/api/jobs", {"action": "plugin_scan", "plugin": "secure-it"})
        self.assertEqual(status, 201)
        job_id = json.loads(data)["id"]

        def finished():
            job = next(j for j in json.loads(self.request("GET", f"/api/jobs?log={job_id}")[1]) if j["id"] == job_id)
            return job if job["status"] in ("succeeded", "failed") else None

        self.assertEqual((wait_for(finished)["status"], wait_for(finished)["log"]),
                         ("succeeded", ["$ /usr/bin/sudo -A /usr/local/bin/secure-it scan --exit-zero"]))

    def test_a_catalog_plugin_whose_executable_is_gone_cannot_be_scanned_over_http(self):
        wait_for(self.state)
        exe = Path(self.tmp.name) / "secure-it"
        exe.unlink()
        try:
            for path in ("/api/plan", "/api/jobs"):
                with self.subTest(path=path):
                    status, data = self.request("POST", path, {"action": "plugin_scan", "plugin": "secure-it"})
                    self.assertEqual((status, json.loads(data)["error"]), (400, SCAN_REFUSED))
        finally:
            exe.write_text("")

    def test_a_catalog_plugin_someone_other_than_root_can_change_is_not_offered_or_scanned_over_http(self):
        wait_for(self.state)
        exe = str(Path(self.tmp.name) / "secure-it")
        self.unprotected[exe] = "/usr/local/bin"
        try:
            [secure_it] = [p for p in self.state()["plugins"] if p["id"] == "secure-it"]
            self.assertEqual((secure_it["installed"], secure_it["scan"], secure_it["unprotected"]), (True, False, "/usr/local/bin"))
            for path in ("/api/plan", "/api/jobs"):
                with self.subTest(path=path):
                    status, data = self.request("POST", path, {"action": "plugin_scan", "plugin": "secure-it"})
                    self.assertEqual((status, json.loads(data)["error"]), (400, SCAN_REFUSED))
        finally:
            self.unprotected.clear()
        status, data = self.request("POST", "/api/plan", {"action": "plugin_scan", "plugin": "secure-it"})
        self.assertEqual(status, 200)

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


TOPIC = {"id": "exposure.listening/open_port", "category": "exposure", "label": {"en": "Open port", "tr": "Açık port"},
         "effort": "medium", "code": "exposure.listening · open_port", "explain": None}


def finding(fingerprint="f1", severity="high", subject=None, **extra):
    return {"fingerprint": fingerprint, "topic": TOPIC["id"], "row": {"key": "subject:/x", "label": "x"}, "severity": severity,
            "title": {"en": "port 22 is open", "tr": "port 22 açık"}, "detail": "d", "remediation": {"en": "close it", "tr": "kapat"},
            "fix_command": None, "accept_command": {"en": "sudo secure-it accept f1", "tr": "sudo secure-it accept f1"},
            "accepted": None, "links": [], "subject": subject, **extra}


def report(*findings, schema=3, **extra):
    return {"schema": schema, "generated_at": "2026-11-03T10:00:00", "scan": {"started_at": "2026-11-03T09:59:00", "facts": []},
            "categories": [{"id": "exposure", "label": {"en": "Network", "tr": "Ağ"}}], "efforts": [{"id": "medium", "label": "medium"}],
            "topics": [TOPIC], "findings": list(findings), "history": [], **extra}


class PluginReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "report.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))
        return brewdash.load_report(self.path)

    def test_no_file_is_not_an_error(self):
        r = brewdash.load_report(self.path)
        self.assertEqual((r["found"], r["error"], r["outdated"], r["report"]), (False, None, None, None))

    def test_a_broken_or_foreign_file_is_reported(self):
        self.assertIn("could not read it", self.load("{nope")["error"])
        r = self.load([finding()])
        self.assertEqual((r["found"], r["error"], r["outdated"], r["report"]),
                         (True, "it has schema None; this brewdash reads schema 3", None, None))
        r = self.load(report(finding(), schema=True))
        self.assertEqual((r["error"], r["outdated"], r["report"]), ("it has schema True; this brewdash reads schema 3", None, None))
        self.assertIn("could not read it", self.load("[" * 200000)["error"])

    def test_a_report_of_another_schema_says_which_side_is_older(self):
        old, new = self.load(report(finding(), schema=2)), self.load(report(finding(), schema=4))
        self.assertEqual((old["error"], old["outdated"], old["report"]), ("it has schema 2; this brewdash reads schema 3", "plugin", None))
        self.assertEqual((new["error"], new["outdated"], new["report"]), ("it has schema 4; this brewdash reads schema 3", "brewdash", None))

    def test_test_stores_read_no_real_home_apps_or_plugins(self):
        store = store_of(FakeBrew())
        self.assertEqual((store.home, store.app_dirs), (EMPTY_HOME.name, []))
        self.assertEqual(os.listdir(EMPTY_HOME.name), [])
        self.assertEqual([c["exe"] for c in store.catalog], ["/nonexistent/secure-it"])

    def test_a_text_is_a_string_or_the_strings_of_a_language_map(self):
        text = brewdash.report_text
        self.assertEqual(text("<img src=x onerror=alert(1)>"), "<img src=x onerror=alert(1)>")
        self.assertEqual(text({"en": "Open", "tr": "Açık", "zh": 7, "es": None}), {"en": "Open", "tr": "Açık"})
        for bad in (None, 7, ["en"], {}, {"en": 1}):
            with self.subTest(text=bad):
                self.assertIsNone(text(bad))

    def test_a_finding_needs_a_fingerprint_a_listed_topic_a_severity_and_a_title_and_may_share_its_fingerprint(self):
        r = self.load(report(
            finding("a"), finding("a", "low"), finding(""), finding(7), finding("b", topic="unlisted"), finding("c", topic=["x"]),
            finding("d", "bogus"), finding("e", title=None), finding("f", "info"), "not a dict"))
        self.assertEqual([(f["fingerprint"], f["severity"]) for f in r["report"]["findings"]], [("a", "high"), ("a", "low"), ("f", "info")])

    def test_each_field_of_a_finding_keeps_its_type_or_is_emptied(self):
        links = [{"label": "GHSA-1", "url": "https://osv.dev/vulnerability/GHSA-1"}, {"label": "x", "url": "javascript:alert(1)"},
                 {"label": 5, "url": "https://a.example"}, "nope"]
        r = self.load(report(
            finding("good", fix_command="brew untap me/tap", accepted={"at": "2026-11-02T10:00:00", "note": "known"}, links=links,
                    subject={"kind": "tap", "name": "me/tap", "deep": [[1]]}, extra="dropped"),
            finding("bad", detail=7, remediation={"en": 1}, fix_command=["rm"], accept_command=[], accepted=True, links="x",
                    row={"key": 5, "label": "x"}, subject="shell"),
            finding("unlabelled", row={"key": "k", "label": None}, accepted={"at": 1, "note": ["n"]})))
        good, bad, unlabelled = r["report"]["findings"]
        self.assertEqual(good, {**finding("good"), "fix_command": "brew untap me/tap", "accepted": {"at": "2026-11-02T10:00:00", "note": "known"},
                                "links": links[:1], "subject": {"kind": "tap", "name": "me/tap"}})
        self.assertEqual(bad, {**finding("bad"), "detail": None, "remediation": None, "accept_command": None,
                               "row": {"key": "fingerprint:bad", "label": finding()["title"]}})
        self.assertEqual((unlabelled["row"], unlabelled["accepted"]),
                         ({"key": "fingerprint:unlabelled", "label": finding()["title"]}, {"at": None, "note": None}))

    def test_a_topic_needs_an_id_a_listed_category_and_a_label_and_an_unlisted_effort_is_none(self):
        sources = [{"title": "Apple", "kind": "apple", "url": "https://support.apple.com/x", "verified": "2026-11-01"},
                   {"title": {"tr": "CIS"}, "kind": 1, "url": "http://cis.example", "verified": 2}, {"title": None, "url": "https://x.example"}]
        odd = {**TOPIC, "id": "odd", "effort": "huge", "code": 5, "explain": {"what": {"en": "w"}, "why": 3, "attack": "a", "sources": sources}}
        topics = [TOPIC, {**TOPIC, "label": "again"}, {**TOPIC, "id": "nocat", "category": "gone"}, {**TOPIC, "id": "listcat", "category": ["exposure"]},
                  {**TOPIC, "id": "nolabel", "label": None}, {**TOPIC, "id": 5}, odd, {**TOPIC, "id": "flat", "explain": "text"}]
        r = self.load(report(categories=[{"id": "exposure", "label": "Ağ"}, {"id": "exposure", "label": "again"}, {"id": 3, "label": "x"},
                                         {"id": "nolabel"}, "x"],
                             efforts=[{"id": "medium", "label": {"en": "medium"}}, {"id": "huge"}], topics=topics))["report"]
        self.assertEqual((r["categories"], r["efforts"]), ([{"id": "exposure", "label": "Ağ"}], [{"id": "medium", "label": {"en": "medium"}}]))
        self.assertEqual([t["id"] for t in r["topics"]], [TOPIC["id"], "odd", "flat"])
        self.assertEqual(r["topics"][0], TOPIC)
        self.assertEqual({k: r["topics"][1][k] for k in ("effort", "code", "explain")}, {"effort": None, "code": None, "explain": {
            "what": {"en": "w"}, "why": None, "attack": "a",
            "sources": [sources[0], {"title": {"tr": "CIS"}, "kind": None, "url": None, "verified": None}]}})
        self.assertIsNone(r["topics"][2]["explain"])

    def test_history_keeps_runs_with_a_time_their_alerts_and_the_checks_that_did_not_run(self):
        history = [
            {"started_at": "2026-11-01T09:00:00", "alerts": [
                {"fingerprint": "a", "severity": "high", "title": {"en": "port 22"}, "accepted": True},
                {"fingerprint": "b", "severity": "low", "title": None, "accepted": "yes"},
                {"fingerprint": "c", "severity": "info", "title": "x"}, {"fingerprint": 5, "severity": "high"}, "x"],
             "checks": [{"id": "system.sip", "title": "SIP", "status": "error", "reason": "TimeoutError"},
                        {"id": "self.fda", "title": None, "status": "skipped", "reason": {"en": "no FDA"}},
                        {"id": "system.firewall", "title": "x", "status": "ok"}, {"id": None, "status": "error"}]},
            {"started_at": None, "alerts": []}, "nope", {"started_at": "2026-11-02T09:00:00", "alerts": "x"}]
        self.assertEqual(self.load(report(history=history))["report"]["history"], [
            {"started_at": "2026-11-01T09:00:00", "alerts": [
                {"fingerprint": "a", "severity": "high", "title": {"en": "port 22"}, "accepted": True},
                {"fingerprint": "b", "severity": "low", "title": "b", "accepted": False}],
             "checks": [{"id": "system.sip", "title": "SIP", "status": "error", "reason": "TimeoutError"},
                        {"id": "self.fda", "title": "self.fda", "status": "skipped", "reason": {"en": "no FDA"}}]},
            {"started_at": "2026-11-02T09:00:00", "alerts": [], "checks": []}])

    def test_the_scan_keeps_its_time_and_the_facts_with_a_label_and_a_value(self):
        facts = [{"label": "host", "value": "mac"}, {"label": {"tr": "kullanıcı"}, "value": "alice"}, {"label": "root", "value": None},
                 {"label": 1, "value": "x"}, "x"]
        r = self.load(report(scan={"started_at": "2026-11-03T09:59:00", "facts": facts}))
        self.assertEqual((r["generated_at"], r["report"]["scan"]), ("2026-11-03T10:00:00", {"started_at": "2026-11-03T09:59:00", "facts": facts[:2]}))
        bare = self.load({"schema": 3, "generated_at": 5, "scan": [], "categories": {}, "topics": "x", "findings": None, "history": 1})
        self.assertEqual((bare["error"], bare["generated_at"], bare["report"]), (None, None, {
            "scan": {"started_at": None, "facts": []}, "categories": [], "efforts": [], "topics": [], "findings": [], "history": []}))

    def test_a_subject_keeps_only_the_strings_links_are_made_from(self):
        clean = brewdash.clean_subject
        self.assertEqual(clean({"kind": "path", "path": "/opt/homebrew/bin/x", "name": "n", "deep": [[1]]}),
                         {"kind": "path", "path": "/opt/homebrew/bin/x"})
        self.assertEqual(clean({"kind": "tap", "name": "me/tap", "path": "/p"}), {"kind": "tap", "name": "me/tap"})
        self.assertEqual(clean({"kind": "shell", "path": "/p"}), {"kind": "shell"})
        for bad in (None, "shell", ["shell"], {}, {"kind": "file", "path": "/p"}, {"kind": "path", "path": 5},
                    {"kind": "path", "name": "/p"}, {"kind": "tap", "name": ["x"]}, {"kind": ["tap"]}):
            with self.subTest(subject=bad):
                self.assertIsNone(clean(bad))

    def test_no_deeply_nested_value_survives_into_the_state(self):
        # Shallow enough for every Python's json decoder: 3.9's gives up near a thousand levels, and the report is
        # then unreadable instead of cleaned, so a deeper value would test nothing there.
        deep = "[" * 500 + "]" * 500
        r = self.load(json.dumps(report(finding(subject={"kind": "path", "path": "X"}, detail={"en": "X"}, links="X"),
                                        scan={"facts": "X"}, history=[{"started_at": "t", "alerts": "X"}])).replace('"X"', deep))
        nesting = lambda v: 1 + max(map(nesting, v.values() if isinstance(v, dict) else v), default=0) \
            if isinstance(v, (dict, list)) else 0
        self.assertEqual(len(r["report"]["findings"]), 1)
        self.assertLess(nesting(r), 20)

    def test_alerts_link_to_where_brewdash_can_act(self):
        pkgs, taps = {"f:redis"}, {"me/tap"}
        link = lambda kind, **kw: brewdash.link_subject({"kind": kind, **kw}, pkgs, taps)
        self.assertEqual(link("path", path="/opt/homebrew/Cellar/redis/7.2/bin/redis-server"), {"type": "package", "id": "f:redis"})
        self.assertEqual(link("path", path="/usr/local/opt/redis/bin/redis-server"), {"type": "package", "id": "f:redis"})
        self.assertIsNone(link("path", path="/opt/homebrew/Cellar/nginx/1/bin/nginx"))
        self.assertIsNone(link("path", path="/Applications/Redis.app/redis"))
        self.assertEqual(link("path", path="/Library/LaunchDaemons/homebrew.mxcl.redis.plist"), {"type": "package", "id": "f:redis"})
        self.assertIsNone(link("path", path="/Library/LaunchDaemons/homebrew.mxcl.nginx.plist"))
        self.assertIsNone(link("path", path="/x/com.redis.plist"))
        self.assertEqual(link("tap", name="me/tap"), {"type": "tap", "name": "me/tap"})
        self.assertIsNone(link("tap", name="gone/tap"))
        self.assertEqual(link("shell"), {"type": "tab", "tab": "shell"})
        self.assertIsNone(brewdash.link_subject(None, pkgs, taps))

    def test_store_reads_a_report_again_only_when_it_changes_and_counts_open_alerts_only(self):
        home = Path(self.tmp.name)
        path = home / ".local" / "state" / "brewdash" / "plugins" / "secure-it" / "report.json"
        store = store_of(FakeBrew(), home=str(home))
        secure_it = lambda: store.plugins()[0]
        self.assertEqual((secure_it()["found"], secure_it()["seq"], secure_it()["path"]), (False, 1, str(path)))
        path.parent.mkdir(parents=True)
        tool = {"kind": "path", "path": "/opt/homebrew/opt/tool/bin/tool"}
        path.write_text(json.dumps(report(finding("low", "low", tool), finding("crit", "critical"), finding("info", "info", tool),
                                          finding("taken", "high", tool, accepted={"at": "2026-11-02T10:00:00", "note": None}))))
        first = secure_it()
        self.assertEqual((first["found"], first["seq"], "report" in first), (True, 2, False))
        title = finding()["title"]
        self.assertEqual(first["alerts"], [{"fingerprint": "crit", "severity": "critical", "title": title, "link": None},
                                           {"fingerprint": "low", "severity": "low", "title": title, "link": None}])
        self.assertEqual(secure_it()["seq"], 2)
        store.refresh()
        wait_for(lambda: store.model)
        package = {"type": "package", "id": "f:tool"}
        self.assertEqual([a["link"] for a in secure_it()["alerts"]], [None, package])
        whole = store.plugin_report("secure-it")
        self.assertEqual((whole["id"], whole["seq"], whole["report"]["topics"]), ("secure-it", 2, [TOPIC]))
        self.assertEqual([(f["fingerprint"], f["link"]) for f in whole["report"]["findings"]],
                         [("low", package), ("crit", None), ("info", package), ("taken", package)])
        self.assertEqual(secure_it()["seq"], 2)
        before = path.stat().st_mtime_ns
        path.write_text(json.dumps(report()))
        os.utime(path, ns=(before, before))
        self.assertEqual((secure_it()["seq"], secure_it()["alerts"]), (3, []))
        self.assertEqual([(p["id"], p["seq"]) for p in store.snapshot()["plugins"]], [("secure-it", 3)])
        with self.assertRaisesRegex(ValueError, "no plugin 'nope'"):
            store.plugin_report("nope")


class PluginCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "plugins"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, pid, data):
        (self.dir / f"{pid}.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def plugins(self, **kw):
        return store_of(FakeBrew(), home=str(self.root), plugins_dir=self.dir, **kw).plugins()

    def test_the_catalog_offers_a_scan_only_when_its_executable_is_there(self):
        exe = self.root / "secure-it"
        [missing] = self.plugins(catalog=catalog(str(exe)))
        [nowhere_safe] = self.plugins(catalog=catalog(str(exe)), unprotected={str(exe): "/usr/local/bin"}.get)
        self.assertEqual((nowhere_safe["installed"], nowhere_safe["scan"], nowhere_safe["unprotected"], missing["unprotected"]),
                         (False, False, "/usr/local/bin", None))
        exe.write_text("")
        [there] = self.plugins(catalog=catalog(str(exe)))
        self.assertEqual((missing["installed"], missing["scan"], there["installed"], there["scan"]), (False, False, True, True))
        [guarded] = self.plugins(catalog=catalog(str(exe)), unprotected={str(exe): "/usr/local/bin"}.get)
        self.assertEqual((guarded["installed"], guarded["scan"], guarded["unprotected"], there["unprotected"]),
                         (True, False, "/usr/local/bin", None))
        self.assertEqual((there["source"], there["install"], there["update"]),
                         ("catalog", brewdash.PLUGIN_CATALOG[0]["install"], brewdash.PLUGIN_CATALOG[0]["update"]))
        self.assertEqual((there["about"], there["about_msg"]),
                         (brewdash.MESSAGES["plugin.about.secure_it"], ["plugin.about.secure_it", {}]))

    def test_every_path_the_catalog_names_is_checked(self):
        exe = self.root / "secure-it"
        exe.write_text("")
        protect = (str(exe), str(self.root / "libexec"), str(self.root / "state"))
        cat = catalog(str(exe), protect=protect)
        for flagged in protect:
            with self.subTest(flagged=flagged):
                unprotected = {flagged: flagged + "/cli.py"}.get
                [plugin] = self.plugins(catalog=cat, unprotected=unprotected)
                store = store_of(FakeBrew(), catalog=cat, unprotected=unprotected)
                self.assertEqual((plugin["scan"], plugin["unprotected"], store.scannable()), (False, flagged + "/cli.py", {}))

    def test_secure_it_is_checked_where_its_installer_puts_it(self):
        self.assertEqual(brewdash.PLUGIN_CATALOG[0]["protect"],
                         ("/usr/local/bin/secure-it", "/usr/local/libexec/secure-it", "/Library/Application Support/secure-it"))

    def test_secure_its_report_is_read_from_brewdashs_state_folder_and_the_old_one_from_home(self):
        """The brewdash security plugin's paths.REPORT_RELPATH has to name the same file; this suite cannot reach that repo."""
        self.assertEqual((brewdash.PLUGIN_CATALOG[0]["report"], brewdash.PLUGIN_CATALOG[0]["legacy_report"]),
                         ("~/.local/state/brewdash/plugins/secure-it/report.json", "~/secure-it/report.json"))

    def test_the_install_and_update_commands_run_sudo_only_inside_the_plugins_clone(self):
        """Run as the user pastes them, with a git and a sudo that only record where they ran."""
        pasted = {(e["id"], key): "\n".join(e[key]) for e in brewdash.PLUGIN_CATALOG for key in ("install", "update")}
        self.assertEqual(sorted(pasted), [("secure-it", "install"), ("secure-it", "update")])
        bin_dir, log = self.root / "bin", self.root / "ran.log"
        bin_dir.mkdir()
        for tool in ("git", "sudo"):
            (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $PWD" >> "$LOG"\n')
            (bin_dir / tool).chmod(0o755)
        sudo_in = {("install", None): [], ("install", "brewdash-plugin-security"): ["brewdash-plugin-security"],
                   ("install", "secure-it"): [], ("update", None): [],
                   ("update", "brewdash-plugin-security"): ["brewdash-plugin-security"], ("update", "secure-it"): ["secure-it"]}
        for shell in (["/bin/zsh", "-f"], ["/bin/bash", "--norc"]):
            for (_, key), text in sorted(pasted.items()):
                for clone in (None, "brewdash-plugin-security", "secure-it"):
                    with self.subTest(shell=shell[0], key=key, clone=clone):
                        work = Path(tempfile.mkdtemp(dir=self.root))
                        if clone:
                            (work / clone).mkdir()
                        log.write_text("")
                        subprocess.run([*shell, "-c", text], cwd=work, env={"PATH": str(bin_dir), "HOME": str(self.root), "LOG": str(log)},
                                       capture_output=True, timeout=30)
                        ran = [line.split(" ", 1) for line in log.read_text().splitlines()]
                        self.assertEqual([os.path.relpath(os.path.realpath(where), os.path.realpath(work)) for tool, where in ran if tool == "sudo"],
                                         sudo_in[key, clone])

    def test_an_older_secure_its_report_says_update_and_is_never_read(self):
        legacy = self.root / "secure-it" / "report.json"
        legacy.parent.mkdir()
        legacy.write_text("{nope")
        new = self.root / ".local" / "state" / "brewdash" / "plugins" / "secure-it" / "report.json"
        [plugin] = self.plugins()
        self.assertEqual((plugin["found"], plugin["outdated"], plugin["error"], plugin["alerts"]),
                         (False, "plugin", f"found {legacy}, where an older brewdash security plugin writes; this brewdash reads {new}", []))
        self.assertEqual(store_of(FakeBrew(), home=str(self.root)).plugin_report("secure-it")["report"], None)
        new.parent.mkdir(parents=True)
        new.write_text(json.dumps(report(finding())))
        [plugin] = self.plugins()
        self.assertEqual((plugin["found"], plugin["outdated"], plugin["error"], len(plugin["alerts"])), (True, None, None, 1))

    def test_the_secure_it_folder_in_the_plugins_folder_is_no_manifest(self):
        plugins_dir = self.root / ".local" / "state" / "brewdash" / "plugins"
        (plugins_dir / "secure-it").mkdir(parents=True)
        (plugins_dir / "secure-it" / "report.json").write_text(json.dumps(report(finding())))
        plugins = store_of(FakeBrew(), home=str(self.root), plugins_dir=plugins_dir).plugins()
        self.assertEqual([(p["id"], p["source"], p["found"]) for p in plugins], [("secure-it", "catalog", True)])

    def test_a_manifest_adds_a_plugin_after_the_catalog_and_only_https_is_a_link(self):
        self.manifest("zeta", {"schema": 1, "name": " Zeta ", "report": "~/zeta/out.json", "homepage": "https://zeta.example"})
        self.manifest("acme", {"schema": 1, "name": "Acme <b>", "report": "/abs/acme.json", "homepage": "http://acme.example"})
        cat, acme, zeta = self.plugins()
        self.assertEqual([p["id"] for p in (cat, acme, zeta)], ["secure-it", "acme", "zeta"])
        self.assertEqual((acme["name"], acme["path"], acme["homepage"], acme["source"]), ("Acme <b>", "/abs/acme.json", None, "manifest"))
        self.assertEqual((zeta["name"], zeta["path"], zeta["homepage"]), ("Zeta", str(self.root / "zeta" / "out.json"), "https://zeta.example"))
        self.assertEqual((acme["installed"], acme["scan"], acme["install"], acme["update"], acme["about"]), (True, False, [], [], None))

    def test_a_manifest_plugin_reads_its_report_and_links_it(self):
        (self.root / "zeta").mkdir()
        (self.root / "zeta" / "out.json").write_text(json.dumps(report(finding(subject={"kind": "shell"}))))
        self.manifest("zeta", {"schema": 1, "name": "Zeta", "report": "~/zeta/out.json"})
        zeta = self.plugins()[1]
        self.assertEqual((zeta["found"], zeta["error"], [a["link"] for a in zeta["alerts"]]), (True, None, [{"type": "tab", "tab": "shell"}]))

    def test_a_broken_manifest_still_gets_a_tab_that_says_why(self):
        self.manifest("broken", "{nope")
        self.manifest("old", {"schema": 2, "report": "/r.json"})
        self.manifest("noreport", {"schema": 1, "name": "N"})
        self.manifest("relative", {"schema": 1, "report": "out.json"})
        self.manifest("notilde", {"schema": 1, "report": "~acme/out.json"})
        by = {p["id"]: p for p in self.plugins()[1:]}
        self.assertEqual(sorted(by), ["broken", "noreport", "notilde", "old", "relative"])
        self.assertIn("could not read its manifest", by["broken"]["error"])
        self.assertIn("has schema 2; this brewdash reads schema 1", by["old"]["error"])
        for pid in ("noreport", "relative", "notilde"):
            with self.subTest(pid=pid):
                self.assertIn("names no report", by[pid]["error"])
        for p in by.values():
            self.assertEqual((p["found"], p["path"], p["alerts"], p["name"] in (p["id"], "N")), (False, None, [], True))

    def test_only_a_file_named_like_an_id_is_a_manifest_and_the_catalog_keeps_its_ids(self):
        good = {"schema": 1, "report": "/r.json"}
        for name in ("Upper", "-dash", "under_score", "a" * 41, "secure-it"):
            self.manifest(name, {**good, "name": "taken" if name == "secure-it" else name})
        (self.dir / "ok.txt").write_text(json.dumps(good))
        self.manifest("a" * 40, good)
        self.manifest("0-k", good)
        plugins = self.plugins()
        self.assertEqual([p["id"] for p in plugins], ["secure-it", "0-k", "a" * 40])
        self.assertEqual((plugins[0]["name"], plugins[0]["tab"], plugins[0]["source"]), ("brewdash security plugin", "Security", "catalog"))

    def test_without_a_plugins_folder_there_is_only_the_catalog(self):
        self.dir.rmdir()
        self.assertEqual([p["id"] for p in self.plugins()], ["secure-it"])
        self.assertEqual([p["id"] for p in store_of(FakeBrew()).plugins()], ["secure-it"])

    def test_the_docs_scripts_and_ui_texts_call_the_plugin_by_its_full_name(self):
        root = Path(brewdash.__file__).resolve().parent
        groups = [sorted(g) for g in (root.glob("*.md"), (root / ".github").rglob("*.md"), (root / "docs").glob("*.html"),
                                      (root / "scripts").glob("*.py"), (root / "web").glob("*.html"), (root / "web").glob("*.js"),
                                      (root / "web" / "i18n").glob("*.js"))]
        self.assertEqual([bool(g) for g in groups], [True] * len(groups))
        texts = {f.relative_to(root).as_posix(): f.read_text() for g in groups for f in g}
        texts["MESSAGES"] = "\n".join(brewdash.MESSAGES.values())
        # A name the docs' hard wrap splits over two lines is still one name.
        texts = {k: " ".join(v.split()) for k, v in texts.items()}
        generic = re.compile(r"(?<!brewdash )security plugin|plugin de seguridad|güvenlik (?:plugin|eklenti)|安全插件", re.I)
        self.assertEqual({k: generic.findall(v) for k, v in texts.items() if generic.search(v)}, {})
        # The docs give the plugin's paths and command, which keep the old name.
        ui = {k: v for k, v in texts.items() if k.startswith("web/") or k == "MESSAGES"}
        self.assertEqual([k for k, v in ui.items() if "secure-it" in v], [])
        named = {k for k, v in texts.items() if "brewdash security plugin" in v}
        self.assertLessEqual({"web/app.js", "web/i18n/en.js", "web/i18n/es.js", "web/i18n/tr.js", "web/i18n/zh.js",
                              "README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "docs/index.html",
                              "scripts/demo.py"}, named)


class FirstUnprotectedTest(unittest.TestCase):
    EXE = "/usr/local/bin/secure-it"
    LIB = "/usr/local/libexec/secure-it"
    MOD = "/usr/local/libexec/secure-it/secure_it/cli.py"

    @staticmethod
    def entry(kind=stat.S_IFDIR, mode=0o755, uid=0, gid=0):
        return SimpleNamespace(st_mode=kind | mode, st_uid=uid, st_gid=gid)

    def first_unprotected(self, changed, path=EXE, unlistable=None):
        file = self.entry(stat.S_IFREG)
        tree = {"/": self.entry(), "/usr": self.entry(), "/usr/local": self.entry(), "/usr/local/bin": self.entry(),
                self.EXE: file, "/usr/local/libexec": self.entry(), self.LIB: self.entry(),
                self.LIB + "/secure-it.py": file, self.LIB + "/secure_it": self.entry(), self.MOD: file, **changed}

        def lstat(p):
            if tree[str(p)] is None:
                raise FileNotFoundError(p)
            if tree[str(p)] == "denied":
                raise PermissionError(p)
            return tree[str(p)]

        def listdir(p):
            if str(p) in (unlistable or {}):
                raise unlistable[str(p)](p)
            return [Path(k).name for k, v in tree.items() if v is not None and k != "/" and str(Path(k).parent) == str(p)]
        return brewdash.first_unprotected(path, lstat, listdir)

    def test_in_a_folder_names_the_first_path_inside_or_above_it_that_someone_other_than_root_can_change(self):
        lib, mod, pkg, folder = self.LIB, self.MOD, self.LIB + "/secure_it", self.entry

        def file(**kw):
            return self.entry(stat.S_IFREG, **kw)
        cases = [
            ("root owns all of it", {}, None, None),
            ("the user owns a module two folders down", {mod: file(uid=501)}, None, mod),
            ("wheel can write a module", {mod: file(mode=0o775)}, None, mod),
            ("wheel can write a folder inside", {pkg: folder(mode=0o775)}, None, pkg),
            ("wheel can write the folder itself", {lib: folder(mode=0o775)}, None, lib),
            ("a symlink inside", {lib + "/secure-it.py": self.entry(stat.S_IFLNK)}, None, lib + "/secure-it.py"),
            ("a pipe inside", {pkg + "/fifo": self.entry(stat.S_IFIFO, mode=0o644)}, None, pkg + "/fifo"),
            ("the user owns a folder above", {"/usr/local/libexec": folder(uid=501)}, None, "/usr/local/libexec"),
            ("only root can open the folder, so it is not walked", {lib: folder(mode=0o700), mod: file(uid=501)},
             {lib: PermissionError}, None),
            ("others may enter the folder but not list it", {lib: folder(mode=0o711)}, {lib: PermissionError}, lib),
            ("its group may read the folder", {lib: folder(mode=0o750, gid=20)}, {lib: PermissionError}, lib),
            ("anyone may read the folder", {lib: folder(mode=0o705)}, {lib: PermissionError}, lib),
            ("the folder cannot be listed for another reason", {lib: folder(mode=0o700)}, {lib: OSError}, lib),
            ("not installed yet", {lib: None}, None, None),
        ]
        for label, changed, unlistable, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.first_unprotected(changed, self.LIB, unlistable), expected)

    def test_the_real_lstat_and_listdir_are_the_defaults(self):
        defaults = inspect.signature(brewdash.first_unprotected).parameters
        self.assertEqual((defaults["lstat"].default, defaults["listdir"].default), (os.lstat, os.listdir))

    def test_names_the_first_path_from_the_file_up_that_someone_other_than_root_can_change(self):
        exe, folder = self.EXE, self.entry

        def file(**kw):
            return self.entry(stat.S_IFREG, **kw)
        cases = [
            ("root owns all of it", {}, None),
            ("the user owns the file", {exe: file(uid=501)}, exe),
            ("Homebrew on an Intel Mac owns the folder", {"/usr/local/bin": folder(uid=501)}, "/usr/local/bin"),
            ("the walk reaches /", {"/": folder(uid=501)}, "/"),
            ("the file is group-writable, even by wheel", {exe: file(mode=0o775)}, exe),
            ("anyone can write the file", {exe: file(mode=0o757)}, exe),
            ("only wheel, that is root, can write the folder", {"/usr/local": folder(mode=0o775)}, None),
            ("admin can write the folder", {"/usr/local": folder(mode=0o775, gid=80)}, "/usr/local"),
            ("anyone can write the folder, sticky or not", {"/usr": folder(mode=0o1777)}, "/usr"),
            ("the file is a symlink", {exe: self.entry(stat.S_IFLNK)}, exe),
            ("a folder is a symlink", {"/usr/local": self.entry(stat.S_IFLNK)}, "/usr/local"),
            ("a device is where the file should be", {exe: self.entry(stat.S_IFCHR)}, exe),
            ("not installed yet, and only root could put it there", {exe: None, "/usr/local/bin": None}, None),
            ("not installed yet, on an Intel Mac", {exe: None, "/usr/local/bin": folder(uid=501)}, "/usr/local/bin"),
            ("a folder cannot be read", {"/usr/local": "denied"}, "/usr/local"),
            ("the nearest of two is named", {exe: file(uid=501), "/usr/local/bin": folder(uid=501)}, exe),
        ]
        for label, changed, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.first_unprotected(changed), expected)

    def test_reads_this_macs_files_and_follows_no_symlink(self):
        with tempfile.NamedTemporaryFile() as mine:
            self.assertEqual((brewdash.first_unprotected("/usr/bin/true"), brewdash.first_unprotected("/etc/hosts"),
                              brewdash.first_unprotected(mine.name), brewdash.first_unprotected("/nonexistent/x")),
                             (None, "/etc", mine.name, None))


class PluginScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_scan_runs_the_catalogs_command_and_nothing_else(self):
        cat = {c["id"]: c for c in brewdash.PLUGIN_CATALOG}
        title, cmds, snap = brewdash.plan_job({"action": "plugin_scan", "plugin": "secure-it"}, by_id(), plugins=cat)
        self.assertEqual((title, cmds, snap), ("brewdash security plugin scan", [["/usr/bin/sudo", "-A", "/usr/local/bin/secure-it", "scan", "--exit-zero"]], False))
        for req, plugins in (({"action": "plugin_scan", "plugin": "secure-it"}, {}),
                             ({"action": "plugin_scan", "plugin": "secure-it"}, None),
                             ({"action": "plugin_scan", "plugin": "acme"}, cat),
                             ({"action": "plugin_scan", "plugin": ["secure-it"]}, cat),
                             ({"action": "plugin_scan"}, cat)):
            with self.subTest(req=req, plugins=plugins), self.assertRaises(ValueError):
                brewdash.plan_job(req, by_id(), plugins=plugins)

    def test_only_an_installed_catalog_plugin_is_scannable(self):
        exe = self.state / "secure-it"
        store = store_of(FakeBrew(), catalog=catalog(str(exe)))
        self.assertEqual(store.scannable(), {})
        exe.write_text("")
        self.assertEqual(list(store.scannable()), ["secure-it"])

    def test_an_executable_someone_other_than_root_can_change_is_not_scannable(self):
        exe = self.state / "secure-it"
        exe.write_text("")
        store = store_of(FakeBrew(), catalog=catalog(str(exe)), unprotected={str(exe): "/usr/local/bin"}.get)
        self.assertEqual(store.scannable(), {})

    def test_a_store_and_the_server_check_who_can_change_the_executable_unless_told_otherwise(self):
        exe = self.state / "secure-it"
        exe.write_text("")
        store = brewdash.Store(FakeBrew(), home=EMPTY_HOME.name, app_dirs=[], sysrun=no_listeners_unsigned,
                               catalog=catalog(str(exe)))
        self.assertEqual((store.scannable(), store.plugins()[0]["unprotected"]), ({}, str(exe)))
        self.assertIs(inspect.signature(brewdash.serve).parameters["unprotected"].default, brewdash.first_unprotected)

    def run_scan(self, script):
        brew = FakeBrew({("-A",): script})
        runner = brewdash.Runner(brew, store_of(brew), self.state)
        job = runner.submit("brewdash security plugin scan", [list(brewdash.PLUGIN_CATALOG[0]["scan"])], False)
        return wait_for(lambda: job if job["status"] in ("succeeded", "failed") else None)

    def test_a_scan_succeeds_only_on_exit_0_and_a_cancelled_password_asks_for_a_terminal(self):
        cancelled = "echo 'sudo: a password is required; the brewdash password dialog was cancelled or could not open'; exit 1"
        crashed = "echo 'Traceback (most recent call last):'; exit 1"
        jobs = [self.run_scan(s) for s in ("exit 0", cancelled, crashed, "exit 2")]
        self.assertEqual([(j["status"], j["exit_code"], j["needs_terminal"]) for j in jobs],
                         [("succeeded", 0, False), ("failed", 1, True), ("failed", 1, False), ("failed", 2, False)])


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
