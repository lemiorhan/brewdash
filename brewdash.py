#!/usr/bin/env python3
"""brewdash: a local web dashboard for Homebrew. Run `python3 brewdash.py`."""

import argparse
import json
import os
import plistlib
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "brewdash"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@+._/-]*$")
SUDO_RE = re.compile(r"sudo: .*(terminal is required|password is required)")
BREW_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1", "HOMEBREW_NO_COLOR": "1"}
SERVICE_OPS = {"start", "stop", "restart"}
STATIC_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                ".png": "image/png"}


# ---------- pure analysis ----------

def _formula_record(f):
    inst = f["installed"][-1]
    return {
        "id": "f:" + f["name"], "kind": "formula", "name": f["name"], "full_name": f["full_name"],
        "display_name": f["name"], "desc": f.get("desc"), "homepage": f.get("homepage"),
        "tap": f.get("tap"), "version": inst["version"], "latest": f["versions"]["stable"],
        "outdated": False, "pinned": f["pinned"], "requested": inst["installed_on_request"],
        "installed_time": inst.get("time"), "deprecated": f["deprecated"], "disabled": f["disabled"],
        "caveats": f.get("caveats"), "license": f.get("license"), "keg_only": f.get("keg_only", False),
        "deps": [], "dependents": [], "required_by": [], "missing_deps": [], "service": None, "orphan": False,
    }


def _cask_record(c):
    return {
        "id": "c:" + c["token"], "kind": "cask", "name": c["token"], "full_name": c["full_token"],
        "display_name": (c.get("name") or [c["token"]])[0], "desc": c.get("desc"),
        "homepage": c.get("homepage"), "tap": c.get("tap"), "version": c["installed"],
        "latest": c["version"], "outdated": False, "pinned": c.get("pinned", False),
        "requested": True, "installed_time": c.get("installed_time"),
        "deprecated": c["deprecated"], "disabled": c["disabled"], "caveats": c.get("caveats"),
        "license": None, "keg_only": False,
        "app_paths": [a["target"] for a in c.get("artifacts", []) if "app" in a and a.get("target")],
        "deps": [], "dependents": [], "required_by": [], "missing_deps": [], "service": None, "orphan": False,
    }


def build_model(info, outdated, services, taps, autoremove):
    pkgs = {}
    resolve = {}
    for f in info["formulae"]:
        rec = _formula_record(f)
        pkgs[rec["id"]] = rec
        resolve[f["name"]] = rec["id"]
        resolve[f["full_name"]] = rec["id"]
    # Runtime deps are recorded under the name used at install time, which can
    # since have become an alias or an old name (php@8.0 -> icu4c -> icu4c@79).
    for f in info["formulae"]:
        for alias in f.get("aliases", []) + f.get("oldnames", []):
            resolve.setdefault(alias, "f:" + f["name"])
    cask_resolve = {}
    for c in info["casks"]:
        rec = _cask_record(c)
        pkgs[rec["id"]] = rec
        cask_resolve[c["token"]] = cask_resolve[c["full_token"]] = rec["id"]

    edges = set()
    for f in info["formulae"]:
        src = "f:" + f["name"]
        for dep in f["installed"][-1].get("runtime_dependencies", []):
            if not dep.get("declared_directly"):
                continue
            target = resolve.get(dep["full_name"])
            if target:
                edges.add((src, target))
            else:
                pkgs[src]["missing_deps"].append(dep["full_name"])
    for c in info["casks"]:
        src = "c:" + c["token"]
        depends_on = c.get("depends_on") or {}
        for name in depends_on.get("formula", []):
            target = resolve.get(name)
            if target:
                edges.add((src, target))
            else:
                pkgs[src]["missing_deps"].append(name)
        for token in depends_on.get("cask", []):
            if token in cask_resolve:
                edges.add((src, cask_resolve[token]))
            else:
                pkgs[src]["missing_deps"].append(token)
    for src, dst in sorted(edges):
        pkgs[src]["deps"].append(dst)
        pkgs[dst]["dependents"].append(src)
    # brew uninstall refuses a formula that any installed formula lists among its runtime
    # dependencies, indirect ones included (youtube-dl -> python@3.11 -> openssl@1.1).
    required = set(edges)
    for f in info["formulae"]:
        for dep in f["installed"][-1].get("runtime_dependencies", []):
            target = resolve.get(dep["full_name"])
            if target and target != "f:" + f["name"]:
                required.add(("f:" + f["name"], target))
    for src, dst in sorted(required):
        pkgs[dst]["required_by"].append(src)

    # brew outdated and autoremove print a formula's full_name (tap/name for tap formulae).
    for kind, names in (("formulae", resolve), ("casks", cask_resolve)):
        for o in outdated.get(kind, []):
            rec = pkgs.get(names.get(o["name"]))
            if rec:
                rec["outdated"] = True
                rec["latest"] = o["current_version"]

    for s in services:
        rec = pkgs.get("f:" + s["name"])
        if rec:
            rec["service"] = {k: s.get(k) for k in ("status", "running", "loaded", "pid", "user", "file", "log_path")}

    for name in autoremove:
        rec = pkgs.get(resolve.get(name))
        if rec:
            rec["orphan"] = True

    tap_list = []
    for t in taps:
        members = [p["id"] for p in pkgs.values() if p["tap"] == t["name"]]
        tap_list.append({
            "name": t["name"], "remote": t.get("remote"), "official": t.get("official"),
            "private": t.get("private"), "last_commit": t.get("last_commit"),
            "formula_count": len(t.get("formula_names") or []),
            "cask_count": len(t.get("cask_tokens") or []), "installed": sorted(members),
        })

    raw = {"f:" + f["name"]: f for f in info["formulae"]}
    raw.update({"c:" + c["token"]: c for c in info["casks"]})
    for pid, rec in pkgs.items():
        rec["advice"] = advise(rec, raw[pid], pkgs, resolve, cask_resolve)

    return {"packages": sorted(pkgs.values(), key=lambda p: p["id"]), "taps": tap_list}


# Advice, PATH suggestions and job titles. The UI gets each message twice: rendered here in English, and as
# [key, args] so web/i18n can render it in another language; a key the UI's language does not have falls
# back to the English text. Error details (an unreadable report, a failed lsof) are not here and stay English.
MESSAGES = {
    "reason.versioned_formula": "it is an old versioned formula",
    "reason.unsupported": "it is no longer supported",
    "reason.deprecated_upstream": "upstream has deprecated it",
    "reason.fails_gatekeeper_check": "it fails the macOS Gatekeeper check",
    "reason.unmaintained": "it is not maintained",
    "reason.discontinued": "upstream has discontinued it",
    "reason.no_license": "it has no license",
    "reason.repo_archived": "its upstream repository is archived",
    "reason.does_not_build": "it does not build",
    "action.install": "Install {name}",
    "action.uninstall": "Uninstall {name}",
    "action.upgrade": "Upgrade",
    "action.upgrade_named": "Upgrade {name}",
    "action.unpin": "Unpin",
    "advice.pinned.title": "Pinned at {version}; brew upgrade skips it",
    "advice.pinned.text": "Unpin it first to upgrade to {latest}.",
    "advice.update.title": "Update available: {version} → {latest}",
    "advice.update.text": "Nothing blocks the upgrade.",
    "advice.disabled.title": "Disabled by Homebrew",
    "advice.disabled.title_why": "Disabled by Homebrew: {why}",
    "advice.deprecated.title": "Deprecated by Homebrew",
    "advice.deprecated.title_why": "Deprecated by Homebrew: {why}",
    "advice.disabled": "Homebrew disabled it: brew will not install or upgrade it any more. "
                       "The installed copy stays until you remove it.",
    "advice.disabled_on": "Homebrew disabled it on {date}: brew will not install or upgrade it any more. "
                          "The installed copy stays until you remove it.",
    "advice.disabled_newer": "Version {latest} exists, but not through brew.",
    "advice.deprecated": "It still works, and Homebrew plans to drop it.",
    "advice.deprecated_on": "It still works, and Homebrew plans to drop it: it will be disabled on {date}.",
    "advice.replacement_missing": "Homebrew suggests {name} instead, and it is not installed.",
    "advice.newer_unsupported": "The newer versions installed are deprecated or disabled too.",
    "advice.no_supported_newer": "No supported newer version is installed; the newest versioned one is {name}.",
    "advice.replacement_installed_only": "A newer or replacement package is already installed, "
                                         "so nothing needs to be installed.",
    "advice.replacement_installed": "A newer or replacement package is already installed.",
    "advice.still_needed": "{count} installed package(s) still need it, directly or through another dependency. "
                           "Uninstall offers to remove them with it, or to remove it anyway and break them.",
    "advice.unused_cask": "Nothing installed depends on it; remove it if you no longer use the app.",
    "advice.unused_requested": "Nothing installed depends on it, so it can be removed once nothing of yours "
                               "needs this exact version.",
    "advice.unused_dependency": "Nothing installed depends on it and it was installed only as a dependency, "
                                "so it is safe to remove.",
    "advice.missing.title": "Missing dependency: {dep}",
    "advice.missing": "{name} was installed against {dep}, which is not installed now.",
    "advice.missing_other_version": "Another version of it is installed, but this package needs exactly that one.",
    "advice.missing_upgrade": "Upgrading brings in whatever the new version needs; otherwise install it.",
    "advice.missing_install": "Installing it should repair {name}; if brew no longer has it, remove {name} instead.",
    "path.duplicate": "{file} already adds this directory earlier; this line can go.",
    "path.synced": "A cloud-synced folder comes before the system folders, so a file that arrives by sync, "
                   "from another device or a shared folder, runs in place of the system command of the same name. "
                   "Move it after $PATH, or copy the tools to a local folder.",
    "path.pinned": "Points at one installed version of {name} and breaks on every upgrade. Use {fixed} instead.",
    "path.pinned_gone": "Points at one installed version of {name} and breaks on every upgrade. "
                        "That version is no longer installed. Use {fixed} instead.",
    "path.old_prefix": "Homebrew lives in {prefix} on this Mac. Use {fixed} instead.",
    "path.old_prefix_gone": "Homebrew lives in {prefix} on this Mac. "
                            "Nothing is installed there either; this line can go.",
    "path.missing": "The directory does not exist; this line can go.",
    "plan.adopt": "bring {name} into brew",
    "plan.trash": "move {name} to the Trash",
    "plan.untap": "untap {names}",
    "plan.upgrade": "upgrade {names}",
    "plan.pin": "pin {names}",
    "plan.unpin": "unpin {names}",
    "plan.uninstall": "uninstall {names}",
    "plan.uninstall_force": "uninstall {names}, ignoring dependents",
    "plan.upgrade_all": "upgrade all outdated",
    "plan.update": "brew update",
    "plan.autoremove": "autoremove",
    "plan.cleanup": "cleanup",
    "plan.service": "service {op} {name}",
    "service.start": "start",
    "service.stop": "stop",
    "service.restart": "restart",
    "plan.install": "install {name}",
}


def msg(key, **args):
    """A message for the UI as [key, args]; an arg may itself be a message."""
    return [key, args]


def render(m):
    """The English text of a message."""
    if not isinstance(m, list):
        return str(m)
    key, args = m
    return MESSAGES[key].format(**{k: render(v) for k, v in args.items()})


def render_all(ms):
    return " ".join(render(m) for m in ms)


def _version_key(name):
    """php@8.10 -> (8, 10); None when the suffix is not numeric."""
    _, _, suffix = name.partition("@")
    try:
        return tuple(int(x) for x in suffix.split("."))
    except ValueError:
        return None


def _newer_family(rec, item, pkgs, resolve):
    """Installed ids of newer versions of the same formula, and an uninstalled one newer than all of them."""
    own = _version_key(rec["name"])
    if own is None:
        return [], None
    base = rec["name"].partition("@")[0]
    installed, available, top = [], [], own
    for name in item.get("versioned_formulae") or []:
        key = _version_key(name)
        if key is None or key <= own:
            continue
        if "f:" + name in pkgs:
            installed.append("f:" + name)
            top = max(top, key)
        else:
            available.append((key, name))
    # The unversioned name is an alias of the newest version (python -> python@3.15) or a
    # formula of its own; either way it is only known to exist when it resolves locally.
    base_id = resolve.get(base)
    if base_id and base_id not in installed:
        key = _version_key(pkgs[base_id]["name"])
        if key is None or key > own:
            installed.append(base_id)
    newest = max(available) if available else None
    return installed, newest[1] if newest and newest[0] > top else None


def advise(rec, item, pkgs, resolve, cask_resolve):
    """What to do about an outdated, deprecated/disabled or broken package.

    Each entry: {level, title, text, msg, alternatives: [installed ids], actions: [{label, msg, req}]}, where
    msg holds the title and text as messages. The reqs are plan_job requests, so the UI runs them through
    the normal confirm flow.
    """
    out = []
    pid, name = rec["id"], rec["name"]

    def entry(level, title, parts, alternatives=(), actions=()):
        out.append({"level": level, "title": render(title), "text": render_all(parts),
                    "msg": {"title": title, "text": parts},
                    "alternatives": list(alternatives), "actions": list(actions)})

    def action(label, req):
        return {"label": render(label), "msg": label, "req": req}

    def install(kind, target):
        return action(msg("action.install", name=target), {"action": "install", "kind": kind, "name": target})

    uninstall = action(msg("action.uninstall", name=name), {"action": "uninstall", "targets": [pid]})

    if rec["outdated"] and not rec["disabled"]:
        if rec["pinned"]:
            entry("info", msg("advice.pinned.title", version=rec["version"]),
                  [msg("advice.pinned.text", latest=rec["latest"])],
                  actions=[action(msg("action.unpin"), {"action": "unpin", "targets": [pid]})])
        else:
            entry("info", msg("advice.update.title", version=rec["version"], latest=rec["latest"]),
                  [msg("advice.update.text")],
                  actions=[action(msg("action.upgrade"), {"action": "upgrade", "targets": [pid]})])

    if rec["disabled"] or rec["deprecated"]:
        kind = "disable" if rec["disabled"] else "deprecation"
        reason = item.get(f"{kind}_reason")
        why = None
        if reason:
            why = msg("reason." + reason) if "reason." + reason in MESSAGES else reason.replace("_", " ")
        date = item.get("disable_date")
        if rec["disabled"]:
            parts = [msg("advice.disabled_on", date=date) if date else msg("advice.disabled")]
            if rec["outdated"]:
                parts.append(msg("advice.disabled_newer", latest=rec["latest"]))
        else:
            parts = [msg("advice.deprecated_on", date=date) if date else msg("advice.deprecated")]
        alternatives, actions, missing_repl = [], [], False
        for rkind, field in (("formula", f"{kind}_replacement_formula"), ("cask", f"{kind}_replacement_cask")):
            repl = item.get(field)
            if repl:
                rid = (resolve if rkind == "formula" else cask_resolve).get(repl)
                if rid:
                    alternatives.append(rid)
                else:
                    missing_repl = True
                    parts.append(msg("advice.replacement_missing", name=repl))
                    actions.append(install(rkind, repl))
        if rec["kind"] == "formula":
            newer, newest = _newer_family(rec, item, pkgs, resolve)
            supported = [i for i in newer if not pkgs[i]["deprecated"] and not pkgs[i]["disabled"]]
            alternatives += [i for i in supported if i not in alternatives]
            if newer and not supported:
                parts.append(msg("advice.newer_unsupported"))
            if not alternatives and newest and not missing_repl:
                parts.append(msg("advice.no_supported_newer", name=newest))
                actions.append(install("formula", newest))
        if alternatives and not actions:
            parts.append(msg("advice.replacement_installed_only"))
        elif alternatives:
            parts.append(msg("advice.replacement_installed"))
        if rec["required_by"]:
            parts.append(msg("advice.still_needed", count=len(rec["required_by"])))
            actions.append(uninstall)
        else:
            if rec["kind"] == "cask":
                parts.append(msg("advice.unused_cask"))
            elif rec["requested"]:
                parts.append(msg("advice.unused_requested"))
            else:
                parts.append(msg("advice.unused_dependency"))
            actions.append(uninstall)
        title = "advice.disabled.title" if rec["disabled"] else "advice.deprecated.title"
        entry("warn", msg(title + "_why", why=why) if why else msg(title), parts, alternatives, actions)

    for dep in rec["missing_deps"]:
        dep_kind = "cask" if rec["kind"] == "cask" and dep in ((item.get("depends_on") or {}).get("cask") or []) \
            else "formula"
        base = dep.partition("@")[0]
        same = sorted({p["id"] for p in pkgs.values() if p["kind"] == "formula"
                       and p["name"].partition("@")[0] == base}) if dep_kind == "formula" else []
        parts = [msg("advice.missing", name=name, dep=dep)]
        if same:
            parts.append(msg("advice.missing_other_version"))
        actions = []
        if rec["outdated"] and not rec["disabled"] and not rec["pinned"]:
            parts.append(msg("advice.missing_upgrade"))
            actions.append(action(msg("action.upgrade_named", name=name), {"action": "upgrade", "targets": [pid]}))
        else:
            parts.append(msg("advice.missing_install", name=name))
        actions.append(install(dep_kind, dep))
        entry("warn", msg("advice.missing.title", dep=dep), parts, same, actions)
    return out


def removal_impact(pkgs, selection):
    """What removing `selection` (package ids) does to the rest of the install."""
    selected = set(selection)
    blockers, todo = set(), list(selected)
    while todo:
        for d in pkgs[todo.pop()]["required_by"]:
            if d not in selected and d not in blockers:
                blockers.add(d)
                todo.append(d)
    blockers = sorted(blockers)
    removed = set(selected)
    new_orphans = set()
    changed = True
    while changed:
        changed = False
        for p in pkgs.values():
            if p["id"] in removed or p["kind"] != "formula" or p["requested"] or not p["required_by"]:
                continue
            if all(d in removed for d in p["required_by"]):
                removed.add(p["id"])
                new_orphans.add(p["id"])
                changed = True
    return {"blockers": blockers, "new_orphans": sorted(new_orphans)}


def parse_autoremove(text):
    names, inside = [], False
    for line in text.splitlines():
        if re.match(r"==> Would autoremove \d+ unneeded formula", line):
            inside = True
            continue
        if inside:
            if not line.strip() or line.startswith("==>"):
                break
            names.append(line.strip())
    return names


def parse_cleanup(text):
    m = re.search(r"free approximately (\S+) of disk space", text)
    return m.group(1) if m else None


def parse_du(text, id_by_path):
    sizes = {}
    for line in text.splitlines():
        kb, _, path = line.partition("\t")
        pid = id_by_path.get(path)
        if pid and kb.isdigit():
            sizes[pid] = sizes.get(pid, 0) + int(kb)
    return sizes


def check_targets(targets, pkgs):
    targets = targets or []
    if not isinstance(targets, list) or not all(isinstance(t, str) and t in pkgs for t in targets):
        raise ValueError("unknown package in targets")
    return targets


def plan_job(req, pkgs, apps=None, taps=None):
    """Turn an API request into (title, [argv...], snapshot?). Raises ValueError on bad input."""
    title, cmds, snap = plan_job_msg(req, pkgs, apps, taps)
    return render(title), cmds, snap


def plan_job_msg(req, pkgs, apps=None, taps=None):
    """plan_job with the title as a message."""
    action = req.get("action")
    if action == "untap":
        names = req.get("taps")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n in (taps or ()) for n in names):
            raise ValueError("unknown tap in taps")
        return msg("plan.untap", names=", ".join(names)), [["brew", "untap", *names]], True
    if action == "adopt":
        app = (apps or {}).get(req.get("app"))
        if not app or app["source"] != "manual" or not app["cask"]:
            raise ValueError("only an app with a verified cask, installed outside brew and the App Store, can be adopted")
        return msg("plan.adopt", name=app["name"]), [["brew", "install", "--cask", "--adopt", app["cask"]]], False
    if action == "trash":
        app = (apps or {}).get(req.get("app"))
        # A cask's app goes through brew uninstall, or brew keeps a record of an app that is gone.
        if not app or app["source"] == "brew":
            raise ValueError("only an app installed outside brew can be moved to the Trash")
        # The path goes in as an argument, so no app name is ever parsed as AppleScript.
        # Inside a Finder tell block POSIX file does not resolve, so the alias is made outside it.
        script = ["-e", "on run argv", "-e", "set f to POSIX file (item 1 of argv) as alias",
                  "-e", 'tell application "Finder" to delete f', "-e", "end run"]
        return msg("plan.trash", name=app["name"]), [["osascript", *script, app["path"]]], False
    targets = check_targets(req.get("targets"), pkgs)
    formulae = [pkgs[t]["name"] for t in targets if pkgs[t]["kind"] == "formula"]
    casks = [pkgs[t]["name"] for t in targets if pkgs[t]["kind"] == "cask"]

    def split(verb, *formula_flags):
        cmds = []
        if formulae:
            cmds.append(["brew", verb, *formula_flags, "--formula", *formulae])
        if casks:
            cmds.append(["brew", verb, "--cask", *casks])
        if not cmds:
            raise ValueError("no targets")
        return cmds

    names = ", ".join(formulae + casks)
    if action in ("upgrade", "pin", "unpin"):
        return msg("plan." + action, names=names), split(action), len(targets) > 1
    if action == "uninstall":
        force = req.get("ignore_dependencies", False)
        if not isinstance(force, bool):
            raise ValueError("ignore_dependencies must be true or false")
        if force:
            return msg("plan.uninstall_force", names=names), split("uninstall", "--ignore-dependencies"), True
        return msg("plan.uninstall", names=names), split("uninstall"), True
    if action == "upgrade_all":
        return msg("plan.upgrade_all"), [["brew", "upgrade"]], True
    if action == "update":
        return msg("plan.update"), [["brew", "update"]], False
    if action == "autoremove":
        return msg("plan.autoremove"), [["brew", "autoremove"]], True
    if action == "cleanup":
        return msg("plan.cleanup"), [["brew", "cleanup"]], False
    if action == "service":
        op = req.get("op")
        if op not in SERVICE_OPS or casks or len(formulae) != 1:
            raise ValueError("service needs one formula and op start|stop|restart")
        if pkgs[targets[0]]["service"] is None:
            raise ValueError("formula has no service")
        return msg("plan.service", op=msg("service." + op), name=formulae[0]), [["brew", "services", op, formulae[0]]], False
    if action == "install":
        name, kind = req.get("name") or "", req.get("kind")
        if not NAME_RE.match(name) or kind not in ("formula", "cask"):
            raise ValueError("bad install name or kind")
        return msg("plan.install", name=name), [["brew", "install", "--" + kind, name]], False
    raise ValueError(f"unknown action {action!r}")


def authorize(path, headers, token, port):
    if headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return False
    if path.startswith("/api/"):
        given = (headers.get("X-Brewdash-Token") or "").encode("utf-8", "surrogateescape")
        return secrets.compare_digest(given, token.encode())
    return True


# ---------- secure-it findings ----------

SECURITY_SCHEMA = 1
SEVERITIES = ("critical", "high", "medium", "low")
ALERT_TEXT = ("check_id", "kind", "title", "detail", "remediation", "fix_command")
IDENTITY_KEYS = ("exe", "plist", "tap")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
BREW_FORMULA_PATH = re.compile(r"^(?:/opt/homebrew|/usr/local)/(?:Cellar|opt)/([^/]+)/")
BREW_SERVICE_PLIST = re.compile(r"^homebrew\.mxcl\.(.+)\.plist$")


def load_security(path):
    """secure-it's report.json. The user can write that file, so every field is
    checked for type and the page escapes all of it; nothing in it becomes a job."""
    out = {"path": str(path), "found": False, "error": None, "generated_at": None, "alerts": []}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return out
    except (OSError, ValueError, RecursionError) as e:
        return {**out, "found": True, "error": f"could not read it: {e}"}
    out["found"] = True
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SECURITY_SCHEMA:
        schema = data.get("schema") if isinstance(data, dict) else None
        return {**out, "error": f"it has schema {schema!r}; this brewdash reads schema {SECURITY_SCHEMA}"}
    out["generated_at"] = data["generated_at"] if isinstance(data.get("generated_at"), str) else None
    for a in data.get("alerts") if isinstance(data.get("alerts"), list) else []:
        if not isinstance(a, dict) or a.get("severity") not in SEVERITIES or not isinstance(a.get("title"), str):
            continue
        rec = {k: a[k] if isinstance(a.get(k), str) else None for k in ALERT_TEXT}
        rec["severity"] = a["severity"]
        ident = a["identity"] if isinstance(a.get("identity"), dict) else {}
        # Only what link_alert reads: a nested value that loads can still be too deep to encode.
        rec["identity"] = {k: ident[k] for k in IDENTITY_KEYS if isinstance(ident.get(k), str)}
        rec["fingerprint"] = a["fingerprint"] if FINGERPRINT_RE.fullmatch(str(a.get("fingerprint"))) else None
        out["alerts"].append(rec)
    out["alerts"].sort(key=lambda a: SEVERITIES.index(a["severity"]))
    return out


def link_alert(alert, pkg_ids, tap_names):
    """Where in brewdash an alert can be acted on: a package, a tap, the Shell tab, or nowhere."""
    ident, check = alert["identity"], alert["check_id"]
    if check == "exposure.listening":
        m = BREW_FORMULA_PATH.match(str(ident.get("exe") or ""))
        if m and "f:" + m.group(1) in pkg_ids:
            return {"type": "package", "id": "f:" + m.group(1)}
    if check == "persistence.launchd":
        m = BREW_SERVICE_PLIST.match(Path(str(ident.get("plist") or "")).name)
        if m and "f:" + m.group(1) in pkg_ids:
            return {"type": "package", "id": "f:" + m.group(1)}
    if check == "packages.brew_taps" and isinstance(ident.get("tap"), str) and ident["tap"] in tap_names:
        return {"type": "tap", "name": ident["tap"]}
    if check in ("persistence.path_shadow", "persistence.shell_rc"):
        return {"type": "tab", "tab": "shell"}
    return None


# ---------- listening ports, app signatures ----------

LSOF = "/usr/sbin/lsof"
CODESIGN = "/usr/bin/codesign"
LOOPBACK = ("127.", "::1", "localhost")


def run_command(argv, timeout=60):
    p = subprocess.run(argv, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def parse_listening(text):
    """`lsof -F pn` into {pid: [{"address", "port", "exposed"}]}. Connected UDP
    sockets (`a->b`) are not listeners and are left out."""
    out, pid = {}, None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None and "->" not in line:
            address, _, port = line[1:].rpartition(":")
            if not port.isdigit():
                continue
            host = address.strip("[]")
            entry = {"address": address, "port": int(port), "exposed": not host.startswith(LOOPBACK)}
            if entry not in out.setdefault(pid, []):
                out[pid].append(entry)
    return out


def service_ports(model, sysrun):
    """Listening sockets of every running brew service, by package id. lsof
    exits 1 when one of the pids is not visible to this user, so its exit code is not read."""
    by_pid = {p["service"]["pid"]: p["id"] for p in model["packages"]
              if p.get("service") and isinstance(p["service"].get("pid"), int)}
    if not by_pid:
        return {}
    pids = ",".join(str(pid) for pid in sorted(by_pid))
    out = {}
    for proto, sel in (("TCP", ["-iTCP", "-sTCP:LISTEN"]), ("UDP", ["-iUDP"])):
        _, stdout, _ = sysrun([LSOF, "-nP", "-a", "-p", pids, *sel, "-F", "pn"])
        for pid, entries in parse_listening(stdout).items():
            if pid in by_pid:
                out.setdefault(by_pid[pid], []).extend({"proto": proto, **e} for e in entries)
    return out


def classify_signature(stderr):
    """`codesign -dv --verbose=2` output: who signed the app as it says. codesign
    does not verify the signature here, and notarization is not checked."""
    if "code object is not signed at all" in stderr:
        return {"kind": "unsigned", "signer": None}
    authorities = [l[len("Authority="):] for l in stderr.splitlines() if l.startswith("Authority=")]
    first = authorities[0] if authorities else ""
    if first in ("Software Signing", "macOS Software Signing"):
        return {"kind": "apple", "signer": first}
    if first in ("Apple Mac OS Application Signing", "Apple iPhone OS Application Signing"):
        return {"kind": "appstore", "signer": first}
    if first.startswith("Developer ID Application:"):
        return {"kind": "developer_id", "signer": first[len("Developer ID Application:"):].strip()}
    if first:
        return {"kind": "other", "signer": first}
    if "Signature=adhoc" in stderr:
        return {"kind": "adhoc", "signer": None}
    return {"kind": "unknown", "signer": None}


def sign_apps(apps, sysrun):
    def one(app):
        try:
            _, _, err = sysrun([CODESIGN, "-dv", "--verbose=2", app["path"]])
            app["signature"] = classify_signature(err)
        except Exception as e:  # one app must not hide the others
            app["signature"] = {"kind": "unknown", "signer": None, "error": str(e)}
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, apps))


# ---------- brew access ----------

# On Cancel, or with no screen to draw on, the helper prints a line SUDO_RE matches, so the job shows the Terminal hint.
ASKPASS = """#!/bin/sh
/usr/bin/osascript -e 'on run argv' \\
  -e 'text returned of (display dialog (item 1 of argv) default answer "" with hidden answer with title "brewdash" with icon caution)' \\
  -e 'end run' "${1:-brewdash needs your password for sudo}" && exit 0
echo "sudo: a password is required; the brewdash password dialog was cancelled or could not open" >&2
exit 1
"""


def write_askpass(state_dir):
    """A SUDO_ASKPASS helper in `state_dir`: a job has no terminal, so sudo asks in a macOS dialog instead.
    sudo passes its prompt as $1 and reads the password from stdout."""
    d = Path(state_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    path = d / "askpass"
    path.write_text(ASKPASS)
    path.chmod(0o700)
    return str(path)


class Brew:
    def __init__(self, exe=None, askpass=None):
        self.exe = exe or shutil.which("brew") or "/opt/homebrew/bin/brew"
        # brew calls sudo with -A whenever SUDO_ASKPASS is set; one the user set wins.
        self.env = {**({"SUDO_ASKPASS": askpass} if askpass else {}), **os.environ, **BREW_ENV}

    def argv(self, cmd):
        return [self.exe, *cmd[1:]] if cmd[0] == "brew" else cmd

    def run(self, *args, timeout=300):
        p = subprocess.run([self.exe, *args], capture_output=True, text=True, errors="replace",
                           env=self.env, stdin=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def json(self, *args):
        rc, out, err = self.run(*args)
        if rc != 0:
            raise RuntimeError(f"brew {' '.join(args)} failed: {err.strip()[-500:]}")
        return json.loads(out)

    def popen(self, cmd):
        # A new session has no controlling terminal, so sudo fails instead of
        # prompting in the terminal that launched brewdash and stalling the queue.
        return subprocess.Popen(self.argv(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=self.env, text=True, errors="replace",
                                bufsize=1, start_new_session=True)


class Store:
    def __init__(self, brew, home=None, app_dirs=None, sysrun=run_command):
        self.brew = brew
        self.home = home or str(Path.home())
        self.app_dirs = APP_DIRS if app_dirs is None else app_dirs
        self.sysrun = sysrun
        self.security_path = Path(self.home) / "secure-it" / "report.json"
        self._security = (None, None)
        self.apps = None
        self.shell = None
        self.ports = None
        self.lock = threading.Lock()
        self.model = None
        self.error = None
        self.version = 0
        self.refreshing = False
        self.dirty = False
        self.generation = 0
        self.sizes = {}
        self.health = None
        self.last_update = None
        self.prefix = None

    def by_id(self):
        with self.lock:
            return {p["id"]: p for p in self.model["packages"]} if self.model else {}

    def apps_by_id(self):
        with self.lock:
            return {a["id"]: a for a in (self.apps or {}).get("apps", [])}

    def tap_names(self):
        with self.lock:
            return {t["name"] for t in self.model["taps"]} if self.model else set()

    def refresh(self):
        """Re-read brew state. A call during a running refresh makes that refresh read again,
        so a read that started before an action finished is never the last word."""
        with self.lock:
            if self.refreshing:
                self.dirty = True
                return
            self.refreshing = True
        while True:
            with self.lock:
                self.dirty = False
            self._read()
            with self.lock:
                if not self.dirty:
                    self.refreshing = False
                    return

    def _read(self):
        try:
            with ThreadPoolExecutor(6) as ex:
                info = ex.submit(self.brew.json, "info", "--json=v2", "--installed")
                outdated = ex.submit(self.brew.json, "outdated", "--json=v2")
                services = ex.submit(self.brew.json, "services", "info", "--all", "--json")
                taps = ex.submit(self.brew.json, "tap-info", "--json", "--installed")
                auto = ex.submit(self.brew.run, "autoremove", "--dry-run")
                repo = ex.submit(self.brew.run, "--repository")
                prefix = ex.submit(self.brew.run, "--prefix")
            model = build_model(info.result(), outdated.result(), services.result(),
                                taps.result(), parse_autoremove(auto.result()[1]))
            fetch_head = Path(repo.result()[1].strip()) / ".git" / "FETCH_HEAD"
            with self.lock:
                self.model = model
                self.prefix = prefix.result()[1].strip()
                self.last_update = fetch_head.stat().st_mtime if fetch_head.exists() else None
                self.error = None
                self.version += 1
                self.generation += 1
                generation = self.generation
            threading.Thread(target=self._slow, args=(model, generation), daemon=True).start()
        except Exception as e:  # surfaced to the UI instead of killing the server
            with self.lock:
                self.error = str(e)

    def _slow(self, model, generation):
        with ThreadPoolExecutor(5) as ex:
            sizes_f = ex.submit(self._sizes, model)
            doctor = ex.submit(self.brew.run, "doctor")
            cleanup = ex.submit(self.brew.run, "cleanup", "-n")
            env_f = ex.submit(self._environment, model)
            ports_f = ex.submit(service_ports, model, self.sysrun)
        try:
            sizes = sizes_f.result()
        except Exception:
            sizes = {}
        try:
            ports = {"error": None, "by_id": ports_f.result()}
        except Exception as e:
            ports = {"error": f"brewdash could not run lsof: {e}", "by_id": {}}
        try:
            rc, out, err = doctor.result()
            _, c_out, c_err = cleanup.result()
            health = {
                "doctor_ok": rc == 0, "doctor": (out + err).strip(),
                "cleanup_reclaim": parse_cleanup(c_out + c_err),
                "cleanup_preview": [l for l in c_out.splitlines() if l.startswith("Would remove")][:200],
            }
        except Exception as e:
            health = {"doctor_ok": False, "doctor": f"brewdash could not run brew doctor: {e}",
                                 "cleanup_reclaim": None, "cleanup_preview": []}
        try:
            apps, shell = env_f.result()
        except Exception as e:
            apps, shell = {"error": str(e), "apps": []}, {"error": str(e), "path": [], "tools": []}
        with self.lock:
            if generation != self.generation:
                return
            self.sizes = sizes
            self.health = health
            self.apps = apps
            self.shell = shell
            self.ports = ports
            self.version += 1

    def _names(self):
        api = Path(self.brew.run("--cache")[1].strip()) / "api"
        out = {}
        for kind, file in (("formula", "formula_names.txt"), ("cask", "cask_names.txt")):
            out[kind] = set((api / file).read_text().split()) if (api / file).is_file() else set()
        return out

    def _environment(self, model):
        names = self._names()
        brew_paths = {a: p["id"] for p in model["packages"] for a in p.get("app_paths", [])}
        apps = scan_apps(self.app_dirs, brew_paths)
        error = None
        try:
            match_casks(apps, names["cask"], self.brew)
        except Exception as e:  # one unknown token fails the whole brew info call
            error = f"could not match apps to casks: {e}"
        sign_apps(apps, self.sysrun)
        installed = {k: {p["name"] for p in model["packages"] if p["kind"] == k} for k in ("formula", "cask")}
        shell = scan_shell(self.home, self.prefix, names, installed)
        return {"error": error, "apps": apps, "names_known": bool(names["cask"])}, shell

    def _sizes(self, model):
        id_by_path = {}
        for p in model["packages"]:
            sub = "Cellar" if p["kind"] == "formula" else "Caskroom"
            id_by_path[f"{self.prefix}/{sub}/{p['name']}"] = p["id"]
            for app in p.get("app_paths", []):
                id_by_path[app] = p["id"]
        if not id_by_path:
            return {}
        p = subprocess.run(["du", "-sk", *id_by_path], capture_output=True, text=True)
        return parse_du(p.stdout, id_by_path)

    def security(self):
        """secure-it's report, read again only when the file changed. Links are
        made on every call, against the packages and taps brewdash has now."""
        try:
            st = self.security_path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        with self.lock:
            cached_key, report = self._security
        fresh = load_security(self.security_path) if report is None or key != cached_key else None
        with self.lock:
            cached_key, report = self._security
            if fresh and (report is None or key != cached_key):
                report = {**fresh, "seq": report["seq"] + 1 if report else 1}
                self._security = (key, report)
            pkg_ids = {p["id"] for p in self.model["packages"]} if self.model else set()
            taps = {t["name"] for t in self.model["taps"]} if self.model else set()
        return {**report, "alerts": [{**a, "link": link_alert(a, pkg_ids, taps)} for a in report["alerts"]]}

    def snapshot(self):
        security = self.security()
        with self.lock:
            return {"version": self.version, "refreshing": self.refreshing, "error": self.error,
                    "last_update": self.last_update, "model": self.model,
                    "sizes": self.sizes, "health": self.health, "apps": self.apps, "shell": self.shell,
                    "ports": self.ports, "security": security}


class Runner:
    def __init__(self, brew, store, state_dir):
        self.brew, self.store, self.state_dir = brew, store, Path(state_dir)
        self.jobs = []
        self.lock = threading.Lock()
        self.q = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def submit(self, title, commands, snapshot, title_msg=None):
        job = {"id": None, "title": title, "title_msg": title_msg, "commands": [shlex.join(c) for c in commands],
               "status": "queued", "log": [], "exit_code": None, "needs_terminal": False,
               "snapshot": None, "created": time.time(), "finished": None}
        with self.lock:
            job["id"] = len(self.jobs) + 1
            self.jobs.append(job)
        self.q.put((job, commands, snapshot))
        return job

    def view(self, with_log=()):
        """All jobs; the log only for ids in `with_log`, so polling stays small."""
        with self.lock:
            return [{**j, "log": list(j["log"]) if j["id"] in with_log else [], "log_len": len(j["log"])}
                    for j in self.jobs]

    def _append(self, job, line):
        with self.lock:
            job["log"].append(line.rstrip("\n"))

    def _work(self):
        while True:
            job, commands, snapshot = self.q.get()
            with self.lock:
                job["status"] = "running"
            try:
                ok = self._execute(job, commands, snapshot)
            except Exception as e:  # the worker is the only one; it must outlive any job
                self._append(job, f"brewdash error: {e!r}")
                ok = False
            with self.lock:
                job["status"] = "succeeded" if ok else "failed"
                job["finished"] = time.time()
            try:
                self._history(job)
            except OSError as e:
                self._append(job, f"could not write history: {e}")
            self.store.refresh()

    def _execute(self, job, commands, snapshot):
        if snapshot and not self._snapshot(job):
            return False
        for cmd in commands:
            self._append(job, "$ " + shlex.join(cmd))
            with self.brew.popen(cmd) as proc:
                for line in proc.stdout:
                    self._append(job, line)
                rc = proc.wait()
            with self.lock:
                job["exit_code"] = rc
            if rc != 0:
                with self.lock:
                    job["needs_terminal"] = any(SUDO_RE.search(l) for l in job["log"])
                return False
        return True

    def _state_dir(self):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        return self.state_dir

    def _snapshot(self, job):
        path = self._state_dir() / "brewfiles" / time.strftime(f"Brewfile-%Y%m%d-%H%M%S-job{job['id']}")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        cmd = ["brew", "bundle", "dump", f"--file={path}", "--force"]
        self._append(job, "$ " + shlex.join(cmd))
        with self.brew.popen(cmd) as proc:
            for line in proc.stdout:
                self._append(job, line)
            rc = proc.wait()
        if rc != 0:
            self._append(job, "snapshot failed; not running the action")
            return False
        with self.lock:
            job["snapshot"] = str(path)
        return True

    def _history(self, job):
        entry = {k: job[k] for k in ("title", "title_msg", "commands", "status", "exit_code", "snapshot", "created",
                                     "finished")}
        with open(self._state_dir() / "history.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    def history(self, limit=300):
        path = self.state_dir / "history.jsonl"
        if not path.exists():
            return []
        lines = path.read_text().splitlines()[-limit:]
        return [json.loads(l) for l in reversed(lines)]


# ---------- install helpers ----------

def installed_names(pkgs, kind):
    """Every spelling brew prints for an installed package: name, full name, tap/name."""
    return {n for p in pkgs.values() if p["kind"] == kind
            for n in (p["name"], p["full_name"], f"{p['tap']}/{p['name']}" if p["tap"] else p["name"])}


def search(brew, q, pkgs):
    if not NAME_RE.match(q):
        raise ValueError("bad query")
    results = []
    for kind in ("formula", "cask"):
        installed = installed_names(pkgs, kind)
        _, out, _ = brew.run("search", "--" + kind, q)
        for name in out.split():
            results.append({"kind": kind, "name": name, "installed": name in installed})
    return results


def preview(brew, kind, name, pkgs):
    if not NAME_RE.match(name) or kind not in ("formula", "cask"):
        raise ValueError("bad name or kind")
    data = brew.json("info", "--json=v2", "--" + kind, name)
    item = (data["formulae"] or data["casks"])[0]
    new_deps = []
    if kind == "formula":
        rc, out, _ = brew.run("deps", "--formula", name)
        installed = installed_names(pkgs, "formula")
        new_deps = [d for d in out.split() if d not in installed] if rc == 0 else []
    version = item["versions"]["stable"] if kind == "formula" else item["version"]
    # The id the package has once installed: `name` may be tap-qualified, record ids never are.
    pid = "f:" + item["name"] if kind == "formula" else "c:" + item["token"]
    return {"kind": kind, "name": name, "id": pid, "desc": item.get("desc"), "homepage": item.get("homepage"),
            "version": version, "caveats": item.get("caveats"), "new_deps": new_deps}


# ---------- apps outside brew ----------

APP_DIRS = ("/Applications", str(Path.home() / "Applications"))


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def scan_apps(dirs, brew_paths):
    """Apps in `dirs`; `brew_paths` maps an app path a cask installed to that cask's id."""
    apps = []
    for d in dirs:
        for app in sorted(Path(d).glob("*.app")):
            try:
                with open(app / "Contents" / "Info.plist", "rb") as f:
                    plist = plistlib.load(f)
            except (OSError, plistlib.InvalidFileException):
                plist = {}
            path = str(app)
            source = "brew" if path in brew_paths else \
                "appstore" if (app / "Contents" / "_MASReceipt").exists() else "manual"
            apps.append({"id": "a:" + path, "name": app.stem, "path": path,
                         "version": plist.get("CFBundleShortVersionString"), "bundle_id": plist.get("CFBundleIdentifier"),
                         "source": source, "brew_id": brew_paths.get(path), "cask": None, "cask_version": None,
                         "auto_updates": None})
    return apps


def match_casks(apps, cask_names, brew):
    """Set app["cask"] on a hand-installed app where a cask with its slug installs exactly that .app."""
    wanted = {}
    for app in apps:
        if app["source"] == "manual" and _slug(app["name"]) in cask_names:
            wanted.setdefault(_slug(app["name"]), []).append(app)
    if not wanted:
        return
    for c in brew.json("info", "--json=v2", "--cask", *sorted(wanted))["casks"]:
        artifacts = {v for a in c.get("artifacts", []) if isinstance(a, dict) for v in a.get("app", [])
                     if isinstance(v, str)}
        for app in wanted.get(c["token"], []):
            if app["name"] + ".app" in artifacts:
                app.update(cask=c["full_token"], cask_version=c.get("version"), auto_updates=c.get("auto_updates"))


# ---------- shell setup ----------

RC_FILES = (".zprofile", ".zshenv", ".zshrc", ".profile", ".bash_profile", ".bashrc")
PATH_LINE = re.compile(r"""^\s*(?:export\s+)?PATH=(?:"([^"]*)"|'([^']*)'|([^\s"'#;]*))\s*(?:[#;].*)?$""")
TOOLS = (  # (name, pattern in an rc line, brew formula that provides it)
    ("nvm", r"nvm\.sh", "nvm"), ("SDKMAN!", r"sdkman-init\.sh", None), ("Oh My Zsh", r"oh-my-zsh\.sh", None),
    ("fzf", r"fzf\.(zsh|bash)|fzf --(zsh|bash)", "fzf"), ("pyenv", r"pyenv init", "pyenv"),
    ("rbenv", r"rbenv init", "rbenv"), ("jenv", r"jenv init", "jenv"), ("asdf", r"asdf\.sh", "asdf"),
    ("direnv", r"direnv hook", "direnv"), ("starship", r"starship init", "starship"),
    ("zoxide", r"zoxide init", "zoxide"), ("conda", r"conda(\.sh| shell)", None), ("mise", r"mise activate", "mise"),
    ("Homebrew", r"brew shellenv", None),
)
SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
SYNCED_ROOTS = ("Library/CloudStorage", "Library/Mobile Documents", "Dropbox")
PATH_TOOLS = (  # (piece of a PATH entry, kind, brew name that provides the same tool)
    (".jbang/bin", "formula", "jbang"), (".docker/bin", "cask", "docker-desktop"),
    ("/usr/local/share/dotnet", "cask", "dotnet-sdk"), (".codeium/windsurf/bin", "cask", "windsurf"),
    ("JetBrains/Toolbox/scripts", "cask", "jetbrains-toolbox"),
)


def _expand(raw, home):
    if raw.startswith("~"):
        raw = home + raw[1:]
    return re.sub(r"\$\{?HOME\}?", home, raw)


def scan_shell(home, prefix, names, installed):
    """PATH entries and shell-initialised tools from the rc files, with what to change.

    `names` is {"formula": set, "cask": set} of what Homebrew offers; `installed` the same for what is
    installed. Only PATH, source and eval lines are read, so nothing else in the rc files leaves them.
    """
    entries, tools, seen = [], [], set()
    for rc in RC_FILES:
        path = Path(home) / rc
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            m = PATH_LINE.match(line)
            if m:
                ahead = True
                for raw in next(g for g in m.groups() if g is not None).split(":"):
                    if raw in ("$PATH", "${PATH}") or raw.rstrip("/") in SYSTEM_BIN_DIRS:
                        ahead = False
                    if not raw or raw in ("$PATH", "${PATH}"):
                        continue
                    entries.append(_path_entry(rc, n, raw, _expand(raw, home), prefix, names, installed, seen,
                                               ahead and _synced(_expand(raw, home), home)))
                continue
            if not re.match(r"^\s*(\[.*\]\s*&&\s*)?(source|\\?\.|eval)\s", line):
                continue
            for tool, pattern, formula in TOOLS:
                if re.search(pattern, line):
                    via_brew = prefix in line or (formula is not None and formula in installed["formula"])
                    alt = formula if formula and not via_brew and formula in names["formula"] else None
                    tools.append({"name": tool, "file": rc, "line": n, "via_brew": via_brew, "alternative": alt})
    return {"path": entries, "tools": tools}


def _synced(d, home):
    """Whether d sits under a cloud-synced folder. Compared without case: APFS is case-insensitive and
    rc files do write /users/... for /Users/..."""
    real = os.path.realpath(d).lower()
    for root in SYNCED_ROOTS:
        base = os.path.join(home, root)
        if any(real == c or real.startswith(c + "/") for c in {base.lower(), os.path.realpath(base).lower()}):
            return True
    return False


def _path_entry(rc, n, raw, d, prefix, names, installed, seen, synced=False):
    entry = {"file": rc, "line": n, "raw": raw, "dir": d, "exists": None, "issue": None, "suggestion": None,
             "suggestion_msg": None, "alternative": None}
    if "$" in d:
        return entry
    entry["exists"] = Path(d).is_dir()
    shell = "bash" if "bash" in rc or rc == ".profile" else "zsh"
    key = (shell, d.rstrip("/"))
    pinned = re.match(re.escape(prefix) + r"/Cellar/([^/]+)/[^/]+(/.*)?$", d)
    old_prefix = re.match(r"/usr/local/(opt|Cellar)/", d) if prefix != "/usr/local" else None
    advice = None
    if key in seen:
        advice = "duplicate", msg("path.duplicate", file=rc)
    elif synced:
        advice = "synced", msg("path.synced")
    elif pinned:
        fixed = f"{prefix}/opt/{pinned.group(1)}{pinned.group(2) or ''}"
        advice = "pinned", msg("path.pinned" if entry["exists"] else "path.pinned_gone", name=pinned.group(1),
                               fixed=fixed)
    elif old_prefix and not entry["exists"]:
        fixed = prefix + d[len("/usr/local"):]
        advice = "old_prefix", (msg("path.old_prefix", prefix=prefix, fixed=fixed) if Path(fixed).is_dir()
                                else msg("path.old_prefix_gone", prefix=prefix))
    elif not entry["exists"]:
        advice = "missing", msg("path.missing")
    if advice:
        issue, m = advice
        entry.update(issue=issue, suggestion=render(m), suggestion_msg=m)
    seen.add(key)
    for piece, kind, name in PATH_TOOLS:
        if piece in d and name in names[kind] and name not in installed[kind]:
            entry["alternative"] = {"kind": kind, "name": name}
    return entry


# ---------- export list ----------

LIST_TYPES = ("tap", "brew", "cask")
DEFAULT_TAPS = {"homebrew/core", "homebrew/cask"}


def list_item(pid, pkgs, apps, taps):
    """The Brewfile entry for a package, app or tap id."""
    if pid.startswith(("f:", "c:")) and pid in pkgs:
        return {"type": "brew" if pid.startswith("f:") else "cask", "name": pkgs[pid]["full_name"]}
    if pid.startswith("a:") and pid in apps and apps[pid]["cask"]:
        return {"type": "cask", "name": apps[pid]["cask"]}
    if pid.startswith("t:") and pid[2:] in taps:
        return {"type": "tap", "name": pid[2:]}
    raise ValueError(f"{pid} cannot go into a Brewfile")


def brewfile(items):
    taps = {i["name"] for i in items if i["type"] == "tap"}
    taps |= {i["name"].rsplit("/", 1)[0] for i in items if i["type"] != "tap" and "/" in i["name"]}
    lines = ["# Brewfile from brewdash. On the new machine: brew bundle --file=Brewfile", ""]
    lines += [f'tap "{t}"' for t in sorted(taps - DEFAULT_TAPS)]
    for kind in ("brew", "cask"):
        for name in sorted(i["name"] for i in items if i["type"] == kind):
            # A tap package needs trust before brew loads it; dump writes the same option.
            lines.append(f'{kind} "{name}"' + (", trusted: true" if "/" in name else ""))
    return "\n".join(lines) + "\n"


class ExportList:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "list.json"
        self.lock = threading.Lock()

    def items(self):
        with self.lock:
            return self._load()

    def _load(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())["items"]

    def change(self, add=(), remove=()):
        for i in [*add, *remove]:
            if i.get("type") not in LIST_TYPES or not NAME_RE.match(i.get("name") or ""):
                raise ValueError("bad list item")
        with self.lock:
            keep = [i for i in self._load() if i not in remove]
            for i in add:
                if i not in keep:
                    keep.append(i)
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"items": keep}, indent=1))
            return keep


# ---------- self update ----------

GITHUB_API = "https://api.github.com/repos/lemiorhan/brewdash"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CHANNELS = ("stable", "beta")
DEFAULT_SETTINGS = {"channel": "stable", "auto": True}
UPDATE_FIRST_CHECK_SECONDS = 60
UPDATE_CHECK_SECONDS = 3600
# GitHub's compare status, read as what the latest commit is to the installed one.
RELATION = {"ahead": "newer", "behind": "older", "identical": "same", "diverged": "diverged"}


def read_installed(root=ROOT):
    """The commit (and tag) install.sh built this copy from, or None for a checkout, which does not update itself."""
    try:
        v = json.loads((Path(root) / "version.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(v, dict) or not SHA_RE.match(str(v.get("sha"))):
        return None
    tag = v.get("tag")
    return {"sha": v["sha"], "tag": tag if isinstance(tag, str) and TAG_RE.match(tag) else None}


def github_fetch(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "brewdash"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def spawn_installer(installer, ref, app_dir, log):
    # Its own session: install.sh quits the app, and this server with it, before it replaces the app.
    with open(log, "w") as out:
        return subprocess.Popen(["/bin/bash", str(installer)],
                                env={**os.environ, "BREWDASH_REF": ref, "BREWDASH_APP_DIR": str(app_dir)},
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def last_line(path):
    try:
        lines = [ln for ln in re.sub(r"\x1b\[[0-9;]*m", "", Path(path).read_text(errors="replace")).splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1].strip() if lines else ""


class Updater:
    def __init__(self, state_dir, installed, root=ROOT, fetch=github_fetch, spawn=spawn_installer):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "settings.json"
        self.installed = installed
        self.root = Path(root)
        self.fetch, self.spawn = fetch, spawn
        self.lock = threading.Lock()
        self.installing = threading.Lock()
        self.installer = None
        self.status = {"latest": None, "relation": None, "checked": None, "error": None, "started": None}

    def settings(self):
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            saved = {}
        s = dict(DEFAULT_SETTINGS)
        if isinstance(saved, dict):
            if saved.get("channel") in CHANNELS:
                s["channel"] = saved["channel"]
            if isinstance(saved.get("auto"), bool):
                s["auto"] = saved["auto"]
        return s

    def save(self, req):
        s = self.settings()
        if "channel" in req:
            if req["channel"] not in CHANNELS:
                raise ValueError("channel must be stable or beta")
            s["channel"] = req["channel"]
        if "auto" in req:
            if not isinstance(req["auto"], bool):
                raise ValueError("auto must be true or false")
            s["auto"] = req["auto"]
        changed = s["channel"] != self.settings()["channel"]
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.write_text(json.dumps(s))
        if changed:
            with self.lock:
                self.status.update(latest=None, relation=None, checked=None)
        return self.view()

    def view(self):
        with self.lock:
            # install.sh quits this server once it has built the new app, so an installer that ended while the
            # server still runs has failed.
            if self.installer is not None and self.installer.poll() is not None:
                code, self.installer = self.installer.returncode, None
                why = last_line(self.state_dir / "update.log") or f"exit status {code}"
                self.status.update(started=None, error=f"install.sh stopped: {why}")
            return {**self.settings(), "installed": self.installed, **self.status}

    def latest(self, channel):
        """The commit `channel` is at: master's head for beta, the highest vX.Y.Z tag for stable (None if no tag)."""
        if channel == "beta":
            sha = self.fetch(f"{GITHUB_API}/commits/master", "application/vnd.github.sha").strip()
            if not SHA_RE.match(sha):
                raise ValueError("GitHub did not answer with a commit")
            return {"sha": sha, "tag": None}
        tags = [(tuple(map(int, m.groups())), t) for t in json.loads(self.fetch(f"{GITHUB_API}/tags?per_page=100"))
                if isinstance(t, dict) and (m := TAG_RE.match(str(t.get("name"))))]
        if not tags:
            return None
        tag = max(tags, key=lambda vt: vt[0])[1]
        if not SHA_RE.match(str(tag["commit"]["sha"])):
            raise ValueError(f"GitHub did not answer with a commit for {tag['name']}")
        return {"sha": tag["commit"]["sha"], "tag": tag["name"]}

    def check(self):
        if not self.installed:
            raise ValueError("this copy has no version.json from install.sh, so it does not update itself")
        channel = self.settings()["channel"]
        try:
            target = self.latest(channel)
            if not target or target["sha"] == self.installed["sha"]:
                rel = "same" if target else None
            else:
                cmp = json.loads(self.fetch(f"{GITHUB_API}/compare/{self.installed['sha']}...{target['sha']}"))
                rel = RELATION.get(cmp.get("status"), "diverged")
            err = None
        except Exception as e:  # urllib raises OSError, but http.client's IncompleteRead and HTTPException are not
            target, rel, err = None, None, str(e) or type(e).__name__
        with self.lock:
            # The channel changed while GitHub answered: this result is for the old one, and installing it would
            # take the app to a channel the user just left.
            if self.settings()["channel"] == channel:
                self.status.update(latest=target, relation=rel, checked=time.time(), error=err)
        return self.view()

    def update(self, manual):
        """Install the channel's latest commit. Only a click goes back to an older or a side commit."""
        # Two installers at once would both swap brewdash.app; one can land inside the other.
        if not self.installing.acquire(blocking=False):
            return self.view()
        try:
            if self.view()["started"]:
                return self.view()
            view = self.check()
            rel = view["relation"]
            if rel == "newer" or (manual and rel in ("older", "diverged")):
                app = self.root.parents[2]
                if app.name != "brewdash.app":
                    raise ValueError(f"install.sh replaces brewdash.app, but this copy runs from {app}")
                target = view["latest"]
                try:
                    proc = self.spawn(self.root / "install.sh", target["tag"] or target["sha"], app.parent,
                                      self.state_dir / "update.log")
                    started, err = time.time(), None
                except OSError as e:
                    proc, started, err = None, None, f"could not start install.sh: {e}"
                with self.lock:
                    self.installer = proc
                    self.status.update(started=started, error=err)
            return self.view()
        finally:
            self.installing.release()

    def run_hourly(self):
        def loop():
            time.sleep(UPDATE_FIRST_CHECK_SECONDS)
            while True:
                if self.settings()["auto"]:
                    try:
                        self.update(manual=False)
                    except Exception as e:  # the loop must outlive any one failure, or updates stop until a restart
                        with self.lock:
                            self.status.update(error=str(e) or type(e).__name__)
                time.sleep(UPDATE_CHECK_SECONDS)
        if self.installed:
            threading.Thread(target=loop, daemon=True).start()


# ---------- http ----------

def make_handler(store, runner, brew, token, port_ref, export, updater):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", headers=()):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            path = urlparse(self.path).path
            if not authorize(path, self.headers, token, port_ref[0]):
                self._send(403, {"error": "forbidden"})
                return None
            return path

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def do_GET(self):
            path = self._guard()
            if path is None:
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                if path == "/api/state":
                    return self._send(200, store.snapshot())
                if path == "/api/jobs":
                    open_ids = {int(i) for i in qs.get("log", [""])[0].split(",") if i}
                    return self._send(200, runner.view(open_ids))
                if path == "/api/history":
                    return self._send(200, runner.history())
                if path == "/api/settings":
                    return self._send(200, updater.view())
                if path == "/api/search":
                    return self._send(200, search(brew, qs.get("q", [""])[0], store.by_id()))
                if path == "/api/preview":
                    return self._send(200, preview(brew, qs.get("kind", [""])[0], qs.get("name", [""])[0], store.by_id()))
                if path == "/api/list":
                    return self._send(200, self._list(export.items()))
                if path == "/api/brewfile":
                    return self._send(200, brewfile(export.items()).encode(), "text/plain; charset=utf-8",
                                      [("Content-Disposition", 'attachment; filename="Brewfile"')])
            except (ValueError, RuntimeError) as e:
                return self._send(400, {"error": str(e)})
            self._static(path)

        def do_POST(self):
            path = self._guard()
            if path is None:
                return
            try:
                body = self._body()
                if path == "/api/refresh":
                    threading.Thread(target=store.refresh, daemon=True).start()
                    return self._send(202, {"ok": True})
                if path == "/api/settings":
                    return self._send(200, updater.save(body))
                if path == "/api/update":
                    if body.get("op") == "check":
                        return self._send(200, updater.check())
                    if body.get("op") == "now":
                        return self._send(200, updater.update(manual=True))
                    raise ValueError("op must be check or now")
                if path == "/api/impact":
                    pkgs = store.by_id()
                    return self._send(200, removal_impact(pkgs, check_targets(body.get("targets"), pkgs)))
                if path == "/api/plan":
                    title, cmds, snap = plan_job_msg(body, store.by_id(), store.apps_by_id(), store.tap_names())
                    return self._send(200, {"title": render(title), "title_msg": title,
                                            "commands": [shlex.join(c) for c in cmds], "snapshot": snap})
                if path == "/api/jobs":
                    title, cmds, snap = plan_job_msg(body, store.by_id(), store.apps_by_id(), store.tap_names())
                    return self._send(201, runner.submit(render(title), cmds, snap, title))
                if path == "/api/list":
                    if body.get("op") == "add":
                        ids = body.get("ids")
                        if not isinstance(ids, list):
                            raise ValueError("ids must be a list")
                        pkgs, apps, taps = store.by_id(), store.apps_by_id(), store.tap_names()
                        items = export.change(add=[list_item(i, pkgs, apps, taps) for i in ids])
                    elif body.get("op") == "remove":
                        items = body.get("items")
                        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                            raise ValueError("items must be a list of objects")
                        items = export.change(remove=items)
                    else:
                        raise ValueError("op must be add or remove")
                    return self._send(200, self._list(items))
            except ValueError as e:  # includes json.JSONDecodeError
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _list(self, items):
            return {"items": items, "brewfile": brewfile(items)}

        def _static(self, path):
            rel = "index.html" if path == "/" else path.lstrip("/")
            target = (WEB / rel).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            self._send(200, target.read_bytes(), STATIC_TYPES.get(target.suffix, "application/octet-stream"))

    return Handler


def serve(port=0, open_browser=True, brew=None, state_dir=STATE_DIR, refresh=True, home=None, app_dirs=None,
          sysrun=run_command, updater=None):
    brew = brew or Brew(askpass=write_askpass(state_dir))
    updater = updater or Updater(state_dir, read_installed())
    store = Store(brew, home, app_dirs, sysrun)
    runner = Runner(brew, store, state_dir)
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, runner, brew, token, port_ref,
                                                                   ExportList(state_dir), updater))
    port_ref[0] = server.server_address[1]
    if refresh:
        threading.Thread(target=store.refresh, daemon=True).start()
    url = f"http://127.0.0.1:{port_ref[0]}/?t={token}"
    return server, url, token


PARENT_POLL_SECONDS = 2


def stop_with(pid, server, interval=PARENT_POLL_SECONDS):
    """Shuts `server` down once process `pid` is gone, so a crashed or force-quit brewdash.app leaves no server behind."""
    def watch():
        while True:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):  # PermissionError: the pid went to another user's process
                server.shutdown()
                return
            time.sleep(interval)
    threading.Thread(target=watch, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="Local web dashboard for Homebrew")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--parent", type=int, metavar="PID", help="stop when the process with this PID exits")
    args = ap.parse_args()
    updater = Updater(STATE_DIR, read_installed())
    server, url, _ = serve(args.port, updater=updater)
    updater.run_hourly()
    if args.parent:
        stop_with(args.parent, server)
    print(f"brewdash running at {url}\nCtrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
