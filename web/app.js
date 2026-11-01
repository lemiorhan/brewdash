"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const link = (url) => /^https?:\/\//.test(url || "") ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>` : esc(url || "–");

async function api(path, body) {
  const opts = { headers: { "X-Brewdash-Token": TOKEN, "Content-Type": "application/json" } };
  if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const S = {
  snap: null, version: -1, pkgs: new Map(),
  tab: "overview", q: "", flags: new Set(), sort: { key: "name", dir: 1 },
  selected: new Set(), current: null, depth: 1,
  jobs: [], openJobs: new Set(), history: [],
  search: { q: "", results: null, preview: null, busy: false },
  list: { items: [], brewfile: "" },
};

const TABS = ["overview", "requested", "deps", "casks", "services", "taps", "health", "security", "apps", "shell", "history",
  "install", "export", "settings"];
const tabLabel = (k) => t("tab." + k);

// ---------- formatting ----------

const fmtSize = (kb) => kb == null ? "…" : kb > 1048576 ? (kb / 1048576).toFixed(1) + " GB" : kb > 1024 ? (kb / 1024).toFixed(0) + " MB" : kb + " KB";
const fmtDate = (ts) => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "";
const fmtAgo = (ts) => {
  if (!ts) return t("ago.never");
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 60 ? t("ago.min", { n: m }) : m < 2880 ? t("ago.h", { n: Math.round(m / 60) }) : t("ago.days", { n: Math.round(m / 1440) });
};
const sizeOf = (id) => S.snap?.sizes?.[id];

const descTitle = (p) => p?.desc ? ` title="${esc(p.desc)}"` : "";

const upgradable = (p) => p.outdated && !p.disabled && !p.pinned;

function badges(p) {
  let out = "";
  if (p.outdated) out += `<span class="badge b-outdated">${t("badge.outdated")}</span>`;
  if (p.pinned) out += `<span class="badge b-pinned">${t("badge.pinned")}</span>`;
  if (p.orphan) out += `<span class="badge b-orphan">${t("badge.orphan")}</span>`;
  if (p.missing_deps.length) out += `<span class="badge b-missing">${t("badge.missing")}</span>`;
  if (p.deprecated) out += `<span class="badge b-deprecated">${t("badge.deprecated")}</span>`;
  if (p.disabled) out += `<span class="badge b-disabled">${t("badge.disabled")}</span>`;
  if (p.service) out += `<span class="badge b-service">${t("badge.service")}</span>`;
  return out;
}

// ---------- data ----------

async function loadState() {
  try {
    const snap = await api("/api/state");
    $("#error").classList.toggle("hidden", !snap.error);
    $("#error").textContent = snap.error ? t("error.brew", { error: snap.error }) : "";
    if (snap.version === S.version && snap.refreshing === S.snap?.refreshing && snap.security?.seq === S.snap?.security?.seq) return;
    S.snap = snap; S.version = snap.version;
    S.pkgs = new Map((snap.model?.packages || []).map((p) => [p.id, p]));
    for (const id of [...S.selected]) if (!itemFor(id)) S.selected.delete(id);
    if (S.current && !S.pkgs.has(S.current)) S.current = null;
    replaceUrl();
    renderAll();
  } catch (e) {
    $("#error").classList.remove("hidden");
    $("#error").textContent = t("error.server", { error: e.message });
  }
}

async function loadList(change) {
  try { S.list = await api("/api/list", change); }
  catch (e) { if (change) alertBox(e.message); return; }
  renderTabs(); renderBulk();
  if (S.tab === "export") renderExport();
}

const appsList = () => S.snap?.apps?.apps || [];
const alerts = () => S.snap?.security?.alerts || [];
const portsOf = (id) => S.snap?.ports?.by_id?.[id] || [];
const alertsFor = (type, key) => alerts().filter((a) => a.link?.type === type && (a.link.id === key || a.link.name === key));

// The Brewfile line an id stands for, or null when it cannot go into a Brewfile.
function itemFor(id) {
  const p = S.pkgs.get(id);
  if (p) return { type: p.kind === "formula" ? "brew" : "cask", name: p.full_name };
  if (id.startsWith("a:")) { const a = appsList().find((x) => x.id === id); return a?.cask ? { type: "cask", name: a.cask } : null; }
  if (id.startsWith("t:") && (S.snap?.model?.taps || []).some((t) => "t:" + t.name === id)) return { type: "tap", name: id.slice(2) };
  return null;
}
const inList = (item) => S.list.items.some((i) => i.type === item.type && i.name === item.name);

async function loadJobs() {
  try {
    S.jobs = await api("/api/jobs?log=" + [...S.openJobs].join(","));
    renderJobs();
  } catch (e) { /* state poll reports connectivity */ }
}

// ---------- header, tabs ----------

function counts() {
  const all = [...S.pkgs.values()];
  return {
    requested: all.filter((p) => p.kind === "formula" && p.requested).length,
    deps: all.filter((p) => p.kind === "formula" && !p.requested).length,
    casks: all.filter((p) => p.kind === "cask").length,
    services: all.filter((p) => p.service).length,
    taps: S.snap?.model?.taps?.length || 0,
    apps: S.snap?.apps ? appsList().length : null,
    shell: S.snap?.shell ? (S.snap.shell.path || []).filter((e) => e.issue).length : null,
    security: S.snap?.security?.found ? alerts().length : null,
    export: S.list.items.length,
    outdated: all.filter((p) => p.outdated).length,
    upgradable: all.filter(upgradable).length,
    orphans: all.filter((p) => p.orphan).length,
    missing: all.filter((p) => p.missing_deps.length).length,
  };
}

function renderHeader() {
  const c = counts();
  const total = Object.values(S.snap?.sizes || {}).reduce((a, b) => a + b, 0);
  const stat = (key, n, style = "") => `<span class="stat"${style}>${t(key, { n, v: `<b>${n}</b>` })}</span>`;
  $("#stats").innerHTML = [
    stat("stat.packages", S.pkgs.size),
    stat("stat.outdated", c.outdated),
    stat("stat.orphans", c.orphans),
    c.missing ? stat("stat.missing", c.missing, ` style="border-color:var(--red)"`) : "",
    stat("stat.disk", total ? fmtSize(total) : "…"),
    S.snap?.refreshing ? `<span class="stat">${t("stat.refreshing")}</span>` : "",
  ].join("");
  $("#last-update").textContent = t("header.last_update", { ago: fmtAgo(S.snap?.last_update) });
  $("#btn-upgrade-all").textContent = t("header.upgrade_all", { n: c.upgradable });
  $("#btn-upgrade-all").disabled = !c.upgradable;
  $("#btn-autoremove").textContent = t("header.autoremove", { n: c.orphans });
  $("#btn-autoremove").disabled = !c.orphans;
}

function renderTabs() {
  const c = counts();
  $("#tabs").innerHTML = TABS.map((k) =>
    `<button data-tab="${k}" class="${S.tab === k ? "active" : ""}">${tabLabel(k)}${c[k] != null ? `<span class="count">${c[k]}</span>` : ""}</button>`
  ).join("");
}

// ---------- package tables ----------

const PKG_TABS = {
  requested: (p) => p.kind === "formula" && p.requested,
  deps: (p) => p.kind === "formula" && !p.requested,
  casks: (p) => p.kind === "cask",
};
const FLAGS = [["outdated", (p) => p.outdated], ["upgradable", upgradable], ["orphan", (p) => p.orphan], ["pinned", (p) => p.pinned], ["missing", (p) => p.missing_deps.length > 0], ["service", (p) => !!p.service]];
const COLS = [
  ["name", (p) => p.name], ["version", (p) => p.version], ["latest", (p) => p.latest],
  ["size", (p) => sizeOf(p.id) ?? -1, "num"], ["installed", (p) => p.installed_time || 0, "num"],
  ["usedby", (p) => p.dependents.length, "num"], ["deps", (p) => p.deps.length, "num"],
  ["desc", (p) => p.desc || ""],
];

function visiblePackages() {
  const q = S.q.trim().toLowerCase();
  let list = [...S.pkgs.values()].filter(PKG_TABS[S.tab]);
  for (const [k, fn] of FLAGS) if (S.flags.has(k)) list = list.filter(fn);
  if (q) list = list.filter((p) => p.name.toLowerCase().includes(q) || (p.desc || "").toLowerCase().includes(q) || p.display_name.toLowerCase().includes(q));
  const col = COLS.find((c) => c[0] === S.sort.key);
  list.sort((a, b) => {
    const x = col[1](a), y = col[1](b);
    return (x < y ? -1 : x > y ? 1 : 0) * S.sort.dir;
  });
  return list;
}

function renderPackages() {
  const list = visiblePackages();
  const allSel = list.length && list.every((p) => S.selected.has(p.id));
  const arrow = (k) => S.sort.key === k ? (S.sort.dir > 0 ? " ▲" : " ▼") : "";
  $("#content").innerHTML = `
    <div class="toolbar">
      <input type="search" id="q" placeholder="${t("pkgs.search")}" value="${esc(S.q)}">
      ${FLAGS.map(([k]) => `<label><input type="checkbox" data-flag="${k}" ${S.flags.has(k) ? "checked" : ""}> ${t("flag." + k)}</label>`).join("")}
      <span class="muted">${t("pkgs.shown", { n: list.length })}</span>
    </div>
    <table>
      <thead><tr><th><input type="checkbox" id="sel-all" ${allSel ? "checked" : ""}></th>
        ${COLS.map(([k, , cls]) => `<th data-sort="${k}" class="${cls || ""}">${t("col." + k)}${arrow(k)}</th>`).join("")}</tr></thead>
      <tbody>${list.map((p) => `
        <tr class="row ${S.current === p.id ? "current" : ""}" data-id="${esc(p.id)}">
          <td><input type="checkbox" data-sel="${esc(p.id)}" ${S.selected.has(p.id) ? "checked" : ""}></td>
          <td class="name"${descTitle(p)}>${esc(p.name)}${badges(p)}</td>
          <td class="mono">${esc(p.version)}</td>
          <td class="mono">${p.outdated ? esc(p.latest) : ""}</td>
          <td class="num">${fmtSize(sizeOf(p.id))}</td>
          <td class="num">${fmtDate(p.installed_time)}</td>
          <td class="num">${p.dependents.length || ""}</td>
          <td class="num">${p.deps.length || ""}</td>
          <td class="desc">${esc(p.desc)}</td>
        </tr>`).join("")}</tbody>
    </table>`;
  const q = $("#q");
  q.addEventListener("input", () => { S.q = q.value; const pos = q.selectionStart; renderPackages(); $("#q").focus(); $("#q").setSelectionRange(pos, pos); });
}

// ---------- detail panel ----------

let cy = null;

function chip(id) {
  const p = S.pkgs.get(id);
  return `<span class="chip" data-goto="${esc(id)}"${descTitle(p)}>${esc(p ? p.name : id)}${p?.kind === "cask" ? ` (${t("detail.cask")})` : ""}</span>`;
}

// All parts translated or none: a paragraph half in English reads worse than one wholly in English.
function adviceText(a) {
  const parts = a.msg?.text.map((m) => tm(m, null));
  return parts && parts.every((x) => x != null) ? parts.join(" ") : a.text;
}

function renderAdvice(p) {
  if (!p.advice.length) return "";
  return `<div class="detail-section"><h4>${t("detail.what_to_do")}</h4>${p.advice.map((a, i) => `
    <div class="${a.level === "warn" ? "warn" : "info"}"><b>${esc(tm(a.msg?.title, a.title))}</b><div style="margin-top:4px">${esc(adviceText(a))}</div>
      ${a.alternatives.length ? `<div class="chips" style="margin-top:6px"><span class="muted">${t("detail.already_installed")}</span>${a.alternatives.map(chip).join("")}</div>` : ""}
      ${a.actions.length ? `<div class="btnrow" style="margin-top:8px">${a.actions.map((x, k) => `<button class="small" data-advice="${i}:${k}">${esc(tm(x.msg, x.label))}</button>`).join("")}</div>` : ""}
    </div>`).join("")}</div>`;
}

function renderDetail() {
  const el = $("#detail");
  const p = PKG_TABS[S.tab] ? S.pkgs.get(S.current) : null;
  if (!p) { el.classList.add("hidden"); if (cy) { cy.destroy(); cy = null; } return; }
  el.classList.remove("hidden");
  const svc = p.service;
  const indirect = p.required_by.filter((id) => !p.dependents.includes(id));
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:start">
      <div><h2 class="detail-title">${esc(p.name)}</h2><div>${p.kind === "cask" ? `<span class="badge b-cask">${t("detail.cask")}</span>` : ""}${badges(p)}</div></div>
      <button class="small" data-close>✕</button>
    </div>
    <p>${esc(p.desc || t("detail.no_desc"))}</p>
    ${renderAdvice(p)}
    ${alertsFor("package", p.id).length ? `<div class="detail-section"><h4>${t("detail.findings")}</h4>${alertsFor("package", p.id).map(alertCard).join("")}</div>` : ""}
    <div class="kv">
      ${p.kind === "cask" && p.display_name !== p.name ? `<div>${t("detail.app")}</div><div>${esc(p.display_name)}</div>` : ""}
      <div>${t("col.version")}</div><div class="mono">${esc(p.version)}${p.outdated ? ` → <b style="color:var(--accent)">${esc(p.latest)}</b>` : ""}</div>
      <div>${t("detail.homepage")}</div><div>${link(p.homepage)}</div>
      <div>${t("detail.tap")}</div><div class="mono">${esc(p.tap || "–")}</div>
      ${p.license ? `<div>${t("detail.license")}</div><div>${esc(p.license)}</div>` : ""}
      <div>${t("col.installed")}</div><div>${fmtDate(p.installed_time)} ${p.kind === "formula" ? (p.requested ? t("detail.by_you") : t("detail.as_dependency")) : ""}</div>
      <div>${t("col.size")}</div><div>${fmtSize(sizeOf(p.id))}</div>
      ${p.keg_only ? `<div>${t("detail.keg_only")}</div><div>${t("detail.keg_only_text")}</div>` : ""}
    </div>
    <div class="detail-section btnrow">
      ${upgradable(p) ? `<button class="accent" data-act="upgrade">${t("btn.upgrade")}</button>` : ""}
      <button data-act="${p.pinned ? "unpin" : "pin"}">${p.pinned ? t("btn.unpin") : t("btn.pin")}</button>
      <button class="danger" data-act="uninstall">${t("btn.uninstall")}</button>
    </div>
    ${svc ? `<div class="detail-section"><h4>${t("detail.service")}</h4>
      <div class="kv"><div>${t("detail.status")}</div><div>${esc(svc.status)}${svc.pid ? ` (pid ${svc.pid})` : ""}</div>
      ${svc.log_path ? `<div>${t("detail.log")}</div><div class="mono">${esc(svc.log_path)}</div>` : ""}
      ${portsOf(p.id).length ? `<div>${t("detail.listening")}</div><div>${listening(p.id)}</div>` : ""}</div>
      <div class="btnrow" style="margin-top:6px">${svcButtons(p)}</div></div>` : ""}
    <div class="detail-section"><h4>${t("detail.depends_on", { n: p.deps.length })}</h4><div class="chips">${p.deps.map(chip).join("") || `<span class="muted">${t("detail.nothing")}</span>`}</div></div>
    <div class="detail-section"><h4>${t("detail.used_by", { n: p.dependents.length })}</h4><div class="chips">${p.dependents.map(chip).join("") || `<span class="muted">${t("detail.no_dependents")}</span>`}</div>
      ${indirect.length ? `<div class="chips" style="margin-top:6px"><span class="muted">${t("detail.indirect")}</span>${indirect.map(chip).join("")}</div>` : ""}</div>
    <div class="detail-section"><h4>${t("detail.graph")}
      <select id="depth" style="margin-left:8px">${[1, 2, 3].map((n) => `<option value="${n}" ${S.depth === n ? "selected" : ""}>${t("detail.hops", { n })}</option>`).join("")}</select></h4>
      <div id="graph"></div>
      <div class="legend"><span><i class="dot" style="background:var(--accent)"></i>${t("legend.selected")}</span><span><i class="dot" style="background:var(--blue)"></i>${t("legend.by_you")}</span><span><i class="dot" style="background:var(--graph-dep)"></i>${t("legend.dependency")}</span><span><i class="dot" style="background:var(--purple)"></i>${t("detail.cask")}</span><span>${t("legend.arrow")}</span></div>
    </div>
    ${p.caveats ? `<div class="detail-section"><h4>${t("detail.caveats")}</h4><pre>${esc(p.caveats)}</pre></div>` : ""}`;
  renderGraph(p);
}

function neighbourhood(rootId, depth) {
  const seen = new Set([rootId]);
  let frontier = [rootId];
  for (let i = 0; i < depth && seen.size < 200; i++) {
    const next = [];
    for (const id of frontier) {
      const p = S.pkgs.get(id);
      for (const n of [...p.deps, ...p.dependents]) if (!seen.has(n)) { seen.add(n); next.push(n); }
    }
    frontier = next;
  }
  const edges = [];
  for (const id of seen) for (const d of S.pkgs.get(id).deps) if (seen.has(d)) edges.push([id, d]);
  return { nodes: [...seen], edges };
}

function renderGraph(p) {
  if (cy) cy.destroy();
  const { nodes, edges } = neighbourhood(p.id, S.depth);
  const v = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const color = (id) => {
    const n = S.pkgs.get(id);
    return id === p.id ? v("--accent") : n.kind === "cask" ? v("--purple") : n.requested ? v("--blue") : v("--graph-dep");
  };
  cy = cytoscape({
    container: $("#graph"),
    elements: [
      ...nodes.map((id) => ({ data: { id, label: S.pkgs.get(id).name, color: color(id), border: S.pkgs.get(id).outdated ? v("--accent") : v("--code-bg") } })),
      ...edges.map(([s, t]) => ({ data: { id: s + ">" + t, source: s, target: t } })),
    ],
    style: [
      { selector: "node", style: { "background-color": "data(color)", label: "data(label)", color: v("--text"), "font-size": 9, "text-valign": "bottom", "text-margin-y": 3, width: 14, height: 14, "border-width": 2, "border-color": "data(border)" } },
      { selector: `node[id = "${p.id}"]`, style: { width: 22, height: 22, "font-size": 11, "font-weight": 700 } },
      { selector: "edge", style: { width: 1, "line-color": v("--graph-edge"), "target-arrow-color": v("--graph-edge"), "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.7 } },
    ],
    maxZoom: 1.6,
    layout: nodes.length > 40 ? { name: "cose", animate: false, nodeRepulsion: 9000 } : { name: "breadthfirst", directed: true, spacingFactor: 1.1, padding: 10 },
  });
  cy.on("tap", "node", (e) => { if (e.target.id() !== p.id) select(e.target.id()); });
}

function select(id) {
  S.current = id;
  const p = S.pkgs.get(id);
  const tab = p.kind === "cask" ? "casks" : p.requested ? "requested" : "deps";
  if (S.tab !== tab) { S.tab = tab; $("#view").scrollTop = 0; renderTabs(); renderView(); }
  else document.querySelectorAll("tr.row").forEach((tr) => tr.classList.toggle("current", tr.dataset.id === id));
  renderDetail();
  syncUrl();
}

// ---------- navigation ----------

const navHash = () => {
  const p = new URLSearchParams({ tab: S.tab });
  if (S.current) p.set("pkg", S.current);
  if (S.flags.size) p.set("flags", [...S.flags].join(","));
  return "#" + p;
};
const page = (hash) => { const p = new URLSearchParams(hash.slice(1)); return `${p.get("tab")}|${p.get("pkg")}`; };

// Each entry carries its position, so popstate can tell Back from Forward.
let navIndex = history.state?.i ?? 0;

// A filter toggle replaces the history entry; only a new tab or package adds one.
function syncUrl() {
  const h = navHash();
  if (h === location.hash) return;
  if (page(h) === page(location.hash)) history.replaceState({ i: navIndex }, "", h);
  else history.pushState({ i: ++navIndex }, "", h);
}

function replaceUrl() {
  if (location.hash !== navHash()) history.replaceState({ i: navIndex }, "", navHash());
}

function applyUrl() {
  const p = new URLSearchParams(location.hash.slice(1));
  S.tab = TABS.includes(p.get("tab")) ? p.get("tab") : "overview";
  S.current = p.get("pkg");
  if (S.snap?.model && !S.pkgs.has(S.current)) S.current = null;
  S.flags = new Set(p.get("flags")?.split(",").filter((f) => FLAGS.some(([k]) => k === f)));
  replaceUrl();
}

// History cannot drop an entry, so the one of a package brew no longer has turns into a copy of
// its neighbour; stepping onto it would change nothing on screen, so it is stepped over.
addEventListener("popstate", (e) => {
  const shown = navHash();
  const i = e.state?.i ?? 0;
  const dir = i < navIndex ? -1 : 1;
  navIndex = i;
  applyUrl();
  if (navHash() === shown && i > 0) { history.go(dir); return; }
  $("#view").scrollTop = 0; renderTabs(); renderView(); renderDetail();
});

// ---------- other tabs ----------

function listening(id) {
  return portsOf(id).map((e) => `<span class="port mono ${e.exposed ? "exposed" : ""}" title="${e.exposed ? t("port.exposed") : t("port.local")}">${esc(e.proto)} ${esc(e.address)}:${esc(e.port)}</span>`).join(" ");
}

const SVC_OPS = ["start", "stop", "restart"];
const svcButtons = (p) => SVC_OPS.map((op) => `<button class="small" data-svc="${op}" data-id="${esc(p.id)}">${t("svc." + op)}</button>`).join("");

function renderServices() {
  const list = [...S.pkgs.values()].filter((p) => p.service);
  const ports = S.snap?.ports;
  $("#content").innerHTML = `${ports?.error ? `<div class="warn">${esc(ports.error)}</div>` : ""}
    <table><thead><tr><th>${t("detail.service")}</th><th>${t("detail.status")}</th><th>PID</th><th>${t("svc.user")}</th><th>${t("detail.listening")}</th><th>Plist</th><th></th></tr></thead><tbody>
    ${list.map((p) => `<tr><td class="name"><span class="chip" data-goto="${esc(p.id)}"${descTitle(p)}>${esc(p.name)}</span></td>
      <td class="st ${p.service.running ? "st-succeeded" : "st-queued"}">${esc(p.service.status)}</td>
      <td class="mono">${esc(p.service.pid || "")}</td><td>${esc(p.service.user || "")}</td>
      <td>${!ports ? `<span class="muted">…</span>` : listening(p.id) || `<span class="muted">${p.service.pid ? t("svc.none") : ""}</span>`}</td>
      <td class="mono muted">${esc(p.service.file || "")}</td>
      <td class="btnrow">${svcButtons(p)}</td></tr>`).join("")}
  </tbody></table>${list.length ? "" : `<p class="muted">${t("svc.empty")}</p>`}`;
}

const REPO = "https://github.com/lemiorhan/brewdash";

// brewdash.py's CHANNELS and the values of its RELATION.
const CHANNELS = ["stable", "beta"];
const RELATIONS = ["same", "newer", "older", "diverged"];
const versionName = (v) => v ? v.tag || v.sha.slice(0, 7) : "";

async function loadSettings(req) {
  try { S.settings = await (req ? api(req.path, req.body) : api("/api/settings")); }
  // While an update runs, no answer is the expected one: install.sh has quit the server to replace the app.
  catch (e) { if (!S.settings?.started) S.settings = { ...(S.settings || {}), error: e.message }; }
  // A failed install.sh leaves the server running; asking again, on any tab, is how the page learns it stopped.
  if (S.settings.started && !S.settingsPoll) S.settingsPoll = setTimeout(() => { S.settingsPoll = null; loadSettings(); }, 3000);
  if (S.tab === "settings") renderView();
}

function updatesCard(u) {
  if (!u) return `<p class="loading">${t("settings.reading")}</p>`;
  if (!u.installed) return u.error ? `<div class="warn">${esc(u.error)}</div>` : `<div class="info">${t("settings.not_installed")}</div>`;
  const latest = versionName(u.latest);
  const status = u.error ? `<div class="warn">${esc(u.error)}</div>`
    : u.started ? `<div class="info">${t("settings.started", { version: esc(latest) })}</div>`
    : !u.checked ? ""
    : !u.latest ? `<p class="muted">${t("settings.no_stable")}</p>`
    : !RELATIONS.includes(u.relation) ? ""
    : `<p>${t("settings.rel." + u.relation, { version: `<b class="mono">${esc(latest)}</b>` })}</p>`;
  const canUpdate = !u.started && ["newer", "older", "diverged"].includes(u.relation);
  const radio = (ch) => `<label class="choice"><input type="radio" name="channel" data-channel="${ch}" ${u.channel === ch ? "checked" : ""}>
    <b>${t("settings.channel." + ch)}</b> <span class="muted">${t("settings.channel." + ch + "_help")}</span></label>`;
  return `<table><tbody>
      <tr><td>${t("settings.installed")}</td><td class="mono">${esc(versionName(u.installed))}</td></tr>
      <tr><td>${t("settings.channel")}</td><td>${CHANNELS.map(radio).join("<br>")}</td></tr>
      <tr><td>${t("settings.auto")}</td><td><label class="choice"><input type="checkbox" data-auto ${u.auto ? "checked" : ""}> <span class="muted">${t("settings.auto_help")}</span></label></td></tr>
      <tr><td>${t("settings.checked")}</td><td>${fmtAgo(u.checked)}</td></tr>
    </tbody></table>
    ${status}
    <div class="btnrow"><button data-update="check">${t("settings.check")}</button>
      <button class="accent" data-update="now" ${canUpdate ? "" : "disabled"}>${u.relation === "older" ? t("settings.go_back", { version: esc(latest) }) : t("settings.update", { version: esc(latest) })}</button></div>`;
}

function renderSettings() {
  if (S.settings === undefined) { S.settings = null; loadSettings(); }
  $("#content").innerHTML = `<div class="card settings"><h3>${t("settings.updates")}</h3>${updatesCard(S.settings)}</div>
    <div class="card contact"><h3>brewdash</h3><table><tbody>
    <tr><td>${t("contact.author")}</td><td>Lemi Orhan Ergin · ${link("https://github.com/lemiorhan")}</td></tr>
    <tr><td>${t("contact.source")}</td><td>${link(REPO)}</td></tr>
    <tr><td>${t("contact.bugs")}</td><td>${link(REPO + "/issues")}</td></tr>
    <tr><td>${t("contact.security")}</td><td>${link(REPO + "/security/advisories/new")}</td></tr>
    <tr><td>${t("detail.license")}</td><td>Apache License 2.0</td></tr>
  </tbody></table></div>`;
}

function renderTaps() {
  const taps = S.snap?.model?.taps || [];
  $("#content").innerHTML = `<table><thead><tr><th></th><th>${t("detail.tap")}</th><th>${t("taps.remote")}</th><th class="num">${t("taps.installed")}</th><th class="num">${t("taps.formulae")}</th><th class="num">${t("tab.casks")}</th><th>${t("taps.last_commit")}</th></tr></thead><tbody>
    ${taps.map((tp) => `<tr><td><input type="checkbox" data-sel="t:${esc(tp.name)}" ${S.selected.has("t:" + tp.name) ? "checked" : ""}></td><td class="name">${esc(tp.name)}${tp.official ? `<span class="badge b-service">${t("taps.official")}</span>` : ""}${tp.private ? `<span class="badge b-pinned">${t("taps.private")}</span>` : ""}${alertsFor("tap", tp.name).length ? `<span class="badge b-missing" data-tab="security" title="${t("taps.flagged")}">secure-it</span>` : ""}</td>
      <td class="mono muted">${esc(tp.remote || "")}</td><td class="num">${tp.installed.length || `<span class="muted">${t("taps.unused")}</span>`}</td>
      <td class="num">${tp.formula_count}</td><td class="num">${tp.cask_count}</td><td class="muted">${esc(tp.last_commit || "")}</td></tr>
      ${tp.installed.length && !tp.official ? `<tr><td></td><td></td><td colspan="5"><div class="chips">${tp.installed.map(chip).join("")}</div></td></tr>` : ""}`).join("")}
  </tbody></table>`;
}

function renderHealth() {
  const h = S.snap?.health;
  const all = [...S.pkgs.values()];
  const missing = all.filter((p) => p.missing_deps.length);
  const deprecated = all.filter((p) => p.deprecated || p.disabled);
  const orphans = all.filter((p) => p.orphan);
  $("#content").innerHTML = `<div class="cards">
    <div class="card"><h3>${t("health.missing", { n: missing.length })}</h3>
      ${missing.map((p) => `<div style="margin-bottom:4px">${t("health.needs", { pkg: chip(p.id), deps: `<span class="mono" style="color:var(--red)">${esc(p.missing_deps.join(", "))}</span>` })}</div>`).join("") || `<span class="muted">${t("health.none")}</span>`}</div>
    <div class="card"><h3>${t("health.orphans", { n: orphans.length })}</h3>
      <p class="muted">${t("health.orphans_text")}</p>
      <div class="chips">${orphans.map((p) => chip(p.id)).join("") || `<span class="muted">${t("health.none")}</span>`}</div>
      ${orphans.length ? `<div style="margin-top:10px"><button class="danger" data-global="autoremove">${t("health.autoremove", { n: orphans.length })}</button></div>` : ""}</div>
    <div class="card"><h3>${t("health.deprecated", { n: deprecated.length })}</h3>
      <div class="chips">${deprecated.map((p) => chip(p.id)).join("") || `<span class="muted">${t("health.none")}</span>`}</div></div>
    <div class="card"><h3>${t("health.cleanup")}</h3>
      ${h ? `<p>${h.cleanup_reclaim ? t("health.can_free", { size: `<b>${esc(h.cleanup_reclaim)}</b>` }) : t("health.nothing_to_clean")}</p>
        ${h.cleanup_preview.length ? `<pre>${esc(h.cleanup_preview.join("\n"))}</pre>` : ""}
        ${h.cleanup_reclaim ? `<div style="margin-top:10px"><button class="accent" data-global="cleanup">${t("health.run_cleanup")}</button></div>` : ""}` : `<p class="loading">${t("common.computing")}</p>`}</div>
    <div class="card" style="grid-column:1/-1"><h3>brew doctor ${h ? (h.doctor_ok ? `<span class="st st-succeeded">${t("common.ok")}</span>` : `<span class="st st-failed">${t("health.warnings")}</span>`) : ""}</h3>
      ${h ? `<pre>${esc(h.doctor || "Your system is ready to brew.")}</pre>` : `<p class="loading">${t("health.running_doctor")}</p>`}</div>
  </div>`;
}

const SIGNATURE = {
  apple: "ok", appstore: "ok", developer_id: "ok", other: "muted", adhoc: "bad", unsigned: "bad", unknown: "muted",
};
// macos/build-app.sh sets this id and signs ad-hoc: brewdash.app is built on the Mac it runs on.
const BREWDASH_ID = "io.github.lemiorhan.brewdash";
const builtHere = (a) => a.bundle_id === BREWDASH_ID && a.signature?.kind === "adhoc";
const weakSignature = (a) => ["adhoc", "unsigned"].includes(a.signature?.kind) && !builtHere(a);

function signature(sig, app) {
  if (!sig) return `<span class="muted">…</span>`;
  if (app && builtHere(app)) return `<span class="muted">${t("sig.built_here")}</span>`;
  const kind = SIGNATURE[sig.kind] ? sig.kind : "unknown";
  const cls = SIGNATURE[kind];
  return `<span class="${cls === "bad" ? "sig-bad" : cls}" title="${esc(sig.error || sig.signer || "")}">${t("sig." + kind)}</span>${kind === "developer_id" || kind === "other" ? ` <span class="muted">${esc(sig.signer)}</span>` : ""}`;
}

function renderApps() {
  const env = S.snap?.apps;
  if (!env) { $("#content").innerHTML = `<p class="loading">${t("apps.scanning")}</p>`; return; }
  const apps = appsList();
  const adoptable = apps.filter((a) => a.source === "manual" && a.cask);
  const row = (a) => {
    const sel = a.source === "brew" ? a.brew_id : a.cask ? a.id : null;
    const newer = a.cask_version && a.version && !a.cask_version.startsWith(a.version) ? ` <span class="muted">${t("apps.cask_has", { version: esc(a.cask_version.split(",")[0]) })}</span>` : "";
    const action = a.source === "brew" ? `<span class="chip" data-goto="${esc(a.brew_id)}"${descTitle(S.pkgs.get(a.brew_id))}>${esc(S.pkgs.get(a.brew_id)?.name || a.brew_id)}</span> <span class="muted">${t("apps.in_casks")}</span>`
      : a.source === "appstore" ? `<span class="muted">${t("apps.via_store")}</span>`
      : a.cask ? `<button class="small accent" data-adopt="${esc(a.id)}">${t("apps.adopt")}</button> <span class="mono">${esc(a.cask)}</span>${newer}${a.auto_updates ? ` <span class="muted">${t("apps.auto_updates")}</span>` : ""}`
      : `<button class="small" data-findcask="${esc(a.name)}">${t("apps.search_cask")}</button>`;
    // Apple's own apps ship with macOS, and trashing brewdash would pull the app out from under itself.
    const remove = a.source === "brew" ? `<button class="small danger" data-uninstall="${esc(a.brew_id)}">${t("btn.uninstall")}</button>`
      : builtHere(a) || a.signature?.kind === "apple" ? ""
      : `<button class="small danger" data-trash="${esc(a.id)}">${t("apps.trash")}</button>`;
    return `<tr><td>${sel ? `<input type="checkbox" data-sel="${esc(sel)}" ${S.selected.has(sel) ? "checked" : ""}>` : ""}</td>
      <td class="name">${esc(a.name)}</td><td class="mono">${esc(a.version || "")}</td><td>${t("source." + a.source)}</td><td>${signature(a.signature, a)}</td><td>${action}</td><td>${remove}</td></tr>`;
  };
  $("#content").innerHTML = `
    ${env.error ? `<div class="warn">${esc(env.error)}</div>` : ""}
    ${env.names_known ? "" : `<div class="info">${t("apps.no_cask_list")}</div>`}
    <p class="muted">${t("apps.adopt_help", { n: adoptable.length })}</p>
    <p class="muted">${t("apps.signature_help")}</p>
    <p class="muted">${t("apps.trash_help")}</p>
    <table><thead><tr><th></th><th>${t("detail.app")}</th><th>${t("col.version")}</th><th>${t("apps.installed_by")}</th><th>${t("apps.signature")}</th><th>Brew</th><th></th></tr></thead>
    <tbody>${apps.map(row).join("")}</tbody></table>`;
}

function renderShell() {
  const sh = S.snap?.shell;
  if (!sh) { $("#content").innerHTML = `<p class="loading">${t("shell.reading")}</p>`; return; }
  const install = (alt) => alt ? `<button class="small" data-install="${esc(alt.kind)}:${esc(alt.name)}">${t("shell.install", { name: esc(alt.name) })}</button>` : "";
  $("#content").innerHTML = `
    ${sh.error ? `<div class="warn">${esc(sh.error)}</div>` : ""}    <h3>PATH</h3>
    <table><thead><tr><th>${t("shell.file")}</th><th>${t("shell.entry")}</th><th>${t("detail.status")}</th><th>${t("detail.what_to_do")}</th></tr></thead><tbody>
    ${sh.path.map((e) => `<tr><td class="mono">${esc(e.file)}:${e.line}</td><td class="mono">${esc(e.raw)}</td>
      <td>${e.issue ? `<span class="badge b-missing">${t("issue." + e.issue)}</span>` : e.exists === null ? `<span class="muted">${t("shell.not_checked")}</span>` : `<span class="ok">${t("common.ok")}</span>`}</td>
      <td>${esc(tm(e.suggestion_msg, e.suggestion || ""))} ${e.alternative ? `<div style="margin-top:4px"><span class="muted">${t("shell.brew_has")}</span> ${install(e.alternative)}</div>` : ""}</td></tr>`).join("")}
    </tbody></table>
    <h3>${t("shell.tools")}</h3>
    <table><thead><tr><th>${t("shell.tool")}</th><th>${t("shell.file")}</th><th>${t("shell.from_brew")}</th><th></th></tr></thead><tbody>
    ${sh.tools.map((tool) => `<tr><td class="name">${esc(tool.name)}</td><td class="mono">${esc(tool.file)}:${tool.line}</td>
      <td>${tool.via_brew ? `<span class="ok">${t("common.yes")}</span>` : t("common.no")}</td>
      <td>${tool.alternative ? `${install({ kind: "formula", name: tool.alternative })} <span class="muted">${t("shell.then_point", { file: `${esc(tool.file)}:${tool.line}` })}</span>` : ""}</td></tr>`).join("")}
    </tbody></table>`;
}

// ---------- secure-it ----------

const SEVERITY_LEVEL = { critical: "critical", high: "critical", medium: "warning", low: "info" };

function alertLink(l) {
  if (!l) return "";
  if (l.type === "package") return `<div class="chips" style="margin-top:6px"><span class="muted">${t("sec.package")}</span>${chip(l.id)}</div>`;
  if (l.type === "tap") return `<div style="margin-top:6px"><button class="small" data-tab="taps">${t("sec.open_tap", { name: esc(l.name) })}</button></div>`;
  const label = TABS.includes(l.tab) ? tabLabel(l.tab) : l.tab;
  return `<div style="margin-top:6px"><button class="small" data-tab="${esc(l.tab)}">${t("sec.open", { name: esc(label) })}</button></div>`;
}

const SEVERITIES = ["critical", "high", "medium", "low"];
const sevLabel = (s) => SEVERITIES.includes(s) ? t("sev." + s) : s;

// Commands are shown to copy, never run: brewdash does not run what a file in the user's home tells it to.
function alertCard(a) {
  const accept = a.fingerprint ? `sudo secure-it accept ${a.fingerprint.slice(0, 12)} --note "why this is fine"` : "";
  return `<div class="alert sev-${esc(a.severity)}">
    <div><span class="sev">${esc(sevLabel(a.severity))}</span> <b>${esc(a.title)}</b> <span class="muted mono">${esc(a.check_id)}</span></div>
    ${a.detail ? `<div class="muted" style="margin-top:4px">${esc(a.detail)}</div>` : ""}
    ${a.remediation ? `<div style="margin-top:6px">${esc(a.remediation)}</div>` : ""}
    ${a.fix_command ? `<div class="cmd"><span class="muted">${t("sec.fix")}</span><pre>${esc(a.fix_command)}</pre><button class="small" data-copytext="${esc(a.fix_command)}">${t("btn.copy")}</button></div>` : ""}
    ${accept ? `<div class="cmd"><span class="muted">${t("sec.if_expected")}</span><pre>${esc(accept)}</pre><button class="small" data-copytext="${esc(accept)}">${t("btn.copy")}</button></div>` : ""}
    ${alertLink(a.link)}</div>`;
}

function renderSecurity() {
  const sec = S.snap?.security;
  if (!sec?.found) {
    $("#content").innerHTML = `<div class="card"><h3>${t("sec.not_setup")}</h3>
      <p>${t("sec.not_setup_text", { link: `<a href="https://github.com/lemiorhan/secure-it" target="_blank" rel="noopener">secure-it</a>`, path: `<span class="mono">${esc(sec?.path || "~/secure-it/report.json")}</span>` })}</p>
      <pre>git clone https://github.com/lemiorhan/secure-it.git\ncd secure-it && sudo ./install.sh\nsudo secure-it scan</pre></div>`;
    return;
  }
  const list = alerts();
  const by = (s) => list.filter((a) => a.severity === s).length;
  $("#content").innerHTML = `
    ${sec.error ? `<div class="warn">${t("sec.unusable", { path: esc(sec.path), error: esc(sec.error) })}</div>` : ""}
    <p class="muted">${t("sec.from", { path: `<span class="mono">${esc(sec.path)}</span>`, time: esc(sec.generated_at || t("sec.unknown_time")) })}
      ${SEVERITIES.map((s) => by(s) ? `<span class="sev-count sev-${s}">${by(s)} ${sevLabel(s)}</span>` : "").join(" ")}</p>
    ${list.length ? list.map(alertCard).join("") : sec.error ? "" : `<p class="ok">✔ ${t("sec.nothing_open")}</p>`}`;
}

// ---------- overview ----------

const STALE_UPDATE_DAYS = 7;
const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)"];

const daysSinceUpdate = () => S.snap?.last_update ? (Date.now() / 1000 - S.snap.last_update) / 86400 : Infinity;

function attention() {
  const all = [...S.pkgs.values()];
  const h = S.snap?.health;
  const items = [];
  const add = (level, count, title, text, go) => { if (count) items.push({ level, count, title, text, go }); };
  // A package table filters within one tab, with its flags ANDed; one item per tab keeps the click showing exactly what the item counts.
  const addPerTab = (level, flags, key, text) => {
    for (const tab of TABS.filter((k) => PKG_TABS[k])) {
      const hits = all.filter((p) => PKG_TABS[tab](p) && flags.every((f) => FLAGS.find(([k]) => k === f)[1](p)));
      add(level, hits.length, t(key, { n: hits.length, tab: tabLabel(tab) }), text(hits.map((p) => p.name).join(", ")), `${tab}:${flags.join(",")}`);
    }
  };
  const names = (xs) => xs.map((x) => x.name).join(", ");
  const missing = all.filter((p) => p.missing_deps.length);
  add("critical", missing.length, t("att.missing", { n: missing.length }), t("att.missing_text", { names: names(missing) }), "health");
  const dead = all.filter((p) => p.deprecated || p.disabled);
  add("critical", dead.length, t("att.dead", { n: dead.length }), t("att.dead_text", { names: names(dead) }), "health");
  if (h) add("warning", !h.doctor_ok, t("att.doctor"), t("att.doctor_text"), "health");
  addPerTab("warning", ["upgradable"], "att.upgradable", (list) => t("att.upgradable_text", { names: list }));
  addPerTab("warning", ["outdated", "pinned"], "att.pinned", (list) => t("att.pinned_text", { names: list }));
  add("warning", daysSinceUpdate() > STALE_UPDATE_DAYS, t("att.stale"), t("att.stale_text", { ago: fmtAgo(S.snap?.last_update) }), "update");
  const pathIssues = (S.snap?.shell?.path || []).filter((e) => e.issue);
  const synced = pathIssues.filter((e) => e.issue === "synced");
  add("warning", synced.length, t("att.synced", { n: synced.length }), t("att.synced_text", { names: synced.map((e) => e.raw).join(", ") }), "shell");
  add("warning", pathIssues.length - synced.length, t("att.path", { n: pathIssues.length - synced.length }), t("att.path_text"), "shell");
  for (const level of ["critical", "warning", "info"]) {
    const hits = alerts().filter((a) => SEVERITY_LEVEL[a.severity] === level);
    add(level, hits.length, t("att.findings", { n: hits.length }), hits.slice(0, 3).map((a) => a.title).join("; ") + (hits.length > 3 ? "; …" : ""), "security");
  }
  if (S.snap?.security?.error) add("warning", true, t("att.report_unreadable"), S.snap.security.error, "security");
  const exposed = all.filter((p) => portsOf(p.id).some((e) => e.exposed));
  add("warning", exposed.length, t("att.exposed", { n: exposed.length }), t("att.exposed_text", { names: names(exposed) }), "services");
  const weak = appsList().filter(weakSignature);
  add("warning", weak.length, t("att.weak", { n: weak.length }), t("att.weak_text", { names: names(weak) }), "apps");
  const orphans = all.filter((p) => p.orphan);
  add("info", orphans.length, t("att.orphans", { n: orphans.length }), t("att.orphans_text"), "health");
  add("info", !!h?.cleanup_reclaim, t("att.cleanup"), t("att.cleanup_text", { size: h?.cleanup_reclaim }), "health");
  const adoptable = appsList().filter((a) => a.source === "manual" && a.cask);
  add("info", adoptable.length, t("att.adoptable", { n: adoptable.length }), t("att.adoptable_text", { names: names(adoptable) }), "apps");
  const unusedTaps = (S.snap?.model?.taps || []).filter((tp) => !tp.official && !tp.installed.length);
  add("info", unusedTaps.length, t("att.unused_taps", { n: unusedTaps.length }), t("att.unused_taps_text", { names: names(unusedTaps) }), "taps");
  const rank = ["critical", "warning", "info"];
  return items.sort((a, b) => rank.indexOf(a.level) - rank.indexOf(b.level));
}

const LEVEL = { critical: ["✖", "var(--red)"], warning: ["▲", "var(--accent)"], info: ["●", "var(--blue)"] };

function tile(value, label, sub = "", tone = "") {
  return `<div class="tile"${tone ? ` style="border-color:${tone}"` : ""}><div class="tile-value">${value}</div><div class="tile-label">${label}</div>${sub ? `<div class="tile-sub muted">${sub}</div>` : ""}</div>`;
}

// One horizontal bar split into segments, with a legend that carries the numbers.
function stackChart(title, parts) {
  const total = parts.reduce((a, p) => a + p.value, 0);
  if (!total) return `<div class="card"><h3>${title}</h3><p class="muted">${t("ov.nothing_yet")}</p></div>`;
  const shown = parts.filter((p) => p.value);
  return `<div class="card"><h3>${title}</h3>
    <div class="stack">${shown.map((p) => `<div class="seg" style="flex:${p.value};background:${p.color}" title="${esc(p.label)}: ${p.value} (${Math.round(100 * p.value / total)}%)"${p.go ? ` data-overview="${p.go}"` : ""}></div>`).join("")}</div>
    <div class="legend">${shown.map((p) => `<span><span class="dot" style="background:${p.color}"></span>${p.value} ${esc(p.label)}</span>`).join("")}</div></div>`;
}

// Horizontal bars for one measure. A bar with an `id` opens that package; a bar with `pkgs` is a group and names them.
function barChart(title, rows, fmt, empty, caption = "") {
  const max = Math.max(...rows.map((r) => r.value), 0);
  const more = (r) => r.id ? (S.pkgs.get(r.id)?.desc ? " — " + esc(S.pkgs.get(r.id).desc) : "") : r.pkgs?.length ? "\n" + esc(nameList(r.pkgs)) : "";
  return `<div class="card"><h3>${title}</h3>${caption ? `<p class="caption muted">${caption}</p>` : ""}${rows.length && max ? `<div class="bars">${rows.map((r) => `
    <div class="bar-row"${r.id ? ` data-goto="${esc(r.id)}"` : ""} title="${esc(r.label)}: ${esc(fmt(r.value))}${more(r)}">
      <span class="bar-label mono">${esc(r.label)}</span>
      <span class="bar-track">${r.value ? `<span class="bar" style="width:${(100 * r.value / max).toFixed(1)}%"></span>` : ""}</span>
      <span class="bar-value mono">${esc(fmt(r.value))}</span></div>`).join("")}</div>` : `<p class="muted">${empty}</p>`}</div>`;
}

const MONTHS_SHOWN = 24;

const monthOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;

function installsChart() {
  const now = new Date();
  const months = [...Array(MONTHS_SHOWN)].map((_, i) => monthOf(new Date(now.getFullYear(), now.getMonth() - (MONTHS_SHOWN - 1 - i), 1)));
  const byMonth = Object.fromEntries(months.map((m) => [m, []]));
  let earlier = 0;
  for (const p of S.pkgs.values()) {
    if (!p.installed_time) continue;
    const m = monthOf(new Date(p.installed_time * 1000));
    if (byMonth[m]) byMonth[m].push(p.name); else if (m < months[0]) earlier++;
  }
  const max = Math.max(...months.map((m) => byMonth[m].length), 0);
  return `<div class="card"><h3>${t("ov.per_month", { n: MONTHS_SHOWN })}</h3>
    ${max ? `<div class="months">${months.map((m) => {
      const n = byMonth[m].length;
      const names = byMonth[m].slice(0, 12).join(", ") + (n > 12 ? ", …" : "");
      return `<div class="month" title="${m}: ${n}${n ? " (" + esc(names) + ")" : ""}"><span class="month-bar" style="height:${(100 * n / max).toFixed(1)}%"></span></div>`;
    }).join("")}</div>
    <div class="months-axis muted"><span>${months[0]}</span><span>${months[months.length - 1]}</span></div>` : `<p class="muted">${t("ov.none_in_period")}</p>`}
    ${earlier ? `<p class="muted">${t("ov.earlier", { n: earlier, month: months[0] })}</p>` : ""}</div>`;
}

const sum = (xs) => xs.reduce((a, b) => a + b, 0);
const nameList = (ps) => ps.map((p) => p.name).sort((a, b) => a.localeCompare(b)).slice(0, 20).join(", ") + (ps.length > 20 ? ", …" : "");

// [key, packages] pairs, the largest group first.
function groupsBy(ps, key) {
  const m = new Map();
  for (const p of ps) {
    const k = key(p);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(p);
  }
  return [...m].sort(([a, x], [b, y]) => y.length - x.length || a.localeCompare(b));
}

// For each package: everything it pulls in, directly or through others, and its longest chain of depends-on links.
function dependencyReach() {
  const memo = new Map();
  const walk = (id) => {
    if (!memo.has(id)) {
      const r = { all: new Set(), chain: [id] };
      memo.set(id, r);
      let longest = [];
      for (const d of S.pkgs.get(id).deps) {
        const sub = walk(d);
        r.all.add(d); sub.all.forEach((x) => r.all.add(x));
        if (sub.chain.length > longest.length) longest = sub.chain;
      }
      r.chain.push(...longest);
    }
    return memo.get(id);
  };
  for (const id of S.pkgs.keys()) walk(id);
  return memo;
}

// The packages sharing the highest score, or the lowest; a null score leaves a package out.
function best(pkgs, score, lowest = false) {
  let top = [], value = null;
  for (const p of pkgs) {
    const v = score(p);
    if (v == null) continue;
    if (value === null || (lowest ? v < value : v > value)) { top = [p]; value = v; }
    else if (v === value) top.push(p);
  }
  return { top: top.sort((a, b) => a.name.localeCompare(b.name)), value };
}

function recordTile(title, { top, value }, fmt, note) {
  if (!top.length) return "";
  const [p, ...ties] = top;
  const goto = (x) => `<span class="rec-also" data-goto="${esc(x.id)}"${descTitle(x)}>${esc(x.name)}</span>`;
  const more = ties.slice(3);
  const also = ties.length ? t("rec.also", { names: ties.slice(0, 3).map(goto).join(", ") + (more.length ? ` <span title="${esc(nameList(more))}">${t("rec.more", { n: more.length })}</span>` : "") }) : "";
  const sub = [note ? esc(note(p)) : "", also].filter(Boolean).join(" · ");
  return `<div class="rec" data-goto="${esc(p.id)}"${descTitle(p)}>
    <div class="rec-title">${title}</div>
    <div class="rec-name mono">${esc(p.name)}</div>
    <div class="rec-value">${esc(fmt(value, p))}</div>
    ${sub ? `<div class="rec-sub muted">${sub}</div>` : ""}</div>`;
}

const DATE_VERSION = /^(19|20)\d\d([.-]|\d\d|$)/;
// A version that starts with a year, such as 2026-10-20 or 2026.4.1, would outrank every other one.
const majorVersion = (p) => { const m = /^\d+/.exec(p.version); return m && !DATE_VERSION.test(p.version) ? +m[0] : null; };

function recordsCard(all, sizes, reach) {
  const sized = all.filter((p) => sizes[p.id] != null);
  const pulled = (p) => reach.get(p.id).all;
  const withDeps = (p) => sizes[p.id] + sum([...pulled(p)].map((id) => sizes[id] || 0));
  const count = (key) => (n) => t(key, { n });
  const tiles = [
    recordTile(t("rec.largest"), best(sized, (p) => sizes[p.id]), fmtSize),
    recordTile(t("rec.smallest"), best(sized, (p) => sizes[p.id], true), fmtSize),
    recordTile(t("rec.heaviest"), best(sized, (p) => pulled(p).size ? withDeps(p) : null), fmtSize,
      (p) => t("rec.heaviest_note", { size: fmtSize(sizes[p.id]), n: pulled(p).size })),
    recordTile(t("rec.most_deps"), best(all, (p) => pulled(p).size || null), count("unit.packages"), (p) => t("rec.direct", { n: p.deps.length })),
    recordTile(t("rec.longest_chain"), best(all, (p) => reach.get(p.id).chain.length - 1 || null), count("unit.levels"),
      (p) => reach.get(p.id).chain.map((id) => S.pkgs.get(id).name).join(" → ")),
    recordTile(t("rec.oldest"), best(all, (p) => p.installed_time || null, true), fmtDate, (p) => fmtAgo(p.installed_time)),
    recordTile(t("rec.latest"), best(all, (p) => p.installed_time || null), fmtDate, (p) => fmtAgo(p.installed_time)),
    recordTile(t("rec.shortest_name"), best(all, (p) => p.name.length, true), count("unit.characters")),
    recordTile(t("rec.longest_name"), best(all, (p) => p.name.length), count("unit.characters")),
    recordTile(t("rec.highest_version"), best(all, majorVersion), (_, p) => p.version),
    recordTile(t("rec.longest_caveats"), best(all, (p) => p.caveats ? p.caveats.split("\n").length : null), count("unit.lines")),
  ].join("");
  return `<div class="card records"><h3>${t("rec.title")}</h3>${tiles ? `<div class="recs">${tiles}</div>` : `<p class="muted">${t("ov.nothing_installed")}</p>`}</div>`;
}

function numbersCard(all, sizes, reach) {
  const formulae = all.filter((p) => p.kind === "formula");
  const mine = formulae.filter((p) => p.requested);
  const kb = all.map((p) => sizes[p.id]).filter((v) => v != null).sort((a, b) => b - a);
  const total = sum(kb);
  const share = (n) => `${Math.round(100 * n / total)}%`;
  const mid = kb.length >> 1;
  const median = kb.length % 2 ? kb[mid] : Math.round((kb[mid - 1] + kb[mid]) / 2);
  const depKb = sum(formulae.filter((p) => !p.requested).map((p) => sizes[p.id] || 0));
  const licenses = groupsBy(formulae.filter((p) => p.license), (p) => p.license);
  const thirdParty = (S.snap?.model?.taps || []).filter((tp) => !tp.official && tp.installed.length);
  const fromThirdParty = thirdParty.flatMap((tp) => tp.installed).map((id) => S.pkgs.get(id));
  const letters = groupsBy(all, (p) => p.name[0]);
  const recent = all.filter((p) => p.installed_time && Date.now() / 1000 - p.installed_time < 30 * 86400);
  const counted = (key, ps) => [key, String(ps.length), ps];
  const rows = [
    ["deps_per_formula", mine.length ? t("num.on_average", { n: (sum(mine.map((p) => reach.get(p.id).all.size)) / mine.length).toFixed(1) }) : "–"],
    ["direct_links", String(sum(all.map((p) => p.deps.length)))],
    ["median_size", total ? fmtSize(median) : "…"],
    ["average_size", total ? fmtSize(Math.round(total / kb.length)) : "…"],
    ["top_ten_share", total ? share(sum(kb.slice(0, 10))) : "…"],
    ["dependency_disk", total ? `${fmtSize(depKb)} (${share(depKb)})` : "…"],
    counted("standalone", all.filter((p) => !p.deps.length && !p.required_by.length)),
    counted("keg_only", formulae.filter((p) => p.keg_only)),
    counted("versioned", all.filter((p) => p.name.includes("@"))),
    counted("with_caveats", all.filter((p) => p.caveats)),
    ["licenses", String(licenses.length)],
    counted("no_license", formulae.filter((p) => !p.license)),
    counted("third_party", fromThirdParty),
    counted("recent", recent),
    ["first_letter", letters.length ? `${letters[0][0]} (${letters[0][1].length})` : "–", letters[0]?.[1]],
  ];
  return `<div class="card"><h3>${t("num.title")}</h3><div class="numbers">${rows.map(([key, value, ps]) => `
    <div class="num-row" title="${esc(t(`num.${key}_help`) + (ps?.length ? "\n\n" + nameList(ps) : ""))}"><span class="muted">${t("num." + key)}</span><span class="num-value">${esc(value)}</span></div>`).join("")}</div></div>`;
}

// A label is a literal shown as is, or a function, so it is translated in the language chosen at render time.
const rangeLabel = (l) => (typeof l === "function" ? l() : l);
const SIZE_RANGES = [[() => t("range.under_1mb"), 1024], ["1–10 MB", 10240], ["10–100 MB", 102400], ["100 MB–1 GB", 1048576], [() => t("range.over_1gb"), Infinity]];
const PULLED_RANGES = [[() => t("range.none"), 1], ["1", 2], ["2–5", 6], ["6–10", 11], ["11–20", 21], ["21–50", 51], [() => t("range.over_50"), Infinity]];
const VERSION_STYLES = [[() => t("vstyle.date"), DATE_VERSION], ["0.x", /^0\./], ["x.y.z", /^\d+\.\d+\.\d+/], ["x.y", /^\d+\.\d+(?![.\d])/], [() => t("vstyle.one_number"), /^\d+(?![.\d])/]];
// 2024-01-01 was a Monday.
const weekdayName = (i) => new Intl.DateTimeFormat(LANG, { weekday: "long" }).format(new Date(2024, 0, 1 + i));

// One bar per range, in the order given; a package goes to the first range its value is under.
function ranges(ps, value, limits) {
  const rows = limits.map(([l]) => ({ label: rangeLabel(l), pkgs: [] }));
  for (const p of ps) rows[limits.findIndex(([, under]) => value(p) < under)].pkgs.push(p);
  return rows.map((r) => ({ ...r, value: r.pkgs.length }));
}

// The largest groups, one bar each, and the rest folded into one bar.
function topGroups(groups, n = 8) {
  const rows = groups.slice(0, n).map(([label, pkgs]) => ({ label, value: pkgs.length, pkgs }));
  const rest = groups.slice(n);
  if (rest.length) rows.push({ label: t("unit.others", { n: rest.length }), value: sum(rest.map(([, ps]) => ps.length)), pkgs: rest.flatMap(([, ps]) => ps) });
  return rows;
}

function distributionCharts(all, sizes, reach) {
  const sized = all.filter((p) => sizes[p.id] != null);
  const dated = all.filter((p) => p.installed_time);
  const style = (p) => rangeLabel(VERSION_STYLES.find(([, re]) => re.test(p.version))?.[0] || (() => t("vstyle.other")));
  const weekday = (p) => (new Date(p.installed_time * 1000).getDay() + 6) % 7;
  const pkg = (n) => t("unit.pkg", { n });
  const none = t("range.none");
  return [
    barChart(t("dist.size"), sized.length ? ranges(sized, (p) => sizes[p.id], SIZE_RANGES) : [], pkg, t("ov.computing_sizes"), t("dist.size_help")),
    barChart(t("dist.pulled"), ranges(all, (p) => reach.get(p.id).all.size, PULLED_RANGES), pkg, t("ov.nothing_installed"), t("dist.pulled_help")),
    barChart(t("dist.licenses"), topGroups(groupsBy(all.filter((p) => p.kind === "formula"), (p) => p.license || none)), pkg, t("dist.no_formulae"),
      t("dist.licenses_help")),
    barChart(t("dist.taps"), topGroups(groupsBy(all, (p) => p.tap || none)), pkg, t("ov.nothing_installed"), t("dist.taps_help")),
    barChart(t("dist.versions"), topGroups(groupsBy(all, style)), pkg, t("ov.nothing_installed"), t("dist.versions_help")),
    barChart(t("dist.weekday"), [0, 1, 2, 3, 4, 5, 6].map((i) => { const pkgs = dated.filter((p) => weekday(p) === i); return { label: weekdayName(i), value: pkgs.length, pkgs }; }),
      pkg, t("dist.no_dates"), t("dist.weekday_help")),
  ].join("");
}

function renderOverview() {
  const all = [...S.pkgs.values()];
  const c = counts();
  const sizes = S.snap?.sizes || {};
  const total = Object.values(sizes).reduce((a, b) => a + b, 0);
  const services = all.filter((p) => p.service);
  const running = services.filter((p) => p.service.running).length;
  const apps = appsList();
  const issues = attention();
  const worst = issues[0]?.level;
  const kindSize = (fn) => all.filter(fn).reduce((a, p) => a + (sizes[p.id] || 0), 0);
  const reach = dependencyReach();
  $("#content").innerHTML = `<div class="ov">
    <div class="tiles">
      ${tile(S.pkgs.size, t("tile.packages"), t("tile.packages_sub", { requested: c.requested, deps: c.deps, casks: c.casks }))}
      ${tile(total ? fmtSize(total) : "…", t("tile.disk"), total ? t("tile.disk_sub", { formulae: fmtSize(kindSize((p) => p.kind === "formula")), casks: fmtSize(kindSize((p) => p.kind === "cask")) }) : t("tile.computing"))}
      ${tile(c.outdated, t("tile.outdated"), t("tile.outdated_sub", { n: c.upgradable }), c.outdated ? "var(--accent)" : "")}
      ${tile(issues.length, t("tile.issues"), worst ? t("tile.worst", { level: t("level." + worst) }) : t("tile.all_clear"), worst ? LEVEL[worst][1] : "var(--green)")}
      ${tile(`${running}/${services.length}`, t("tile.services"))}
      ${tile(S.snap?.apps ? apps.length : "…", t("tile.apps"), S.snap?.apps ? t("tile.apps_sub", { n: apps.filter((a) => a.source === "brew").length }) : t("tile.scanning"))}
      ${tile(fmtAgo(S.snap?.last_update), t("tile.last_update"), "", daysSinceUpdate() > STALE_UPDATE_DAYS ? "var(--accent)" : "")}
    </div>
    <div class="card attention"><h3>${t("att.title")}</h3>
      ${issues.length ? issues.map((i) => `
        <div class="att att-${i.level}" data-overview="${esc(i.go)}">
          <span class="att-icon" style="color:${LEVEL[i.level][1]}">${LEVEL[i.level][0]}</span>
          <span class="att-level" style="color:${LEVEL[i.level][1]}">${t("level." + i.level)}</span>
          <span><b>${typeof i.count === "number" ? i.count + " " : ""}${esc(i.title)}</b> <span class="muted">${esc(i.text)}</span></span>
          <span class="att-go muted">${i.go === "update" ? t("att.run_update") : t("att.open")}</span>
        </div>`).join("") : `<p class="ok">✔ ${t("att.none")}</p>`}
    </div>
    ${recordsCard(all, sizes, reach)}
    <div class="cards">
      ${stackChart(t("ov.what_installed"), [
        { label: t("legend.by_you"), value: c.requested, color: SERIES[0], go: "requested" },
        { label: t("ov.dependencies"), value: c.deps, color: SERIES[1], go: "deps" },
        { label: t("ov.casks"), value: c.casks, color: SERIES[2], go: "casks" }])}
      ${S.snap?.apps ? stackChart(t("ov.how_apps"), [
        { label: "brew", value: apps.filter((a) => a.source === "brew").length, color: SERIES[0], go: "apps" },
        { label: "App Store", value: apps.filter((a) => a.source === "appstore").length, color: SERIES[1], go: "apps" },
        { label: t("ov.by_hand"), value: apps.filter((a) => a.source === "manual").length, color: SERIES[2], go: "apps" }]) : ""}
      ${barChart(t("ov.largest"), all.filter((p) => sizes[p.id]).sort((a, b) => sizes[b.id] - sizes[a.id]).slice(0, 10)
        .map((p) => ({ id: p.id, label: p.name, value: sizes[p.id] })), fmtSize, total ? t("ov.no_sizes") : t("ov.computing_sizes"))}
      ${barChart(t("ov.most_depended"), all.filter((p) => p.required_by.length).sort((a, b) => b.required_by.length - a.required_by.length).slice(0, 10)
        .map((p) => ({ id: p.id, label: p.name, value: p.required_by.length })), (n) => t("unit.pkg", { n }), t("ov.no_dependents"))}
      ${installsChart()}
      ${numbersCard(all, sizes, reach)}
    </div>
    <h3 class="ov-section">${t("ov.distributions")}</h3>
    <div class="cards">${distributionCharts(all, sizes, reach)}</div></div>`;
}

const LIST_GROUPS = ["tap", "brew", "cask"];

function renderExport() {
  const items = S.list.items;
  const deps = items.filter((i) => i.type === "brew" && [...S.pkgs.values()].some((p) => p.full_name === i.name && p.kind === "formula" && !p.requested));
  $("#content").innerHTML = `    ${items.length ? "" : `<p class="muted">${t("export.empty")}</p>`}
    <div class="cards">${LIST_GROUPS.map((type) => {
      const group = items.filter((i) => i.type === type);
      return group.length ? `<div class="card"><h3>${t("export.group." + type, { n: group.length })}</h3>${group.map((i) =>
        `<div class="listitem"><span class="mono">${esc(i.name)}</span> <button class="small" data-unlist="${esc(i.type)}:${esc(i.name)}">${t("btn.remove")}</button></div>`).join("")}</div>` : "";
    }).join("")}</div>
    ${deps.length ? `<div class="info">${t("export.deps", { names: deps.map((i) => `<span class="mono">${esc(i.name)}</span>`).join(", ") })}</div>` : ""}
    ${items.length ? `<h3>Brewfile</h3><pre id="brewfile">${esc(S.list.brewfile)}</pre>
      <div class="btnrow" style="margin-top:8px"><button class="accent" id="dl-brewfile">${t("export.download")}</button><button id="copy-brewfile">${t("btn.copy")}</button></div>
      <h3>${t("export.new_mac")}</h3>
      <ol>
        <li>${t("export.step1")} <pre>/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"</pre></li>
        <li>${t("export.step2")} <pre>brew bundle --file=Brewfile</pre></li>
      </ol>
      <p class="muted">${t("export.trusted")}</p>` : ""}`;
}

async function downloadBrewfile() {
  const r = await fetch("/api/brewfile", { headers: { "X-Brewdash-Token": TOKEN } });
  if (!r.ok) return alertBox(`HTTP ${r.status}`);
  const app = window.webkit?.messageHandlers?.brewdash;
  if (app) return app.postMessage({ save: "Brewfile", text: await r.text() });
  // As text/plain, Chrome saves it as Brewfile.txt, and brew bundle --file=Brewfile then finds nothing.
  const url = URL.createObjectURL(new Blob([await r.text()], { type: "application/octet-stream" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: "Brewfile" });
  a.click();
  URL.revokeObjectURL(url);
}

async function renderHistory() {
  try { S.history = await api("/api/history"); } catch (e) { S.history = []; }
  if (S.tab !== "history") return;
  $("#content").innerHTML = `<table><thead><tr><th>${t("history.when")}</th><th>${t("history.action")}</th><th>${t("detail.status")}</th><th>${t("history.commands")}</th><th>${t("history.snapshot")}</th></tr></thead><tbody>
    ${S.history.map((h) => `<tr><td class="mono">${new Date(h.created * 1000).toLocaleString(LANG)}</td><td>${esc(tm(h.title_msg, h.title))}</td>
      <td class="st st-${esc(h.status)}">${esc(jobStatus(h.status))}${h.exit_code ? ` (${h.exit_code})` : ""}</td>
      <td class="mono">${h.commands.map(esc).join("<br>")}</td><td class="mono muted">${esc(h.snapshot || "")}</td></tr>`).join("")}
    </tbody></table>${S.history.length ? "" : `<p class="muted">${t("history.empty")}</p>`}`;
}

function renderInstall() {
  const s = S.search;
  const pv = s.preview;
  const box = $("#search-q");
  const draft = box ? box.value : s.q;
  const focus = box && document.activeElement === box ? [box.selectionStart, box.selectionEnd] : null;
  $("#content").innerHTML = `
    <div class="toolbar"><input type="search" id="search-q" placeholder="${t("install.placeholder")}" value="${esc(draft)}" style="width:420px">
      <button id="search-go">${t("install.search")}</button>${s.busy ? `<span class="muted">${t("install.searching")}</span>` : ""}</div>
    <div style="display:flex;gap:16px;align-items:start">
      <div style="flex:1">${s.results == null ? `<p class="muted">${t("install.help")}</p>` :
        s.results.length ? `<table><thead><tr><th>${t("col.name")}</th><th>${t("install.kind")}</th><th></th></tr></thead><tbody>${s.results.map((r) =>
          `<tr class="row" data-preview="${esc(r.kind)}:${esc(r.name)}"><td class="name">${esc(r.name)}</td><td>${esc(r.kind)}</td><td>${r.installed ? `<span class="badge b-service">${t("install.installed")}</span>` : ""}</td></tr>`).join("")}</tbody></table>` :
        `<p class="muted">${t("install.no_results")}</p>`}</div>
      <div style="width:460px">${pv ? (pv.loading ? `<div class="card loading">${t("install.loading", { name: esc(pv.name) })}</div>` : pv.error ? `<div class="card warn">${esc(pv.error)}</div>` : `
        <div class="card"><h3 class="mono">${esc(pv.name)} <span class="muted">${esc(pv.version)}</span> <span class="badge b-cask">${esc(pv.kind)}</span></h3>
          <p>${esc(pv.desc || "")}</p>${pv.homepage ? `<p>${link(pv.homepage)}</p>` : ""}
          ${pv.kind === "formula" ? `<p>${pv.new_deps.length ? t("install.new_deps", { n: pv.new_deps.length, v: `<b>${pv.new_deps.length}</b>` }) : t("install.no_new_deps")}</p>
            <div class="chips">${pv.new_deps.map((d) => `<span class="chip">${esc(d)}</span>`).join("")}</div>` : ""}
          ${pv.caveats ? `<h4 class="muted">${t("detail.caveats")}</h4><pre>${esc(pv.caveats)}</pre>` : ""}
          <div style="margin-top:12px">${S.pkgs.has(pv.id) ? `<span class="muted">${t("install.already")}</span>` : `<button class="accent" data-install="${esc(pv.kind)}:${esc(pv.name)}">${t("install.install", { name: esc(pv.name) })}</button>`}</div></div>`) : ""}</div>
    </div>`;
  if (focus) { const b = $("#search-q"); b.focus(); b.setSelectionRange(...focus); }
}

async function doSearch() {
  const q = $("#search-q").value.trim();
  if (!q) return;
  S.search = { q, results: null, preview: null, busy: true };
  renderInstall();
  try { S.search.results = await api("/api/search?q=" + encodeURIComponent(q)); }
  catch (e) { S.search.results = []; S.search.preview = { error: e.message }; }
  S.search.busy = false;
  renderInstall();
}

async function doPreview(kind, name) {
  S.search.preview = { loading: true, name };
  renderInstall();
  try { S.search.preview = await api(`/api/preview?kind=${kind}&name=${encodeURIComponent(name)}`); }
  catch (e) { S.search.preview = { error: e.message }; }
  renderInstall();
}

// ---------- actions ----------

function closeModal() { $("#modal").classList.add("hidden"); $("#modal-card").innerHTML = ""; }

async function confirmAction(req, { impact = false, breaks = [] } = {}) {
  let plan, imp = null;
  try {
    plan = await api("/api/plan", req);
    if (impact) imp = await api("/api/impact", { targets: req.targets });
  } catch (e) { alertBox(e.message); return; }
  const names = (req.targets || []).map((id) => S.pkgs.get(id)?.name || id);
  const blocked = req.action === "uninstall" && imp?.blockers.length > 0;
  $("#modal-card").innerHTML = `
    <h2>${esc(tm(plan.title_msg, plan.title))}</h2>
    ${names.length > 1 ? `<div class="chips">${names.map((n) => `<span class="chip">${esc(n)}</span>`).join("")}</div>` : ""}
    <p class="muted">${t("modal.will_run")}</p><pre>${plan.commands.map(esc).join("\n")}</pre>
    ${blocked ? `<div class="warn">${t("modal.blocked", { n: imp.blockers.length })}<div class="chips" style="margin-top:6px">${imp.blockers.map(chip).join("")}</div>
      <div class="btnrow" style="margin-top:8px"><button class="small" id="add-blockers">${t("modal.uninstall_too")}</button><button class="small danger" id="force-uninstall">${t("modal.force")}</button></div></div>` : ""}
    ${breaks.length ? `<div class="warn"><b>${t("modal.breaks")}</b><div class="chips" style="margin-top:6px">${breaks.map(chip).join("")}</div></div>` : ""}
    ${imp?.new_orphans.length ? `<div class="info">${t("modal.new_orphans", { n: imp.new_orphans.length })}<div class="chips" style="margin-top:6px">${imp.new_orphans.map(chip).join("")}</div></div>` : ""}
    ${plan.snapshot ? `<div class="info">${t("modal.snapshot")}</div>` : ""}
    <div class="modal-actions"><button id="m-cancel">${t("btn.cancel")}</button>${blocked ? "" : `<button id="m-ok" class="${["uninstall", "autoremove", "trash", "untap"].includes(req.action) ? "danger" : "accent"}">${t("btn.run")}</button>`}</div>`;
  $("#modal").classList.remove("hidden");
  $("#m-cancel").onclick = closeModal;
  const ok = $("#m-ok");
  if (ok) ok.onclick = async () => {
    closeModal();
    try {
      const job = await api("/api/jobs", req);
      S.openJobs.add(job.id);
      $("#jobs").classList.remove("collapsed");
      if (req.targets?.length > 1) S.selected.clear();
      req.taps?.forEach((n) => S.selected.delete("t:" + n));
      loadJobs(); renderBulk();
    } catch (e) { alertBox(e.message); }
  };
  const add = $("#add-blockers");
  if (add) add.onclick = () => confirmAction({ ...req, targets: [...new Set([...req.targets, ...imp.blockers])] }, { impact: true });
  const force = $("#force-uninstall");
  if (force) force.onclick = () => confirmAction({ ...req, ignore_dependencies: true }, { breaks: imp.blockers });
}

function alertBox(msg) {
  $("#modal-card").innerHTML = `<h2>${t("modal.error")}</h2><div class="warn">${esc(msg)}</div><div class="modal-actions"><button id="m-cancel">${t("btn.close")}</button></div>`;
  $("#modal").classList.remove("hidden");
  $("#m-cancel").onclick = closeModal;
}

function act(action, targets) {
  return confirmAction({ action, targets }, { impact: action === "uninstall" });
}

// Each bulk action, the selected ids it applies to, and why it is off when it applies to none.
function tapOf(id) {
  return id.startsWith("t:") ? (S.snap?.model?.taps || []).find((tp) => tp.name === id.slice(2)) : null;
}

function bulkActions(ids) {
  const pk = ids.filter((id) => S.pkgs.has(id)).map((id) => S.pkgs.get(id));
  const items = ids.map(itemFor).filter(Boolean);
  const fresh = items.filter((i) => !inList(i));
  const none = pk.length ? null : t("bulk.why.none");
  return [
    ["upgrade", t("btn.upgrade"), "accent", pk.filter(upgradable).map((p) => p.id), none || t("bulk.why.upgrade")],
    ["pin", t("btn.pin"), "", pk.filter((p) => !p.pinned).map((p) => p.id), none || t("bulk.why.pin")],
    ["unpin", t("btn.unpin"), "", pk.filter((p) => p.pinned).map((p) => p.id), none || t("bulk.why.unpin")],
    ["uninstall", t("btn.uninstall"), "danger", pk.map((p) => p.id), none],
    ["untap", t("btn.untap"), "danger", ids.filter((id) => !tapOf(id)?.installed.length && tapOf(id)), t("bulk.why.untap")],
    ["list", t("bulk.add_to_list"), "", ids.filter((id) => itemFor(id) && fresh.some((i) => i.type === itemFor(id).type && i.name === itemFor(id).name)),
      items.length ? t("bulk.why.listed") : t("bulk.why.unlistable")],
  ];
}

function renderBulk() {
  const el = $("#bulk");
  const ids = [...S.selected];
  el.classList.toggle("hidden", !ids.length);
  if (!ids.length) return;
  el.innerHTML = `<b>${t("bulk.selected", { n: ids.length })}</b>
    ${bulkActions(ids).map(([k, label, cls, applies, why]) => `<button class="${cls}" data-bulk="${k}" ${applies.length ? "" : `disabled title="${esc(why)}"`}>${label} (${applies.length})</button>`).join("")}
    <button data-bulk="clear">${t("bulk.clear")}</button>
    <span class="muted hint">${t("bulk.hint")}</span>`;
}

// ---------- jobs ----------

const JOB_STATUSES = ["queued", "running", "succeeded", "failed"];
const jobStatus = (s) => JOB_STATUSES.includes(s) ? t("job.status." + s) : s;

function renderJobs() {
  const active = S.jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  $("#jobs-title").innerHTML = `${t("jobs.title")} ${active ? `<span class="st st-running">${t("jobs.active", { n: active })}</span>` : `<span class="muted">${S.jobs.length}</span>`}`;
  const body = $("#jobs-body");
  const scroll = {};
  body.querySelectorAll("pre[data-log]").forEach((pre) => { scroll[pre.dataset.log] = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4; });
  body.innerHTML = [...S.jobs].reverse().map((j) => `
    <div class="job"><div class="job-head" data-job="${j.id}"><span class="st st-${j.status}">${esc(jobStatus(j.status))}</span><b>${esc(tm(j.title_msg, j.title))}</b>
      <span class="muted">${new Date(j.created * 1000).toLocaleTimeString(LANG)}</span>${j.snapshot ? `<span class="muted mono">${t("jobs.snapshot", { path: esc(j.snapshot) })}</span>` : ""}</div>
      ${j.status === "failed" ? `<div class="terminal-hint">${j.needs_terminal ? t("jobs.needs_sudo") : t("jobs.failed")}
        <pre style="margin-top:6px">${j.commands.map(esc).join("\n")}</pre><button class="small" style="margin-top:6px" data-copy="${j.id}">${t("jobs.copy")}</button></div>` : ""}
      ${S.openJobs.has(j.id) ? `<pre data-log="${j.id}">${esc(j.log.join("\n")) || t("jobs.waiting")}</pre>` : ""}</div>`).join("") || `<p class="muted">${t("jobs.empty")}</p>`;
  body.querySelectorAll("pre[data-log]").forEach((pre) => { if (scroll[pre.dataset.log] !== false) pre.scrollTop = pre.scrollHeight; });
}

// ---------- render root ----------

function renderIntro() {
  if (S.tab === "overview") { $("#intro").innerHTML = hero(); return; }
  $("#intro").innerHTML = `<div class="intro"><h2>${tabLabel(S.tab)}</h2><p>${t(`intro.${S.tab}.what`)}</p><p><b>${t("intro.what_to_do")}</b> ${t(`intro.${S.tab}.todo`)}</p></div>`;
}

function hero() {
  return `<div class="hero"><div>
      <h1>brew<span>dash</span></h1>
      <p class="lead">${t("hero.lead")}</p>
      <ul>
        <li>${t("hero.understand")}</li>
        <li>${t("hero.clean")}</li>
        <li>${t("hero.safe", { link: `<a href="https://github.com/lemiorhan/secure-it" target="_blank" rel="noopener">secure-it</a>` })}</li>
        <li>${t("hero.move")}</li>
      </ul></div>
    <div class="hero-side"><h4>${t("hero.how")}</h4>
      <p>${t("hero.how_text")}</p>
      <p>${t("hero.start")}</p></div></div>`;
}

function renderView() {
  renderIntro();
  if (S.tab !== "settings" && !S.snap?.model) { $("#content").innerHTML = `<p class="loading">${S.snap?.error ? t("view.failed") : t("view.reading")}</p>`; return; }
  if (PKG_TABS[S.tab]) renderPackages();
  else ({ overview: renderOverview, services: renderServices, taps: renderTaps, health: renderHealth, history: renderHistory, install: renderInstall,
    apps: renderApps, shell: renderShell, export: renderExport, security: renderSecurity, settings: renderSettings })[S.tab]();
}

function renderAll() {
  renderHeader(); renderTabs(); renderView(); renderDetail(); renderBulk();
}

// ---------- events ----------

// The click handler below names its target `t`, which hides t().
const copiedLabel = () => t("btn.copied");

document.addEventListener("click", (e) => {
  const t = e.target;
  const d = (k) => t.closest(`[data-${k}]`)?.dataset[k.replace(/-(\w)/g, (_, c) => c.toUpperCase())];
  if (t.closest("#modal") && t.id === "modal") return closeModal();
  if (d("tab")) { S.tab = d("tab"); $("#view").scrollTop = 0; renderTabs(); renderView(); renderDetail(); return; }
  if (t.matches("[data-sel]")) { t.checked ? S.selected.add(t.dataset.sel) : S.selected.delete(t.dataset.sel); renderBulk(); return; }
  if (t.id === "sel-all") { visiblePackages().forEach((p) => t.checked ? S.selected.add(p.id) : S.selected.delete(p.id)); renderPackages(); renderBulk(); return; }
  if (d("flag")) { const k = d("flag"); S.flags.has(k) ? S.flags.delete(k) : S.flags.add(k); renderPackages(); return; }
  if (d("sort")) { const k = d("sort"); S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : 1 }; renderPackages(); return; }
  if (d("overview")) {
    const [tab, flags] = d("overview").split(":");
    if (tab === "update") { confirmAction({ action: "update" }); return; }
    S.tab = tab; S.flags = new Set(flags ? flags.split(",") : []); S.q = ""; $("#view").scrollTop = 0; renderTabs(); renderView(); renderDetail(); return;
  }
  if (d("goto")) { select(d("goto")); return; }
  if (t.closest("[data-close]")) { S.current = null; renderDetail(); renderView(); return; }
  if (d("act")) { act(d("act"), [S.current]); return; }
  if (d("advice")) {
    const [i, k] = d("advice").split(":").map(Number);
    const req = S.pkgs.get(S.current).advice[i].actions[k].req;
    confirmAction(req, { impact: req.action === "uninstall" }); return;
  }
  if (d("svc")) { confirmAction({ action: "service", op: d("svc"), targets: [t.closest("[data-svc]").dataset.id] }); return; }
  if (d("bulk")) {
    const b = d("bulk");
    if (b === "clear") { S.selected.clear(); renderBulk(); renderView(); return; }
    const ids = bulkActions([...S.selected]).find(([k]) => k === b)[3];
    if (b === "list") { loadList({ op: "add", ids }); return; }
    if (b === "untap") { confirmAction({ action: "untap", taps: ids.map((id) => id.slice(2)) }); return; }
    act(b, ids); return;
  }
  if (d("unlist")) { const [type, ...n] = d("unlist").split(":"); loadList({ op: "remove", items: [{ type, name: n.join(":") }] }); return; }
  if (d("adopt")) { confirmAction({ action: "adopt", app: d("adopt") }); return; }
  if (t.matches("[data-channel]")) { loadSettings({ path: "/api/settings", body: { channel: t.dataset.channel } }); return; }
  if (t.matches("[data-auto]")) { loadSettings({ path: "/api/settings", body: { auto: t.checked } }); return; }
  if (d("update")) { loadSettings({ path: "/api/update", body: { op: d("update") } }); return; }
  if (d("trash")) { confirmAction({ action: "trash", app: d("trash") }); return; }
  if (d("uninstall")) { act("uninstall", [d("uninstall")]); return; }
  if (d("findcask")) { S.tab = "install"; renderTabs(); renderView(); $("#search-q").value = d("findcask"); doSearch(); return; }
  if (t.id === "dl-brewfile") { downloadBrewfile(); return; }
  if (t.id === "copy-brewfile") { navigator.clipboard.writeText(S.list.brewfile); t.textContent = copiedLabel(); return; }
  if (d("global")) { confirmAction({ action: d("global") }); return; }
  if (d("preview")) { const [k, ...n] = d("preview").split(":"); doPreview(k, n.join(":")); return; }
  if (d("install")) { const [k, ...n] = d("install").split(":"); confirmAction({ action: "install", kind: k, name: n.join(":") }); return; }
  if (t.id === "search-go") { doSearch(); return; }
  if (d("copy")) { const j = S.jobs.find((x) => x.id === +d("copy")); navigator.clipboard.writeText(j.commands.join(" && ")); t.textContent = copiedLabel(); return; }
  if (t.matches("[data-copytext]")) { navigator.clipboard.writeText(t.dataset.copytext); t.textContent = copiedLabel(); return; }
  if (d("job")) { const id = +d("job"); S.openJobs.has(id) ? S.openJobs.delete(id) : S.openJobs.add(id); loadJobs(); return; }
  if (t.closest("#jobs-head")) { $("#jobs").classList.toggle("collapsed"); return; }
  const row = t.closest("tr.row[data-id]");
  if (row && !t.matches("input")) select(row.dataset.id);
});
document.addEventListener("click", syncUrl);

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "search-q") doSearch();
  if (e.key === "Escape") closeModal();
});
document.addEventListener("change", (e) => {
  if (e.target.id === "depth") { S.depth = +e.target.value; renderGraph(S.pkgs.get(S.current)); }
});

$("#btn-refresh").onclick = () => api("/api/refresh", {}).then(loadState);
$("#btn-update").onclick = () => confirmAction({ action: "update" });
$("#btn-upgrade-all").onclick = () => confirmAction({ action: "upgrade_all" });
$("#btn-autoremove").onclick = () => confirmAction({ action: "autoremove" });
$("#btn-theme").onclick = () => { applyTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]); renderDetail(); };
applyTheme(currentTheme());
$("#lang").innerHTML = Object.entries(LANGS).map(([k, name]) => `<option value="${k}" ${k === LANG ? "selected" : ""}>${name}</option>`).join("");
$("#lang").onchange = (e) => { setLang(e.target.value); applyTheme(currentTheme()); renderAll(); renderJobs(); };
translateStatic();
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => renderDetail());

// ---------- polling ----------

let lastActive = false;
async function tick() {
  await loadJobs();
  const active = S.jobs.some((j) => j.status === "queued" || j.status === "running");
  if (lastActive && !active && S.tab === "history") renderHistory();
  lastActive = active;
  await loadState();
  setTimeout(tick, active ? 800 : 3000);
}
applyUrl();
renderAll();
loadList();
tick();
