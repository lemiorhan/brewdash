#!/usr/bin/env python3
"""brewdash: a local web dashboard for Homebrew. Run `python3 brewdash.py`."""

import argparse
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
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
STATIC_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}


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
        "deps": [], "dependents": [], "missing_deps": [], "service": None, "orphan": False,
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
        "deps": [], "dependents": [], "missing_deps": [], "service": None, "orphan": False,
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


REASONS = {
    "versioned_formula": "it is an old versioned formula",
    "unsupported": "it is no longer supported",
    "deprecated_upstream": "upstream has deprecated it",
    "fails_gatekeeper_check": "it fails the macOS Gatekeeper check",
    "unmaintained": "it is not maintained",
    "discontinued": "upstream has discontinued it",
    "no_license": "it has no license",
    "repo_archived": "its upstream repository is archived",
    "does_not_build": "it does not build",
}


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

    Each entry: {level, title, text, alternatives: [installed ids], actions: [{label, req}]}.
    The reqs are plan_job requests, so the UI runs them through the normal confirm flow.
    """
    out = []
    pid, name = rec["id"], rec["name"]

    def entry(level, title, text, alternatives=(), actions=()):
        out.append({"level": level, "title": title, "text": text,
                    "alternatives": list(alternatives), "actions": list(actions)})

    def install(kind, target):
        return {"label": f"Install {target}", "req": {"action": "install", "kind": kind, "name": target}}

    uninstall = {"label": f"Uninstall {name}", "req": {"action": "uninstall", "targets": [pid]}}

    if rec["outdated"] and not rec["disabled"]:
        if rec["pinned"]:
            entry("info", f"Pinned at {rec['version']}; brew upgrade skips it",
                  f"Unpin it first to upgrade to {rec['latest']}.",
                  actions=[{"label": "Unpin", "req": {"action": "unpin", "targets": [pid]}}])
        else:
            entry("info", f"Update available: {rec['version']} → {rec['latest']}",
                  "Nothing blocks the upgrade.",
                  actions=[{"label": "Upgrade", "req": {"action": "upgrade", "targets": [pid]}}])

    if rec["disabled"] or rec["deprecated"]:
        kind = "disable" if rec["disabled"] else "deprecation"
        reason = item.get(f"{kind}_reason")
        why = REASONS.get(reason, reason.replace("_", " ") if reason else None)
        if rec["disabled"]:
            when = f" on {item['disable_date']}" if item.get("disable_date") else ""
            parts = [f"Homebrew disabled it{when}: brew will not install or upgrade it any more. "
                     "The installed copy stays until you remove it."]
            if rec["outdated"]:
                parts.append(f"Version {rec['latest']} exists, but not through brew.")
        else:
            parts = ["It still works, and Homebrew plans to drop it"
                     + (f": it will be disabled on {item['disable_date']}." if item.get("disable_date") else ".")]
        alternatives, actions, missing_repl = [], [], False
        for rkind, field in (("formula", f"{kind}_replacement_formula"), ("cask", f"{kind}_replacement_cask")):
            repl = item.get(field)
            if repl:
                rid = (resolve if rkind == "formula" else cask_resolve).get(repl)
                if rid:
                    alternatives.append(rid)
                else:
                    missing_repl = True
                    parts.append(f"Homebrew suggests {repl} instead, and it is not installed.")
                    actions.append(install(rkind, repl))
        if rec["kind"] == "formula":
            newer, newest = _newer_family(rec, item, pkgs, resolve)
            supported = [i for i in newer if not pkgs[i]["deprecated"] and not pkgs[i]["disabled"]]
            alternatives += [i for i in supported if i not in alternatives]
            if newer and not supported:
                parts.append("The newer versions installed are deprecated or disabled too.")
            if not alternatives and newest and not missing_repl:
                parts.append(f"No supported newer version is installed; the newest versioned one is {newest}.")
                actions.append(install("formula", newest))
        if alternatives and not actions:
            parts.append("A newer or replacement package is already installed, so nothing needs to be installed.")
        elif alternatives:
            parts.append("A newer or replacement package is already installed.")
        if rec["dependents"]:
            parts.append(f"{len(rec['dependents'])} installed package(s) still depend on it; it can be removed "
                         "once they are upgraded off it or removed.")
        else:
            if rec["kind"] == "cask":
                parts.append("Nothing installed depends on it; remove it if you no longer use the app.")
            elif rec["requested"]:
                parts.append("Nothing installed depends on it, so it can be removed once nothing of yours "
                             "needs this exact version.")
            else:
                parts.append("Nothing installed depends on it and it was installed only as a dependency, "
                             "so it is safe to remove.")
            actions.append(uninstall)
        entry("warn", f"{'Disabled' if rec['disabled'] else 'Deprecated'} by Homebrew"
              + (f": {why}" if why else ""), " ".join(parts), alternatives, actions)

    for dep in rec["missing_deps"]:
        dep_kind = "cask" if rec["kind"] == "cask" and dep in ((item.get("depends_on") or {}).get("cask") or []) \
            else "formula"
        base = dep.partition("@")[0]
        same = sorted({p["id"] for p in pkgs.values() if p["kind"] == "formula"
                       and p["name"].partition("@")[0] == base}) if dep_kind == "formula" else []
        text = f"{name} was installed against {dep}, which is not installed now."
        if same:
            text += " Another version of it is installed, but this package needs exactly that one."
        actions = []
        if rec["outdated"] and not rec["disabled"] and not rec["pinned"]:
            text += " Upgrading brings in whatever the new version needs; otherwise install it."
            actions.append({"label": f"Upgrade {name}", "req": {"action": "upgrade", "targets": [pid]}})
        else:
            text += f" Installing it should repair {name}; if brew no longer has it, remove {name} instead."
        actions.append(install(dep_kind, dep))
        entry("warn", f"Missing dependency: {dep}", text, same, actions)
    return out


def removal_impact(pkgs, selection):
    """What removing `selection` (package ids) does to the rest of the install."""
    selected = set(selection)
    blockers = sorted({d for s in selected for d in pkgs[s]["dependents"] if d not in selected})
    removed = set(selected)
    new_orphans = set()
    changed = True
    while changed:
        changed = False
        for p in pkgs.values():
            if p["id"] in removed or p["kind"] != "formula" or p["requested"] or not p["dependents"]:
                continue
            if all(d in removed for d in p["dependents"]):
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


def plan_job(req, pkgs):
    """Turn an API request into (title, [argv...], snapshot?). Raises ValueError on bad input."""
    action = req.get("action")
    targets = check_targets(req.get("targets"), pkgs)
    formulae = [pkgs[t]["name"] for t in targets if pkgs[t]["kind"] == "formula"]
    casks = [pkgs[t]["name"] for t in targets if pkgs[t]["kind"] == "cask"]

    def split(verb):
        cmds = []
        if formulae:
            cmds.append(["brew", verb, "--formula", *formulae])
        if casks:
            cmds.append(["brew", verb, "--cask", *casks])
        if not cmds:
            raise ValueError("no targets")
        return cmds

    names = ", ".join(formulae + casks)
    if action in ("upgrade", "pin", "unpin"):
        return f"{action} {names}", split(action), len(targets) > 1
    if action == "uninstall":
        return f"uninstall {names}", split("uninstall"), True
    if action == "upgrade_all":
        return "upgrade all outdated", [["brew", "upgrade"]], True
    if action == "update":
        return "brew update", [["brew", "update"]], False
    if action == "autoremove":
        return "autoremove", [["brew", "autoremove"]], True
    if action == "cleanup":
        return "cleanup", [["brew", "cleanup"]], False
    if action == "service":
        op = req.get("op")
        if op not in SERVICE_OPS or casks or len(formulae) != 1:
            raise ValueError("service needs one formula and op start|stop|restart")
        if pkgs[targets[0]]["service"] is None:
            raise ValueError("formula has no service")
        return f"service {op} {formulae[0]}", [["brew", "services", op, formulae[0]]], False
    if action == "install":
        name, kind = req.get("name") or "", req.get("kind")
        if not NAME_RE.match(name) or kind not in ("formula", "cask"):
            raise ValueError("bad install name or kind")
        return f"install {name}", [["brew", "install", "--" + kind, name]], False
    raise ValueError(f"unknown action {action!r}")


def authorize(path, headers, token, port):
    if headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return False
    if path.startswith("/api/"):
        given = (headers.get("X-Brewdash-Token") or "").encode("utf-8", "surrogateescape")
        return secrets.compare_digest(given, token.encode())
    return True


# ---------- brew access ----------

class Brew:
    def __init__(self, exe=None):
        self.exe = exe or shutil.which("brew") or "/opt/homebrew/bin/brew"
        self.env = {**os.environ, **BREW_ENV}

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
    def __init__(self, brew):
        self.brew = brew
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
        with ThreadPoolExecutor(3) as ex:
            sizes_f = ex.submit(self._sizes, model)
            doctor = ex.submit(self.brew.run, "doctor")
            cleanup = ex.submit(self.brew.run, "cleanup", "-n")
        try:
            sizes = sizes_f.result()
        except Exception:
            sizes = {}
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
        with self.lock:
            if generation != self.generation:
                return
            self.sizes = sizes
            self.health = health
            self.version += 1

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

    def snapshot(self):
        with self.lock:
            return {"version": self.version, "refreshing": self.refreshing, "error": self.error,
                    "last_update": self.last_update, "model": self.model,
                    "sizes": self.sizes, "health": self.health}


class Runner:
    def __init__(self, brew, store, state_dir):
        self.brew, self.store, self.state_dir = brew, store, Path(state_dir)
        self.jobs = []
        self.lock = threading.Lock()
        self.q = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def submit(self, title, commands, snapshot):
        job = {"id": None, "title": title, "commands": [shlex.join(c) for c in commands],
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
        entry = {k: job[k] for k in ("title", "commands", "status", "exit_code", "snapshot", "created", "finished")}
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


# ---------- http ----------

def make_handler(store, runner, brew, token, port_ref):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
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
                if path == "/api/search":
                    return self._send(200, search(brew, qs.get("q", [""])[0], store.by_id()))
                if path == "/api/preview":
                    return self._send(200, preview(brew, qs.get("kind", [""])[0], qs.get("name", [""])[0], store.by_id()))
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
                if path == "/api/impact":
                    pkgs = store.by_id()
                    return self._send(200, removal_impact(pkgs, check_targets(body.get("targets"), pkgs)))
                if path == "/api/plan":
                    title, cmds, snap = plan_job(body, store.by_id())
                    return self._send(200, {"title": title, "commands": [shlex.join(c) for c in cmds], "snapshot": snap})
                if path == "/api/jobs":
                    title, cmds, snap = plan_job(body, store.by_id())
                    return self._send(201, runner.submit(title, cmds, snap))
            except ValueError as e:  # includes json.JSONDecodeError
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _static(self, path):
            rel = "index.html" if path == "/" else path.lstrip("/")
            target = (WEB / rel).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            self._send(200, target.read_bytes(), STATIC_TYPES.get(target.suffix, "application/octet-stream"))

    return Handler


def serve(port=0, open_browser=True, brew=None, state_dir=STATE_DIR, refresh=True):
    brew = brew or Brew()
    store = Store(brew)
    runner = Runner(brew, store, state_dir)
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, runner, brew, token, port_ref))
    port_ref[0] = server.server_address[1]
    if refresh:
        threading.Thread(target=store.refresh, daemon=True).start()
    url = f"http://127.0.0.1:{port_ref[0]}/?t={token}"
    return server, url, token


def main():
    ap = argparse.ArgumentParser(description="Local web dashboard for Homebrew")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    server, url, _ = serve(args.port)
    print(f"brewdash running at {url}\nCtrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
