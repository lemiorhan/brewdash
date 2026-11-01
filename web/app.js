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
  tab: "requested", q: "", flags: new Set(), sort: { key: "name", dir: 1 },
  selected: new Set(), current: null, depth: 1,
  jobs: [], openJobs: new Set(), history: [],
  search: { q: "", results: null, preview: null, busy: false },
};

const TABS = [
  ["requested", "Installed by you"], ["deps", "Dependencies"], ["casks", "Casks"],
  ["services", "Services"], ["taps", "Taps"], ["health", "Health"],
  ["history", "History"], ["install", "Install"],
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
    if (snap.version === S.version && snap.refreshing === S.snap?.refreshing) return;
    S.snap = snap; S.version = snap.version;
    S.pkgs = new Map((snap.model?.packages || []).map((p) => [p.id, p]));
    for (const id of [...S.selected]) if (!S.pkgs.has(id)) S.selected.delete(id);
    if (S.current && !S.pkgs.has(S.current)) S.current = null;
    renderAll();
  } catch (e) {
    $("#error").classList.remove("hidden");
    $("#error").textContent = "Cannot reach brewdash server: " + e.message;
  }
}

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
const FLAGS = [["outdated", (p) => p.outdated], ["orphan", (p) => p.orphan], ["pinned", (p) => p.pinned], ["missing", (p) => p.missing_deps.length > 0], ["service", (p) => !!p.service]];
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
  $("#view").innerHTML = `
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
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:start">
      <div><h2 class="detail-title">${esc(p.name)}</h2><div>${p.kind === "cask" ? `<span class="badge b-cask">cask</span>` : ""}${badges(p)}</div></div>
      <button class="small" data-close>✕</button>
    </div>
    <p>${esc(p.desc || "No description.")}</p>
    ${renderAdvice(p)}
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
      ${svc.log_path ? `<div>Log</div><div class="mono">${esc(svc.log_path)}</div>` : ""}</div>
      <div class="btnrow" style="margin-top:6px">${["start", "stop", "restart"].map((op) => `<button class="small" data-svc="${op}" data-id="${esc(p.id)}">${op}</button>`).join("")}</div></div>` : ""}
    <div class="detail-section"><h4>Depends on (${p.deps.length})</h4><div class="chips">${p.deps.map(chip).join("") || `<span class="muted">nothing</span>`}</div></div>
    <div class="detail-section"><h4>Used by (${p.dependents.length})</h4><div class="chips">${p.dependents.map(chip).join("") || `<span class="muted">nothing installed depends on this</span>`}</div></div>
    <div class="detail-section"><h4>Graph
      <select id="depth" style="margin-left:8px"><option value="1" ${S.depth === 1 ? "selected" : ""}>1 hop</option><option value="2" ${S.depth === 2 ? "selected" : ""}>2 hops</option><option value="3" ${S.depth === 3 ? "selected" : ""}>3 hops</option></select></h4>
      <div id="graph"></div>
      <div class="legend"><span><i class="dot" style="background:var(--accent)"></i>selected</span><span><i class="dot" style="background:var(--blue)"></i>installed by you</span><span><i class="dot" style="background:#59606d"></i>dependency</span><span><i class="dot" style="background:var(--purple)"></i>cask</span><span>arrow = depends on</span></div>
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
  const color = (id) => {
    const n = S.pkgs.get(id);
    return id === p.id ? "#f2b33d" : n.kind === "cask" ? "#b18cf2" : n.requested ? "#5aa9e6" : "#59606d";
  };
  cy = cytoscape({
    container: $("#graph"),
    elements: [
      ...nodes.map((id) => ({ data: { id, label: S.pkgs.get(id).name, color: color(id), border: S.pkgs.get(id).outdated ? "#f2b33d" : "#0b0d10" } })),
      ...edges.map(([s, t]) => ({ data: { id: s + ">" + t, source: s, target: t } })),
    ],
    style: [
      { selector: "node", style: { "background-color": "data(color)", label: "data(label)", color: "#cfd3da", "font-size": 9, "text-valign": "bottom", "text-margin-y": 3, width: 14, height: 14, "border-width": 2, "border-color": "data(border)" } },
      { selector: `node[id = "${p.id}"]`, style: { width: 22, height: 22, "font-size": 11, "font-weight": 700 } },
      { selector: "edge", style: { width: 1, "line-color": "#3a414d", "target-arrow-color": "#3a414d", "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.7 } },
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

function renderServices() {
  const list = [...S.pkgs.values()].filter((p) => p.service);
  $("#view").innerHTML = `<table><thead><tr><th>Service</th><th>Status</th><th>PID</th><th>User</th><th>Plist</th><th></th></tr></thead><tbody>
    ${list.map((p) => `<tr><td class="name"><span class="chip" data-goto="${esc(p.id)}">${esc(p.name)}</span></td>
      <td class="st ${p.service.running ? "st-succeeded" : "st-queued"}">${esc(p.service.status)}</td>
      <td class="mono">${esc(p.service.pid || "")}</td><td>${esc(p.service.user || "")}</td>
      <td class="mono muted">${esc(p.service.file || "")}</td>
      <td class="btnrow">${["start", "stop", "restart"].map((op) => `<button class="small" data-svc="${op}" data-id="${esc(p.id)}">${op}</button>`).join("")}</td></tr>`).join("")}
  </tbody></table>${list.length ? "" : `<p class="muted">No installed formula defines a service.</p>`}`;
}

function renderTaps() {
  const taps = S.snap?.model?.taps || [];
  $("#view").innerHTML = `<table><thead><tr><th>Tap</th><th>Remote</th><th class="num">Installed from it</th><th class="num">Formulae</th><th class="num">Casks</th><th>Last commit</th></tr></thead><tbody>
    ${taps.map((t) => `<tr><td class="name">${esc(t.name)}${t.official ? `<span class="badge b-service">official</span>` : ""}${t.private ? `<span class="badge b-pinned">private</span>` : ""}</td>
      <td class="mono muted">${esc(t.remote || "")}</td><td class="num">${t.installed.length || `<span class="muted">0 · unused?</span>`}</td>
      <td class="num">${t.formula_count}</td><td class="num">${t.cask_count}</td><td class="muted">${esc(t.last_commit || "")}</td></tr>
      ${t.installed.length && !t.official ? `<tr><td></td><td colspan="5"><div class="chips">${t.installed.map(chip).join("")}</div></td></tr>` : ""}`).join("")}
  </tbody></table>`;
}

function renderHealth() {
  const h = S.snap?.health;
  const all = [...S.pkgs.values()];
  const missing = all.filter((p) => p.missing_deps.length);
  const deprecated = all.filter((p) => p.deprecated || p.disabled);
  const orphans = all.filter((p) => p.orphan);
  $("#view").innerHTML = `<div class="cards">
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

async function renderHistory() {
  try { S.history = await api("/api/history"); } catch (e) { S.history = []; }
  if (S.tab !== "history") return;
  $("#view").innerHTML = `<p class="muted">Brewfile snapshots are written before every uninstall, autoremove and multi-package action. Restore with <span class="mono">brew bundle install --file=&lt;snapshot&gt;</span>.</p>
    <table><thead><tr><th>When</th><th>Action</th><th>Status</th><th>Commands</th><th>Snapshot</th></tr></thead><tbody>
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
  $("#view").innerHTML = `
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

async function confirmAction(req, { impact = false } = {}) {
  let plan, imp = null;
  try {
    plan = await api("/api/plan", req);
    if (impact) imp = await api("/api/impact", { targets: req.targets });
  } catch (e) { alertBox(e.message); return; }
  const names = (req.targets || []).map((id) => S.pkgs.get(id)?.name || id);
  $("#modal-card").innerHTML = `
    <h2>${esc(plan.title)}</h2>
    ${names.length > 1 ? `<div class="chips">${names.map((n) => `<span class="chip">${esc(n)}</span>`).join("")}</div>` : ""}
    <p class="muted">Will run:</p><pre>${plan.commands.map(esc).join("\n")}</pre>
    ${imp?.blockers.length ? `<div class="warn"><b>${imp.blockers.length} installed package(s) depend on this.</b> brew will refuse the uninstall unless you remove them too:<div class="chips" style="margin-top:6px">${imp.blockers.map(chip).join("")}</div>
      <div style="margin-top:8px"><button class="small" id="add-blockers">Add them to the selection</button></div></div>` : ""}
    ${imp?.new_orphans.length ? `<div class="info">Afterwards <b>${imp.new_orphans.length}</b> dependencies will no longer be needed (autoremove can clean them):<div class="chips" style="margin-top:6px">${imp.new_orphans.map(chip).join("")}</div></div>` : ""}
    ${plan.snapshot ? `<div class="info">A Brewfile snapshot of the current install is written first.</div>` : ""}
    <div class="modal-actions"><button id="m-cancel">Cancel</button><button id="m-ok" class="${req.action === "uninstall" || req.action === "autoremove" ? "danger" : "accent"}">Run</button></div>`;
  $("#modal").classList.remove("hidden");
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
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
}

function alertBox(msg) {
  $("#modal-card").innerHTML = `<h2>Error</h2><div class="warn">${esc(msg)}</div><div class="modal-actions"><button id="m-cancel">Close</button></div>`;
  $("#modal").classList.remove("hidden");
  $("#m-cancel").onclick = closeModal;
}

function act(action, targets) {
  return confirmAction({ action, targets }, { impact: action === "uninstall" });
}

function renderBulk() {
  const el = $("#bulk");
  const ids = [...S.selected];
  el.classList.toggle("hidden", !ids.length);
  if (!ids.length) return;
  const anyOutdated = ids.some((id) => S.pkgs.has(id) && upgradable(S.pkgs.get(id)));
  el.innerHTML = `<b>${ids.length} selected</b>
    <button class="accent" data-bulk="upgrade" ${anyOutdated ? "" : "disabled"}>Upgrade</button>
    <button data-bulk="pin">Pin</button><button data-bulk="unpin">Unpin</button>
    <button class="danger" data-bulk="uninstall">Uninstall</button>
    <button data-bulk="clear">Clear</button>`;
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

function renderView() {
  if (!S.snap?.model) { $("#view").innerHTML = `<p class="loading">${S.snap?.error ? "Failed to read brew state." : "Reading Homebrew state…"}</p>`; return; }
  if (PKG_TABS[S.tab]) renderPackages();
  else ({ services: renderServices, taps: renderTaps, health: renderHealth, history: renderHistory, install: renderInstall })[S.tab]();
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
    const ids = [...S.selected].filter((id) => b !== "upgrade" || upgradable(S.pkgs.get(id)));
    act(b, ids); return;
  }
  if (d("global")) { confirmAction({ action: d("global") }); return; }
  if (d("preview")) { const [k, ...n] = d("preview").split(":"); doPreview(k, n.join(":")); return; }
  if (d("install")) { const [k, ...n] = d("install").split(":"); confirmAction({ action: "install", kind: k, name: n.join(":") }); return; }
  if (t.id === "search-go") { doSearch(); return; }
  if (d("copy")) { const j = S.jobs.find((x) => x.id === +d("copy")); navigator.clipboard.writeText(j.commands.join(" && ")); t.textContent = "Copied"; return; }
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
tick();
