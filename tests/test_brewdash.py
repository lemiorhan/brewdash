import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

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

    def test_deprecated_package_with_dependents_is_not_offered_an_uninstall(self):
        a = only(advice_of([formula("lib@1", **DEPRECATED), formula("app", deps=["lib@1"])]), "f:lib@1")
        self.assertEqual(reqs(a), [])
        self.assertEqual(a["text"], "It still works, and Homebrew plans to drop it. 1 installed package(s) still "
                                    "depend on it; it can be removed once they are upgraded off it or removed.")

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
        tree = ast.parse(Path(brewdash.__file__).read_text())
        [fn] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "advise"]
        docstring = fn.body[0].value
        fragments = {n.value.strip() for n in ast.walk(fn) if n is not docstring
                     and isinstance(n, ast.Constant) and isinstance(n.value, str) and " " in n.value.strip()}
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


class RemovalImpactTest(unittest.TestCase):
    def test_installed_dependents_outside_the_selection_block_removal(self):
        self.assertEqual(brewdash.removal_impact(by_id(), ["f:libC"])["blockers"], ["f:libA", "f:tool"])

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

    def __init__(self, scripts=None, prefix="/nonexistent", info=INFO, raises=()):
        self.scripts = scripts or {}
        self.popened = []
        self.prefix = prefix
        self.info = info
        self.raises = raises

    def run(self, *args, timeout=300):
        out = {("autoremove", "--dry-run"): ParseTest.AUTOREMOVE, ("--repository",): "/nonexistent",
               ("--prefix",): self.prefix, ("cleanup", "-n"): "free approximately 1MB of disk space",
               ("doctor",): "Warning: something", ("search", "--formula", "jq"): "jq\njq-lsp\nme/tap/tool\n",
               ("search", "--cask", "jq"): "jqbook\n", ("deps", "--formula", "newpkg"): "libC\nfresh\n",
               ("search", "--formula", "gui"): "gui\n", ("search", "--cask", "gui"): "gui\nme/tap/font\n"}
        rc = 1 if args == ("doctor",) else 0
        return rc, out.get(args, ""), ""

    def json(self, *args):
        # Like brew: a tap-qualified argument comes back with a short name/token.
        if args[0] == "info" and "--formula" in args:
            return {"formulae": [formula(args[-1].split("/")[-1], full_name=args[-1], versions={"stable": "4.2"})], "casks": []}
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
        store = brewdash.Store(brew)
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
        store = brewdash.Store(brew)
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
        store = brewdash.Store(brew)
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
        store = brewdash.Store(brew)
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
        runner = brewdash.Runner(brew, brewdash.Store(brew), blocked)
        first = runner.submit("a", [["brew", "a"]], False)
        second = runner.submit("b", [["brew", "b"]], False)
        wait_for(lambda: second["status"] == "succeeded")
        self.assertEqual(first["status"], "succeeded")
        self.assertTrue(any(l.startswith("could not write history") for l in first["log"]))

    def test_store_is_refreshed_after_every_job(self):
        brew = FakeBrew()
        store = brewdash.Store(brew)
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

        store = brewdash.Store(SlowBrew())
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
            store = brewdash.Store(FakeBrew(prefix=prefix))
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
            store = brewdash.Store(BrokenDoctor(prefix=prefix))
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

        store = brewdash.Store(SlowFirstDoctor())
        store.refresh()
        wait_for(lambda: doctors)
        store.refresh()
        wait_for(lambda: store.health and store.health["doctor"] == "new")
        first_doctor.set()
        time.sleep(0.3)
        self.assertEqual(store.health["doctor"], "new")

    def test_stale_slow_result_does_not_overwrite_a_newer_one(self):
        store = brewdash.Store(FakeBrew())
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


class ServerWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server, url, cls.token = brewdash.serve(port=0, open_browser=False, brew=FakeBrew(), state_dir=cls.tmp.name)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, token=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Brewdash-Token"] = self.token
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def state(self):
        status, data = self.request("GET", "/api/state")
        snap = json.loads(data)
        return snap if snap["model"] else None

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
        self.assertEqual((status, json.loads(data)), (200, {"title": "uninstall libC", "commands": ["brew uninstall --formula libC"], "snapshot": True}))
        status, data = self.request("POST", "/api/impact", {"targets": ["f:libC"]})
        self.assertEqual(json.loads(data)["blockers"], ["f:libA", "f:tool"])
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
        history = json.loads(self.request("GET", "/api/history")[1])
        self.assertEqual(history[0]["commands"], ["brew pin --formula libA"])

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


if __name__ == "__main__":
    unittest.main()
