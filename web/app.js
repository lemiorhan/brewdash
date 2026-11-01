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

const TABS = [
  ["overview", "Overview"], ["requested", "Installed by you"], ["deps", "Dependencies"], ["casks", "Casks"],
  ["services", "Services"], ["taps", "Taps"], ["health", "Health"], ["security", "Security"],
  ["apps", "Apps"], ["shell", "Shell"], ["history", "History"], ["install", "Install"], ["export", "Export"],
];

// ---------- formatting ----------

const fmtSize = (kb) => kb == null ? "…" : kb > 1048576 ? (kb / 1048576).toFixed(1) + " GB" : kb > 1024 ? (kb / 1024).toFixed(0) + " MB" : kb + " KB";
const fmtDate = (ts) => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "";
const fmtAgo = (ts) => {
  if (!ts) return "never";
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 60 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
};
const sizeOf = (id) => S.snap?.sizes?.[id];

const upgradable = (p) => p.outdated && !p.disabled && !p.pinned;

function badges(p) {
  let out = "";
  if (p.outdated) out += `<span class="badge b-outdated">outdated</span>`;
  if (p.pinned) out += `<span class="badge b-pinned">pinned</span>`;
  if (p.orphan) out += `<span class="badge b-orphan">orphan</span>`;
  if (p.missing_deps.length) out += `<span class="badge b-missing">missing dep</span>`;
  if (p.deprecated) out += `<span class="badge b-deprecated">deprecated</span>`;
  if (p.disabled) out += `<span class="badge b-disabled">disabled</span>`;
  if (p.service) out += `<span class="badge b-service">service</span>`;
  return out;
}

// ---------- data ----------

async function loadState() {
  try {
    const snap = await api("/api/state");
    $("#error").classList.toggle("hidden", !snap.error);
    $("#error").textContent = snap.error ? "brew error: " + snap.error : "";
    if (snap.version === S.version && snap.refreshing === S.snap?.refreshing && snap.security?.seq === S.snap?.security?.seq) return;
    S.snap = snap; S.version = snap.version;
    S.pkgs = new Map((snap.model?.packages || []).map((p) => [p.id, p]));
    for (const id of [...S.selected]) if (!itemFor(id)) S.selected.delete(id);
    if (S.current && !S.pkgs.has(S.current)) S.current = null;
    renderAll();
  } catch (e) {
    $("#error").classList.remove("hidden");
    $("#error").textContent = "Cannot reach brewdash server: " + e.message;
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
  $("#stats").innerHTML = [
    `<span class="stat"><b>${S.pkgs.size}</b> packages</span>`,
    `<span class="stat"><b>${c.outdated}</b> outdated</span>`,
    `<span class="stat"><b>${c.orphans}</b> orphans</span>`,
    c.missing ? `<span class="stat" style="border-color:var(--red)"><b>${c.missing}</b> missing deps</span>` : "",
    `<span class="stat"><b>${total ? fmtSize(total) : "…"}</b> on disk</span>`,
    S.snap?.refreshing ? `<span class="stat">refreshing…</span>` : "",
  ].join("");
  $("#last-update").textContent = "last brew update: " + fmtAgo(S.snap?.last_update);
  $("#btn-upgrade-all").textContent = `Upgrade all (${c.upgradable})`;
  $("#btn-upgrade-all").disabled = !c.upgradable;
  $("#btn-autoremove").textContent = `Autoremove (${c.orphans})`;
  $("#btn-autoremove").disabled = !c.orphans;
}

function renderTabs() {
  const c = counts();
  $("#tabs").innerHTML = TABS.map(([k, label]) =>
    `<button data-tab="${k}" class="${S.tab === k ? "active" : ""}">${label}${c[k] != null ? `<span class="count">${c[k]}</span>` : ""}</button>`
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
  ["name", "Name", (p) => p.name], ["version", "Version", (p) => p.version], ["latest", "Latest", (p) => p.latest],
  ["size", "Size", (p) => sizeOf(p.id) ?? -1, "num"], ["installed", "Installed", (p) => p.installed_time || 0, "num"],
  ["usedby", "Used by", (p) => p.dependents.length, "num"], ["deps", "Deps", (p) => p.deps.length, "num"],
  ["desc", "Description", (p) => p.desc || ""],
];

function visiblePackages() {
  const q = S.q.trim().toLowerCase();
  let list = [...S.pkgs.values()].filter(PKG_TABS[S.tab]);
  for (const [k, fn] of FLAGS) if (S.flags.has(k)) list = list.filter(fn);
  if (q) list = list.filter((p) => p.name.toLowerCase().includes(q) || (p.desc || "").toLowerCase().includes(q) || p.display_name.toLowerCase().includes(q));
  const col = COLS.find((c) => c[0] === S.sort.key);
  list.sort((a, b) => {
    const x = col[2](a), y = col[2](b);
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
      <input type="search" id="q" placeholder="Search name or description" value="${esc(S.q)}">
      ${FLAGS.map(([k]) => `<label><input type="checkbox" data-flag="${k}" ${S.flags.has(k) ? "checked" : ""}> ${k}</label>`).join("")}
      <span class="muted">${list.length} shown</span>
    </div>
    <table>
      <thead><tr><th><input type="checkbox" id="sel-all" ${allSel ? "checked" : ""}></th>
        ${COLS.map(([k, label, , cls]) => `<th data-sort="${k}" class="${cls || ""}">${label}${arrow(k)}</th>`).join("")}</tr></thead>
      <tbody>${list.map((p) => `
        <tr class="row ${S.current === p.id ? "current" : ""}" data-id="${esc(p.id)}">
          <td><input type="checkbox" data-sel="${esc(p.id)}" ${S.selected.has(p.id) ? "checked" : ""}></td>
          <td class="name">${esc(p.name)}${badges(p)}</td>
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
  return `<span class="chip" data-goto="${esc(id)}">${esc(p ? p.name : id)}${p?.kind === "cask" ? " (cask)" : ""}</span>`;
}

function renderAdvice(p) {
  if (!p.advice.length) return "";
  return `<div class="detail-section"><h4>What to do</h4>${p.advice.map((a, i) => `
    <div class="${a.level === "warn" ? "warn" : "info"}"><b>${esc(a.title)}</b><div style="margin-top:4px">${esc(a.text)}</div>
      ${a.alternatives.length ? `<div class="chips" style="margin-top:6px"><span class="muted">Already installed:</span>${a.alternatives.map(chip).join("")}</div>` : ""}
      ${a.actions.length ? `<div class="btnrow" style="margin-top:8px">${a.actions.map((x, k) => `<button class="small" data-advice="${i}:${k}">${esc(x.label)}</button>`).join("")}</div>` : ""}
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
      <div><h2 class="detail-title">${esc(p.name)}</h2><div>${p.kind === "cask" ? `<span class="badge b-cask">cask</span>` : ""}${badges(p)}</div></div>
      <button class="small" data-close>✕</button>
    </div>
    <p>${esc(p.desc || "No description.")}</p>
    ${renderAdvice(p)}
    ${alertsFor("package", p.id).length ? `<div class="detail-section"><h4>secure-it findings</h4>${alertsFor("package", p.id).map(alertCard).join("")}</div>` : ""}
    <div class="kv">
      ${p.kind === "cask" && p.display_name !== p.name ? `<div>App</div><div>${esc(p.display_name)}</div>` : ""}
      <div>Version</div><div class="mono">${esc(p.version)}${p.outdated ? ` → <b style="color:var(--accent)">${esc(p.latest)}</b>` : ""}</div>
      <div>Homepage</div><div>${link(p.homepage)}</div>
      <div>Tap</div><div class="mono">${esc(p.tap || "–")}</div>
      ${p.license ? `<div>License</div><div>${esc(p.license)}</div>` : ""}
      <div>Installed</div><div>${fmtDate(p.installed_time)} ${p.kind === "formula" ? (p.requested ? "(by you)" : "(as dependency)") : ""}</div>
      <div>Size</div><div>${fmtSize(sizeOf(p.id))}</div>
      ${p.keg_only ? `<div>Keg-only</div><div>yes, not linked into the prefix</div>` : ""}
    </div>
    <div class="detail-section btnrow">
      ${upgradable(p) ? `<button class="accent" data-act="upgrade">Upgrade</button>` : ""}
      <button data-act="${p.pinned ? "unpin" : "pin"}">${p.pinned ? "Unpin" : "Pin"}</button>
      <button class="danger" data-act="uninstall">Uninstall</button>
    </div>
    ${svc ? `<div class="detail-section"><h4>Service</h4>
      <div class="kv"><div>Status</div><div>${esc(svc.status)}${svc.pid ? ` (pid ${svc.pid})` : ""}</div>
      ${svc.log_path ? `<div>Log</div><div class="mono">${esc(svc.log_path)}</div>` : ""}
      ${portsOf(p.id).length ? `<div>Listening</div><div>${listening(p.id)}</div>` : ""}</div>
      <div class="btnrow" style="margin-top:6px">${["start", "stop", "restart"].map((op) => `<button class="small" data-svc="${op}" data-id="${esc(p.id)}">${op}</button>`).join("")}</div></div>` : ""}
    <div class="detail-section"><h4>Depends on (${p.deps.length})</h4><div class="chips">${p.deps.map(chip).join("") || `<span class="muted">nothing</span>`}</div></div>
    <div class="detail-section"><h4>Used by (${p.dependents.length})</h4><div class="chips">${p.dependents.map(chip).join("") || `<span class="muted">nothing installed depends on this directly</span>`}</div>
      ${indirect.length ? `<div class="chips" style="margin-top:6px"><span class="muted">Also needed indirectly by:</span>${indirect.map(chip).join("")}</div>` : ""}</div>
    <div class="detail-section"><h4>Graph
      <select id="depth" style="margin-left:8px"><option value="1" ${S.depth === 1 ? "selected" : ""}>1 hop</option><option value="2" ${S.depth === 2 ? "selected" : ""}>2 hops</option><option value="3" ${S.depth === 3 ? "selected" : ""}>3 hops</option></select></h4>
      <div id="graph"></div>
      <div class="legend"><span><i class="dot" style="background:var(--accent)"></i>selected</span><span><i class="dot" style="background:var(--blue)"></i>installed by you</span><span><i class="dot" style="background:var(--graph-dep)"></i>dependency</span><span><i class="dot" style="background:var(--purple)"></i>cask</span><span>arrow = depends on</span></div>
    </div>
    ${p.caveats ? `<div class="detail-section"><h4>Caveats</h4><pre>${esc(p.caveats)}</pre></div>` : ""}`;
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
}

// ---------- other tabs ----------

function listening(id) {
  return portsOf(id).map((e) => `<span class="port mono ${e.exposed ? "exposed" : ""}" title="${e.exposed ? "reachable from other machines on the network" : "this Mac only"}">${esc(e.proto)} ${esc(e.address)}:${esc(e.port)}</span>`).join(" ");
}

function renderServices() {
  const list = [...S.pkgs.values()].filter((p) => p.service);
  const ports = S.snap?.ports;
  $("#content").innerHTML = `${ports?.error ? `<div class="warn">${esc(ports.error)}</div>` : ""}
    <table><thead><tr><th>Service</th><th>Status</th><th>PID</th><th>User</th><th>Listening</th><th>Plist</th><th></th></tr></thead><tbody>
    ${list.map((p) => `<tr><td class="name"><span class="chip" data-goto="${esc(p.id)}">${esc(p.name)}</span></td>
      <td class="st ${p.service.running ? "st-succeeded" : "st-queued"}">${esc(p.service.status)}</td>
      <td class="mono">${esc(p.service.pid || "")}</td><td>${esc(p.service.user || "")}</td>
      <td>${!ports ? `<span class="muted">…</span>` : listening(p.id) || `<span class="muted">${p.service.pid ? "none" : ""}</span>`}</td>
      <td class="mono muted">${esc(p.service.file || "")}</td>
      <td class="btnrow">${["start", "stop", "restart"].map((op) => `<button class="small" data-svc="${op}" data-id="${esc(p.id)}">${op}</button>`).join("")}</td></tr>`).join("")}
  </tbody></table>${list.length ? "" : `<p class="muted">No installed formula defines a service.</p>`}`;
}

function renderTaps() {
  const taps = S.snap?.model?.taps || [];
  $("#content").innerHTML = `<table><thead><tr><th></th><th>Tap</th><th>Remote</th><th class="num">Installed from it</th><th class="num">Formulae</th><th class="num">Casks</th><th>Last commit</th></tr></thead><tbody>
    ${taps.map((t) => `<tr><td><input type="checkbox" data-sel="t:${esc(t.name)}" ${S.selected.has("t:" + t.name) ? "checked" : ""}></td><td class="name">${esc(t.name)}${t.official ? `<span class="badge b-service">official</span>` : ""}${t.private ? `<span class="badge b-pinned">private</span>` : ""}${alertsFor("tap", t.name).length ? `<span class="badge b-missing" data-tab="security" title="secure-it flags this tap">secure-it</span>` : ""}</td>
      <td class="mono muted">${esc(t.remote || "")}</td><td class="num">${t.installed.length || `<span class="muted">0 · unused?</span>`}</td>
      <td class="num">${t.formula_count}</td><td class="num">${t.cask_count}</td><td class="muted">${esc(t.last_commit || "")}</td></tr>
      ${t.installed.length && !t.official ? `<tr><td></td><td></td><td colspan="5"><div class="chips">${t.installed.map(chip).join("")}</div></td></tr>` : ""}`).join("")}
  </tbody></table>`;
}

function renderHealth() {
  const h = S.snap?.health;
  const all = [...S.pkgs.values()];
  const missing = all.filter((p) => p.missing_deps.length);
  const deprecated = all.filter((p) => p.deprecated || p.disabled);
  const orphans = all.filter((p) => p.orphan);
  $("#content").innerHTML = `<div class="cards">
    <div class="card"><h3>Missing dependencies (${missing.length})</h3>
      ${missing.map((p) => `<div style="margin-bottom:4px">${chip(p.id)} needs <span class="mono" style="color:var(--red)">${esc(p.missing_deps.join(", "))}</span></div>`).join("") || `<span class="muted">None.</span>`}</div>
    <div class="card"><h3>Orphans: autoremove candidates (${orphans.length})</h3>
      <p class="muted">Installed as dependencies; nothing needs them any more.</p>
      <div class="chips">${orphans.map((p) => chip(p.id)).join("") || `<span class="muted">None.</span>`}</div>
      ${orphans.length ? `<div style="margin-top:10px"><button class="danger" data-global="autoremove">Autoremove ${orphans.length}</button></div>` : ""}</div>
    <div class="card"><h3>Deprecated or disabled (${deprecated.length})</h3>
      <div class="chips">${deprecated.map((p) => chip(p.id)).join("") || `<span class="muted">None.</span>`}</div></div>
    <div class="card"><h3>Cleanup</h3>
      ${h ? `<p>${h.cleanup_reclaim ? `<b>${esc(h.cleanup_reclaim)}</b> can be freed.` : "Nothing to clean."}</p>
        ${h.cleanup_preview.length ? `<pre>${esc(h.cleanup_preview.join("\n"))}</pre>` : ""}
        ${h.cleanup_reclaim ? `<div style="margin-top:10px"><button class="accent" data-global="cleanup">Run brew cleanup</button></div>` : ""}` : `<p class="loading">Computing…</p>`}</div>
    <div class="card" style="grid-column:1/-1"><h3>brew doctor ${h ? (h.doctor_ok ? `<span class="st st-succeeded">ok</span>` : `<span class="st st-failed">warnings</span>`) : ""}</h3>
      ${h ? `<pre>${esc(h.doctor || "Your system is ready to brew.")}</pre>` : `<p class="loading">Running brew doctor…</p>`}</div>
  </div>`;
}

const SOURCE = { brew: "brew", appstore: "App Store", manual: "installed by hand" };
const SIGNATURE = {
  apple: ["Apple", "ok"], appstore: ["App Store", "ok"], developer_id: ["Developer ID", "ok"], other: ["other signer", "muted"],
  adhoc: ["ad-hoc, no signer", "bad"], unsigned: ["not signed", "bad"], unknown: ["unknown", "muted"],
};
const weakSignature = (a) => ["adhoc", "unsigned"].includes(a.signature?.kind);

function signature(sig) {
  if (!sig) return `<span class="muted">…</span>`;
  const [label, cls] = SIGNATURE[sig.kind] || SIGNATURE.unknown;
  return `<span class="${cls === "bad" ? "sig-bad" : cls}" title="${esc(sig.error || sig.signer || "")}">${label}</span>${sig.kind === "developer_id" || sig.kind === "other" ? ` <span class="muted">${esc(sig.signer)}</span>` : ""}`;
}

function renderApps() {
  const env = S.snap?.apps;
  if (!env) { $("#content").innerHTML = `<p class="loading">Scanning /Applications…</p>`; return; }
  const apps = appsList();
  const adoptable = apps.filter((a) => a.source === "manual" && a.cask);
  const row = (a) => {
    const sel = a.source === "brew" ? a.brew_id : a.cask ? a.id : null;
    const newer = a.cask_version && a.version && !a.cask_version.startsWith(a.version) ? ` <span class="muted">(cask has ${esc(a.cask_version.split(",")[0])})</span>` : "";
    const action = a.source === "brew" ? `<span class="chip" data-goto="${esc(a.brew_id)}">${esc(S.pkgs.get(a.brew_id)?.name || a.brew_id)}</span> <span class="muted">upgrade and uninstall in Casks</span>`
      : a.source === "appstore" ? `<span class="muted">updates through the App Store</span>`
      : a.cask ? `<button class="small accent" data-adopt="${esc(a.id)}">Bring into brew</button> <span class="mono">${esc(a.cask)}</span>${newer}${a.auto_updates ? ` <span class="muted">updates itself</span>` : ""}`
      : `<button class="small" data-findcask="${esc(a.name)}">Search cask</button>`;
    return `<tr><td>${sel ? `<input type="checkbox" data-sel="${esc(sel)}" ${S.selected.has(sel) ? "checked" : ""}>` : ""}</td>
      <td class="name">${esc(a.name)}</td><td class="mono">${esc(a.version || "")}</td><td>${SOURCE[a.source]}</td><td>${signature(a.signature)}</td><td>${action}</td></tr>`;
  };
  $("#content").innerHTML = `
    ${env.error ? `<div class="warn">${esc(env.error)}</div>` : ""}
    ${env.names_known ? "" : `<div class="info">Homebrew's cask list is not cached yet, so apps are not matched to casks. Run brew update, then Refresh.</div>`}
    <p class="muted">${adoptable.length} app(s) installed by hand have a matching brew cask. <b>Bring into brew</b> runs <span class="mono">brew install --cask --adopt</span>: brew takes over the existing app, and from then on upgrades and uninstalls it like any cask. A match is shown only when the cask installs exactly that .app.</p>
    <p class="muted">Signature shows who signed each app, as the app itself declares it. brewdash does not verify the signature and does not check notarization; <span class="mono">spctl --assess</span> does both, but takes minutes for a full Applications folder.</p>
    <table><thead><tr><th></th><th>App</th><th>Version</th><th>Installed by</th><th>Signature</th><th>Brew</th></tr></thead>
    <tbody>${apps.map(row).join("")}</tbody></table>`;
}

const ISSUE = { duplicate: "duplicate", missing: "missing", pinned: "pinned version", old_prefix: "old prefix", synced: "synced folder first" };

function renderShell() {
  const sh = S.snap?.shell;
  if (!sh) { $("#content").innerHTML = `<p class="loading">Reading shell setup…</p>`; return; }
  const install = (alt) => alt ? `<button class="small" data-install="${esc(alt.kind)}:${esc(alt.name)}">Install ${esc(alt.name)} with brew</button>` : "";
  $("#content").innerHTML = `
    ${sh.error ? `<div class="warn">${esc(sh.error)}</div>` : ""}    <h3>PATH</h3>
    <table><thead><tr><th>File</th><th>Entry</th><th>Status</th><th>What to do</th></tr></thead><tbody>
    ${sh.path.map((e) => `<tr><td class="mono">${esc(e.file)}:${e.line}</td><td class="mono">${esc(e.raw)}</td>
      <td>${e.issue ? `<span class="badge b-missing">${ISSUE[e.issue]}</span>` : e.exists === null ? `<span class="muted">not checked</span>` : `<span class="ok">ok</span>`}</td>
      <td>${esc(e.suggestion || "")} ${e.alternative ? `<div style="margin-top:4px"><span class="muted">brew has this tool:</span> ${install(e.alternative)}</div>` : ""}</td></tr>`).join("")}
    </tbody></table>
    <h3>Tools started by the shell</h3>
    <table><thead><tr><th>Tool</th><th>File</th><th>From brew</th><th></th></tr></thead><tbody>
    ${sh.tools.map((t) => `<tr><td class="name">${esc(t.name)}</td><td class="mono">${esc(t.file)}:${t.line}</td>
      <td>${t.via_brew ? `<span class="ok">yes</span>` : "no"}</td>
      <td>${t.alternative ? `${install({ kind: "formula", name: t.alternative })} <span class="muted">then point ${esc(t.file)}:${t.line} at brew's copy</span>` : ""}</td></tr>`).join("")}
    </tbody></table>`;
}

// ---------- secure-it ----------

const SEVERITY_LEVEL = { critical: "critical", high: "critical", medium: "warning", low: "info" };

function alertLink(l) {
  if (!l) return "";
  if (l.type === "package") return `<div class="chips" style="margin-top:6px"><span class="muted">Package:</span>${chip(l.id)}</div>`;
  if (l.type === "tap") return `<div style="margin-top:6px"><button class="small" data-tab="taps">Open ${esc(l.name)} in Taps</button></div>`;
  const label = TABS.find(([k]) => k === l.tab)?.[1] || l.tab;
  return `<div style="margin-top:6px"><button class="small" data-tab="${esc(l.tab)}">Open ${esc(label)}</button></div>`;
}

// Commands are shown to copy, never run: brewdash does not run what a file in the user's home tells it to.
function alertCard(a) {
  const accept = a.fingerprint ? `sudo secure-it accept ${a.fingerprint.slice(0, 12)} --note "why this is fine"` : "";
  return `<div class="alert sev-${esc(a.severity)}">
    <div><span class="sev">${esc(a.severity)}</span> <b>${esc(a.title)}</b> <span class="muted mono">${esc(a.check_id)}</span></div>
    ${a.detail ? `<div class="muted" style="margin-top:4px">${esc(a.detail)}</div>` : ""}
    ${a.remediation ? `<div style="margin-top:6px">${esc(a.remediation)}</div>` : ""}
    ${a.fix_command ? `<div class="cmd"><span class="muted">Fix:</span><pre>${esc(a.fix_command)}</pre><button class="small" data-copytext="${esc(a.fix_command)}">Copy</button></div>` : ""}
    ${accept ? `<div class="cmd"><span class="muted">If it is expected:</span><pre>${esc(accept)}</pre><button class="small" data-copytext="${esc(accept)}">Copy</button></div>` : ""}
    ${alertLink(a.link)}</div>`;
}

function renderSecurity() {
  const sec = S.snap?.security;
  if (!sec?.found) {
    $("#content").innerHTML = `<div class="card"><h3>secure-it is not set up</h3>
      <p><a href="https://github.com/lemiorhan/secure-it" target="_blank" rel="noopener">secure-it</a> is a separate, root-run scanner for macOS security settings, persistence, listening ports and installed packages. After each scan it writes <span class="mono">${esc(sec?.path || "~/secure-it/report.json")}</span>, and brewdash shows those findings here, next to the packages they concern.</p>
      <pre>git clone https://github.com/lemiorhan/secure-it.git\ncd secure-it && sudo ./install.sh\nsudo secure-it scan</pre></div>`;
    return;
  }
  const list = alerts();
  const by = (s) => list.filter((a) => a.severity === s).length;
  $("#content").innerHTML = `
    ${sec.error ? `<div class="warn">brewdash could not use ${esc(sec.path)}: ${esc(sec.error)}</div>` : ""}
    <p class="muted">From <span class="mono">${esc(sec.path)}</span>, scan of ${esc(sec.generated_at || "unknown time")}. Only findings you have not accepted are listed.
      ${["critical", "high", "medium", "low"].map((s) => by(s) ? `<span class="sev-count sev-${s}">${by(s)} ${s}</span>` : "").join(" ")}</p>
    ${list.length ? list.map(alertCard).join("") : sec.error ? "" : `<p class="ok">✔ secure-it reports nothing open.</p>`}`;
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
  const addPerTab = (level, flags, title, text) => {
    for (const [tab, label] of TABS.filter(([t]) => PKG_TABS[t])) {
      const hits = all.filter((p) => PKG_TABS[tab](p) && flags.every((f) => FLAGS.find(([k]) => k === f)[1](p)));
      add(level, hits.length, `${title} in ${label}`, text(hits.map((p) => p.name).join(", ")), `${tab}:${flags.join(",")}`);
    }
  };
  const missing = all.filter((p) => p.missing_deps.length);
  add("critical", missing.length, "missing dependencies", `${missing.map((p) => p.name).join(", ")} cannot run until the missing packages are installed.`, "health");
  const dead = all.filter((p) => p.deprecated || p.disabled);
  add("critical", dead.length, "deprecated or disabled", `${dead.map((p) => p.name).join(", ")}: Homebrew stopped or will stop maintaining these. Each detail panel says what to do.`, "health");
  if (h) add("warning", !h.doctor_ok, "brew doctor has warnings", "Read them in Health.", "health");
  addPerTab("warning", ["upgradable"], "can be upgraded", (names) => `${names}.`);
  addPerTab("warning", ["outdated", "pinned"], "outdated but pinned", (names) => `${names}: brew upgrade skips these until they are unpinned.`);
  add("warning", daysSinceUpdate() > STALE_UPDATE_DAYS, "brew update is stale", `Last run ${fmtAgo(S.snap?.last_update)}; the outdated list is only as fresh as that.`, "update");
  const pathIssues = (S.snap?.shell?.path || []).filter((e) => e.issue);
  const synced = pathIssues.filter((e) => e.issue === "synced");
  add("warning", synced.length, "synced folders ahead in PATH", `${synced.map((e) => e.raw).join(", ")}: a file that arrives by sync runs in place of the system command of the same name.`, "shell");
  add("warning", pathIssues.length - synced.length, "PATH problems", "Duplicate, missing or outdated entries in your shell startup files.", "shell");
  for (const level of ["critical", "warning", "info"]) {
    const hits = alerts().filter((a) => SEVERITY_LEVEL[a.severity] === level);
    add(level, hits.length, "secure-it findings", hits.slice(0, 3).map((a) => a.title).join("; ") + (hits.length > 3 ? "; …" : ""), "security");
  }
  if (S.snap?.security?.error) add("warning", true, "secure-it report unreadable", S.snap.security.error, "security");
  const exposed = all.filter((p) => portsOf(p.id).some((e) => e.exposed));
  add("warning", exposed.length, "services reachable from the network", `${exposed.map((p) => p.name).join(", ")}: listening on all interfaces, not only this Mac.`, "services");
  const weak = appsList().filter(weakSignature);
  add("warning", weak.length, "apps without a real signature", `${weak.map((a) => a.name).join(", ")}: not signed, or signed ad-hoc with no identity behind it.`, "apps");
  const orphans = all.filter((p) => p.orphan);
  add("info", orphans.length, "orphans", "Installed as dependencies; nothing needs them any more. Autoremove takes them out.", "health");
  add("info", !!h?.cleanup_reclaim, "cleanup", `${h?.cleanup_reclaim} can be freed with brew cleanup.`, "health");
  const adoptable = appsList().filter((a) => a.source === "manual" && a.cask);
  add("info", adoptable.length, "apps brew could manage", `${adoptable.map((a) => a.name).join(", ")}: installed by hand, and a cask installs exactly that app.`, "apps");
  const unusedTaps = (S.snap?.model?.taps || []).filter((t) => !t.official && !t.installed.length);
  add("info", unusedTaps.length, "unused taps", `${unusedTaps.map((t) => t.name).join(", ")}: nothing installed comes from them.`, "taps");
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
  if (!total) return `<div class="card"><h3>${title}</h3><p class="muted">Nothing yet.</p></div>`;
  const shown = parts.filter((p) => p.value);
  return `<div class="card"><h3>${title}</h3>
    <div class="stack">${shown.map((p) => `<div class="seg" style="flex:${p.value};background:${p.color}" title="${esc(p.label)}: ${p.value} (${Math.round(100 * p.value / total)}%)"${p.go ? ` data-overview="${p.go}"` : ""}></div>`).join("")}</div>
    <div class="legend">${shown.map((p) => `<span><span class="dot" style="background:${p.color}"></span>${p.value} ${esc(p.label)}</span>`).join("")}</div></div>`;
}

// Top-N horizontal bars for one measure; each bar opens its package.
function barChart(title, rows, fmt, empty) {
  const max = Math.max(...rows.map((r) => r.value), 0);
  return `<div class="card"><h3>${title}</h3>${rows.length && max ? `<div class="bars">${rows.map((r) => `
    <div class="bar-row" data-goto="${esc(r.id)}" title="${esc(r.label)}: ${esc(fmt(r.value))}">
      <span class="bar-label mono">${esc(r.label)}</span>
      <span class="bar-track"><span class="bar" style="width:${(100 * r.value / max).toFixed(1)}%"></span></span>
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
  return `<div class="card"><h3>Installed per month, last ${MONTHS_SHOWN} months</h3>
    ${max ? `<div class="months">${months.map((m) => {
      const n = byMonth[m].length;
      const names = byMonth[m].slice(0, 12).join(", ") + (n > 12 ? ", …" : "");
      return `<div class="month" title="${m}: ${n}${n ? " (" + esc(names) + ")" : ""}"><span class="month-bar" style="height:${(100 * n / max).toFixed(1)}%"></span></div>`;
    }).join("")}</div>
    <div class="months-axis muted"><span>${months[0]}</span><span>${months[months.length - 1]}</span></div>` : `<p class="muted">Nothing installed in this period.</p>`}
    ${earlier ? `<p class="muted">${earlier} package(s) were installed before ${months[0]}.</p>` : ""}</div>`;
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
  $("#content").innerHTML = `<div class="ov">
    <div class="tiles">
      ${tile(S.pkgs.size, "packages", `${c.requested} by you · ${c.deps} dependencies · ${c.casks} casks`)}
      ${tile(total ? fmtSize(total) : "…", "on disk", total ? `formulae ${fmtSize(kindSize((p) => p.kind === "formula"))} · casks ${fmtSize(kindSize((p) => p.kind === "cask"))}` : "computing")}
      ${tile(c.outdated, "outdated", `${c.upgradable} can be upgraded now`, c.outdated ? "var(--accent)" : "")}
      ${tile(issues.length, "things to look at", worst ? `worst: ${worst}` : "all clear", worst ? LEVEL[worst][1] : "var(--green)")}
      ${tile(`${running}/${services.length}`, "services running")}
      ${tile(S.snap?.apps ? apps.length : "…", "apps", S.snap?.apps ? `${apps.filter((a) => a.source === "brew").length} managed by brew` : "scanning")}
      ${tile(fmtAgo(S.snap?.last_update), "last brew update", "", daysSinceUpdate() > STALE_UPDATE_DAYS ? "var(--accent)" : "")}
    </div>
    <div class="card attention"><h3>Needs attention</h3>
      ${issues.length ? issues.map((i) => `
        <div class="att att-${i.level}" data-overview="${esc(i.go)}">
          <span class="att-icon" style="color:${LEVEL[i.level][1]}">${LEVEL[i.level][0]}</span>
          <span class="att-level" style="color:${LEVEL[i.level][1]}">${i.level}</span>
          <span><b>${typeof i.count === "number" ? i.count + " " : ""}${esc(i.title)}</b> <span class="muted">${esc(i.text)}</span></span>
          <span class="att-go muted">${i.go === "update" ? "run brew update →" : "open →"}</span>
        </div>`).join("") : `<p class="ok">✔ Nothing needs attention.</p>`}
    </div>
    <div class="cards">
      ${stackChart("What is installed", [
        { label: "installed by you", value: c.requested, color: SERIES[0], go: "requested" },
        { label: "dependencies", value: c.deps, color: SERIES[1], go: "deps" },
        { label: "casks", value: c.casks, color: SERIES[2], go: "casks" }])}
      ${S.snap?.apps ? stackChart("How apps got here", [
        { label: "brew", value: apps.filter((a) => a.source === "brew").length, color: SERIES[0], go: "apps" },
        { label: "App Store", value: apps.filter((a) => a.source === "appstore").length, color: SERIES[1], go: "apps" },
        { label: "by hand", value: apps.filter((a) => a.source === "manual").length, color: SERIES[2], go: "apps" }]) : ""}
      ${barChart("Largest on disk", all.filter((p) => sizes[p.id]).sort((a, b) => sizes[b.id] - sizes[a.id]).slice(0, 10)
        .map((p) => ({ id: p.id, label: p.name, value: sizes[p.id] })), fmtSize, total ? "No sizes." : "Computing sizes…")}
      ${barChart("Most depended on", all.filter((p) => p.required_by.length).sort((a, b) => b.required_by.length - a.required_by.length).slice(0, 10)
        .map((p) => ({ id: p.id, label: p.name, value: p.required_by.length })), (n) => `${n} pkg`, "No package depends on another.")}
      ${installsChart()}
    </div></div>`;
}

const LIST_GROUPS = [["tap", "Taps"], ["brew", "Formulae"], ["cask", "Casks and apps"]];

function renderExport() {
  const items = S.list.items;
  const deps = items.filter((i) => i.type === "brew" && [...S.pkgs.values()].some((p) => p.full_name === i.name && p.kind === "formula" && !p.requested));
  $("#content").innerHTML = `    ${items.length ? "" : `<p class="muted">The list is empty.</p>`}
    <div class="cards">${LIST_GROUPS.map(([type, label]) => {
      const group = items.filter((i) => i.type === type);
      return group.length ? `<div class="card"><h3>${label} (${group.length})</h3>${group.map((i) =>
        `<div class="listitem"><span class="mono">${esc(i.name)}</span> <button class="small" data-unlist="${esc(i.type)}:${esc(i.name)}">Remove</button></div>`).join("")}</div>` : "";
    }).join("")}</div>
    ${deps.length ? `<div class="info">${deps.map((i) => `<span class="mono">${esc(i.name)}</span>`).join(", ")}: installed here only as a dependency. brew installs dependencies anyway, so the list does not need them.</div>` : ""}
    ${items.length ? `<h3>Brewfile</h3><pre id="brewfile">${esc(S.list.brewfile)}</pre>
      <div class="btnrow" style="margin-top:8px"><button class="accent" id="dl-brewfile">Download Brewfile</button><button id="copy-brewfile">Copy</button></div>
      <h3>On the new Mac</h3>
      <ol>
        <li>Install Homebrew: <pre>/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"</pre></li>
        <li>Put the Brewfile in a folder and run there: <pre>brew bundle --file=Brewfile</pre></li>
      </ol>
      <p class="muted">Lines with <span class="mono">trusted: true</span> come from third-party taps; that option lets brew load them without asking. App Store apps are not in the list.</p>` : ""}`;
}

async function downloadBrewfile() {
  const r = await fetch("/api/brewfile", { headers: { "X-Brewdash-Token": TOKEN } });
  if (!r.ok) return alertBox(`HTTP ${r.status}`);
  // As text/plain, Chrome saves it as Brewfile.txt, and brew bundle --file=Brewfile then finds nothing.
  const url = URL.createObjectURL(new Blob([await r.text()], { type: "application/octet-stream" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: "Brewfile" });
  a.click();
  URL.revokeObjectURL(url);
}

async function renderHistory() {
  try { S.history = await api("/api/history"); } catch (e) { S.history = []; }
  if (S.tab !== "history") return;
  $("#content").innerHTML = `<table><thead><tr><th>When</th><th>Action</th><th>Status</th><th>Commands</th><th>Snapshot</th></tr></thead><tbody>
    ${S.history.map((h) => `<tr><td class="mono">${new Date(h.created * 1000).toLocaleString()}</td><td>${esc(h.title)}</td>
      <td class="st st-${esc(h.status)}">${esc(h.status)}${h.exit_code ? ` (${h.exit_code})` : ""}</td>
      <td class="mono">${h.commands.map(esc).join("<br>")}</td><td class="mono muted">${esc(h.snapshot || "")}</td></tr>`).join("")}
    </tbody></table>${S.history.length ? "" : `<p class="muted">No actions yet.</p>`}`;
}

function renderInstall() {
  const s = S.search;
  const pv = s.preview;
  const box = $("#search-q");
  const draft = box ? box.value : s.q;
  const focus = box && document.activeElement === box ? [box.selectionStart, box.selectionEnd] : null;
  $("#content").innerHTML = `
    <div class="toolbar"><input type="search" id="search-q" placeholder="Search Homebrew (formulae and casks), press Enter" value="${esc(draft)}" style="width:420px">
      <button id="search-go">Search</button>${s.busy ? `<span class="muted">searching…</span>` : ""}</div>
    <div style="display:flex;gap:16px;align-items:start">
      <div style="flex:1">${s.results == null ? `<p class="muted">Search runs <span class="mono">brew search --formula</span> and <span class="mono">--cask</span>.</p>` :
        s.results.length ? `<table><thead><tr><th>Name</th><th>Kind</th><th></th></tr></thead><tbody>${s.results.map((r) =>
          `<tr class="row" data-preview="${esc(r.kind)}:${esc(r.name)}"><td class="name">${esc(r.name)}</td><td>${r.kind}</td><td>${r.installed ? `<span class="badge b-service">installed</span>` : ""}</td></tr>`).join("")}</tbody></table>` :
        `<p class="muted">No results.</p>`}</div>
      <div style="width:460px">${pv ? (pv.loading ? `<div class="card loading">Loading ${esc(pv.name)}…</div>` : pv.error ? `<div class="card warn">${esc(pv.error)}</div>` : `
        <div class="card"><h3 class="mono">${esc(pv.name)} <span class="muted">${esc(pv.version)}</span> <span class="badge b-cask">${pv.kind}</span></h3>
          <p>${esc(pv.desc || "")}</p>${pv.homepage ? `<p>${link(pv.homepage)}</p>` : ""}
          ${pv.kind === "formula" ? `<p>${pv.new_deps.length ? `Will also install <b>${pv.new_deps.length}</b> new dependencies:` : "No new dependencies."}</p>
            <div class="chips">${pv.new_deps.map((d) => `<span class="chip">${esc(d)}</span>`).join("")}</div>` : ""}
          ${pv.caveats ? `<h4 class="muted">Caveats</h4><pre>${esc(pv.caveats)}</pre>` : ""}
          <div style="margin-top:12px">${S.pkgs.has(pv.id) ? `<span class="muted">Already installed.</span>` : `<button class="accent" data-install="${esc(pv.kind)}:${esc(pv.name)}">Install ${esc(pv.name)}</button>`}</div></div>`) : ""}</div>
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
    <h2>${esc(plan.title)}</h2>
    ${names.length > 1 ? `<div class="chips">${names.map((n) => `<span class="chip">${esc(n)}</span>`).join("")}</div>` : ""}
    <p class="muted">Will run:</p><pre>${plan.commands.map(esc).join("\n")}</pre>
    ${blocked ? `<div class="warn"><b>${imp.blockers.length} installed package(s) need this</b>, directly or through another dependency, so brew refuses the plain uninstall:<div class="chips" style="margin-top:6px">${imp.blockers.map(chip).join("")}</div>
      <div class="btnrow" style="margin-top:8px"><button class="small" id="add-blockers">Uninstall them too</button><button class="small danger" id="force-uninstall">Uninstall anyway and break them</button></div></div>` : ""}
    ${breaks.length ? `<div class="warn"><b>These stay installed but may stop working:</b><div class="chips" style="margin-top:6px">${breaks.map(chip).join("")}</div></div>` : ""}
    ${imp?.new_orphans.length ? `<div class="info">Afterwards <b>${imp.new_orphans.length}</b> dependencies will no longer be needed (autoremove can clean them):<div class="chips" style="margin-top:6px">${imp.new_orphans.map(chip).join("")}</div></div>` : ""}
    ${plan.snapshot ? `<div class="info">A Brewfile snapshot of the current install is written first.</div>` : ""}
    <div class="modal-actions"><button id="m-cancel">Cancel</button>${blocked ? "" : `<button id="m-ok" class="${req.action === "uninstall" || req.action === "autoremove" ? "danger" : "accent"}">Run</button>`}</div>`;
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
      loadJobs(); renderBulk();
    } catch (e) { alertBox(e.message); }
  };
  const add = $("#add-blockers");
  if (add) add.onclick = () => confirmAction({ ...req, targets: [...new Set([...req.targets, ...imp.blockers])] }, { impact: true });
  const force = $("#force-uninstall");
  if (force) force.onclick = () => confirmAction({ ...req, ignore_dependencies: true }, { breaks: imp.blockers });
}

function alertBox(msg) {
  $("#modal-card").innerHTML = `<h2>Error</h2><div class="warn">${esc(msg)}</div><div class="modal-actions"><button id="m-cancel">Close</button></div>`;
  $("#modal").classList.remove("hidden");
  $("#m-cancel").onclick = closeModal;
}

function act(action, targets) {
  return confirmAction({ action, targets }, { impact: action === "uninstall" });
}

// Each bulk action, the selected ids it applies to, and why it is off when it applies to none.
function bulkActions(ids) {
  const pk = ids.filter((id) => S.pkgs.has(id)).map((id) => S.pkgs.get(id));
  const items = ids.map(itemFor).filter(Boolean);
  const fresh = items.filter((i) => !inList(i));
  const none = pk.length ? null : "Only installed formulae and casks; the selection has none.";
  return [
    ["upgrade", "Upgrade", "accent", pk.filter(upgradable).map((p) => p.id), none || "Nothing selected is outdated, or it is pinned or disabled."],
    ["pin", "Pin", "", pk.filter((p) => !p.pinned).map((p) => p.id), none || "Everything selected is already pinned."],
    ["unpin", "Unpin", "", pk.filter((p) => p.pinned).map((p) => p.id), none || "Nothing selected is pinned."],
    ["uninstall", "Uninstall", "danger", pk.map((p) => p.id), none],
    ["list", "Add to list", "", ids.filter((id) => itemFor(id) && fresh.some((i) => i.type === itemFor(id).type && i.name === itemFor(id).name)),
      items.length ? "Everything selected is already on the export list." : "Only packages, taps and apps with a brew cask can go on the list."],
  ];
}

function renderBulk() {
  const el = $("#bulk");
  const ids = [...S.selected];
  el.classList.toggle("hidden", !ids.length);
  if (!ids.length) return;
  el.innerHTML = `<b>${ids.length} selected</b>
    ${bulkActions(ids).map(([k, label, cls, applies, why]) => `<button class="${cls}" data-bulk="${k}" ${applies.length ? "" : `disabled title="${esc(why)}"`}>${label} (${applies.length})</button>`).join("")}
    <button data-bulk="clear">Clear</button>
    <span class="muted hint">Each button acts only on the selected items it applies to.</span>`;
}

// ---------- jobs ----------

function renderJobs() {
  const active = S.jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  $("#jobs-title").innerHTML = `Jobs ${active ? `<span class="st st-running">${active} active</span>` : `<span class="muted">${S.jobs.length}</span>`}`;
  const body = $("#jobs-body");
  const scroll = {};
  body.querySelectorAll("pre[data-log]").forEach((pre) => { scroll[pre.dataset.log] = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4; });
  body.innerHTML = [...S.jobs].reverse().map((j) => `
    <div class="job"><div class="job-head" data-job="${j.id}"><span class="st st-${j.status}">${j.status}</span><b>${esc(j.title)}</b>
      <span class="muted">${new Date(j.created * 1000).toLocaleTimeString()}</span>${j.snapshot ? `<span class="muted mono">snapshot: ${esc(j.snapshot)}</span>` : ""}</div>
      ${j.status === "failed" ? `<div class="terminal-hint">${j.needs_terminal ? "This needs your sudo password. Run it in Terminal:" : "Failed. To retry or debug, run it in Terminal:"}
        <pre style="margin-top:6px">${j.commands.map(esc).join("\n")}</pre><button class="small" style="margin-top:6px" data-copy="${j.id}">Copy command</button></div>` : ""}
      ${S.openJobs.has(j.id) ? `<pre data-log="${j.id}">${esc(j.log.join("\n")) || "waiting…"}</pre>` : ""}</div>`).join("") || `<p class="muted">No jobs yet.</p>`;
  body.querySelectorAll("pre[data-log]").forEach((pre) => { if (scroll[pre.dataset.log] !== false) pre.scrollTop = pre.scrollHeight; });
}

// ---------- render root ----------

// What each page shows, and what you can do there. [title, what it is, what to do]
const INTRO = {
  requested: ["Installed by you", "Formulae you asked for with <b>brew install</b>. These are the roots: everything in Dependencies is here because one of them needs it.",
    "Click a row for its details, dependency graph and advice. Tick rows to upgrade, pin, uninstall or add them to the export list in one go. Uninstall first shows what would break."],
  deps: ["Dependencies", "Formulae that brew installed because another package needs them. An <b>orphan</b> is one that nothing needs any more.",
    "You rarely act here directly. Use the <b>orphan</b> filter and <b>Autoremove</b> to clear leftovers; if you use one of these tools yourself, keep it installed by you instead."],
  casks: ["Casks", "Apps and other binaries installed with <b>brew install --cask</b>.",
    "Upgrade, pin or uninstall them like formulae. Casks that update themselves may look outdated here while the app is current; their detail panel says so."],
  services: ["Services", "Formulae that can run in the background through <b>brew services</b> (databases, queues, web servers), with their state and the ports they listen on.",
    "Start, stop or restart a service. A port marked in amber listens on every network interface, so other machines on your network can reach it; stop the service or bind it to 127.0.0.1 if that is not what you want."],
  taps: ["Taps", "The repositories brew reads formulae and casks from. <b>homebrew/core</b> and <b>homebrew/cask</b> are Homebrew's own; every other tap is code from a third party that brew runs as you.",
    "Untap a tap nothing is installed from. Keep a third-party tap only if you trust its owner; a <b>secure-it</b> badge means the scanner flagged it."],
  health: ["Health", "What brew itself reports: missing dependencies, deprecated or disabled packages, orphans, cache that cleanup can free, and the full <b>brew doctor</b> output.",
    "Work from the top: missing dependencies break packages now, deprecated ones will. Then autoremove and clean up to win back disk space."],
  security: ["Security", "Findings from <b>secure-it</b>, a separate scanner that runs as root and checks macOS settings, startup items, open ports, shell files and packages. brewdash only reads the report it leaves in your home folder.",
    "Read each finding and fix it with the command shown, or accept it in secure-it if it is expected. brewdash copies commands for you to run in Terminal; it never runs them itself."],
  apps: ["Apps", "Every app in /Applications and ~/Applications: how it got there (brew, App Store or by hand) and who signed it.",
    "<b>Bring into brew</b> lets Homebrew take over an app you installed by hand, so it upgrades with everything else. Check apps marked <b>not signed</b> or <b>ad-hoc</b>: nothing vouches for who built them."],
  shell: ["Shell", "The PATH lines and tools your shell startup files (.zshrc, .zprofile, .bashrc and friends) set up.",
    "Fix what is flagged by editing the file yourself; brewdash never edits these files. A <b>synced folder first</b> entry puts a Dropbox or iCloud folder before the system folders, so a file that arrives by sync runs in place of a system command."],
  history: ["History", "Every action brewdash ran, with its commands and exit status. Before each uninstall, autoremove or multi-package action, brewdash writes a Brewfile snapshot of what was installed.",
    "To undo, restore a snapshot with <span class=\"mono\">brew bundle install --file=&lt;snapshot&gt;</span>."],
  install: ["Install", "Search Homebrew's formulae and casks, and see what an install would bring with it before you run it.",
    "Search, click a result to preview its new dependencies and caveats, then install."],
  export: ["Export", "A list of taps, formulae and casks to set up another Mac with, written as a Brewfile.",
    "Select packages, taps or apps in any tab and press <b>Add to list</b>; the list is kept between runs. Then download the Brewfile and run <span class=\"mono\">brew bundle</span> on the new Mac."],
};

function renderIntro() {
  if (S.tab === "overview") { $("#intro").innerHTML = hero(); return; }
  const [title, what, todo] = INTRO[S.tab];
  $("#intro").innerHTML = `<div class="intro"><h2>${title}</h2><p>${what}</p><p><b>What to do:</b> ${todo}</p></div>`;
}

function hero() {
  return `<div class="hero"><div>
      <h1>brew<span>dash</span></h1>
      <p class="lead">A local dashboard for Homebrew: see what is installed on this Mac, why it is there, and what needs your attention.</p>
      <ul>
        <li><b>Understand</b> every formula, cask, service and tap, with dependency graphs that show who needs what.</li>
        <li><b>Clean up</b> orphans, stale caches, pinned and deprecated packages, with a Brewfile snapshot before anything is removed.</li>
        <li><b>Stay safe</b>: ports your services open to the network, unsigned apps, risky PATH entries and, with <a href="https://github.com/lemiorhan/secure-it" target="_blank" rel="noopener">secure-it</a>, a full security scan.</li>
        <li><b>Move</b> to a new Mac with an exported Brewfile.</li>
      </ul></div>
    <div class="hero-side"><h4>How it works</h4>
      <p>brewdash runs on 127.0.0.1 only and every request carries a one-time token. It reads brew's own JSON output and your files, and changes nothing until you press a button and confirm the exact commands it will run.</p>
      <p>Start below with <b>Needs attention</b>; each line opens the page where you can act on it.</p></div></div>`;
}

function renderView() {
  renderIntro();
  if (!S.snap?.model) { $("#content").innerHTML = `<p class="loading">${S.snap?.error ? "Failed to read brew state." : "Reading Homebrew state…"}</p>`; return; }
  if (PKG_TABS[S.tab]) renderPackages();
  else ({ overview: renderOverview, services: renderServices, taps: renderTaps, health: renderHealth, history: renderHistory, install: renderInstall,
    apps: renderApps, shell: renderShell, export: renderExport, security: renderSecurity })[S.tab]();
}

function renderAll() {
  renderHeader(); renderTabs(); renderView(); renderDetail(); renderBulk();
}

// ---------- events ----------

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
    act(b, ids); return;
  }
  if (d("unlist")) { const [type, ...n] = d("unlist").split(":"); loadList({ op: "remove", items: [{ type, name: n.join(":") }] }); return; }
  if (d("adopt")) { confirmAction({ action: "adopt", app: d("adopt") }); return; }
  if (d("findcask")) { S.tab = "install"; renderTabs(); renderView(); $("#search-q").value = d("findcask"); doSearch(); return; }
  if (t.id === "dl-brewfile") { downloadBrewfile(); return; }
  if (t.id === "copy-brewfile") { navigator.clipboard.writeText(S.list.brewfile); t.textContent = "Copied"; return; }
  if (d("global")) { confirmAction({ action: d("global") }); return; }
  if (d("preview")) { const [k, ...n] = d("preview").split(":"); doPreview(k, n.join(":")); return; }
  if (d("install")) { const [k, ...n] = d("install").split(":"); confirmAction({ action: "install", kind: k, name: n.join(":") }); return; }
  if (t.id === "search-go") { doSearch(); return; }
  if (d("copy")) { const j = S.jobs.find((x) => x.id === +d("copy")); navigator.clipboard.writeText(j.commands.join(" && ")); t.textContent = "Copied"; return; }
  if (t.matches("[data-copytext]")) { navigator.clipboard.writeText(t.dataset.copytext); t.textContent = "Copied"; return; }
  if (d("job")) { const id = +d("job"); S.openJobs.has(id) ? S.openJobs.delete(id) : S.openJobs.add(id); loadJobs(); return; }
  if (t.closest("#jobs-head")) { $("#jobs").classList.toggle("collapsed"); return; }
  const row = t.closest("tr.row[data-id]");
  if (row && !t.matches("input")) select(row.dataset.id);
});

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
renderAll();
loadList();
tick();
