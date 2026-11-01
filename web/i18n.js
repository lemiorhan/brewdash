"use strict";
// Loaded in <head>, before the catalogs in web/i18n/ fill I18N, so theme.js and app.js can translate.
const LANGS = { en: "English", tr: "Türkçe", es: "Español", zh: "中文" };
const I18N = {};

function detectLang() {
  try { const saved = localStorage.getItem("brewdash-lang"); if (LANGS[saved]) return saved; } catch (e) { /* private mode */ }
  for (const l of navigator.languages || [navigator.language || ""]) {
    const base = l.toLowerCase().split("-")[0];
    if (LANGS[base]) return base;
  }
  return "en";
}

let LANG = detectLang();

const fill = (s, vars) => s.replace(/\{(\w+)\}/g, (m, k) => k in vars ? vars[k] : m);

// A catalog value is a string, or {one, other, ...} chosen by Intl.PluralRules on vars.n.
function pick(value, vars) {
  if (typeof value !== "object") return value;
  return value[new Intl.PluralRules(LANG).select(vars.n)] ?? value.other;
}

// The text of `key` in the chosen language, falling back to English. Vars are put in as given:
// a caller that puts the result into HTML escapes the vars that came from outside.
function t(key, vars = {}) {
  const value = I18N[LANG]?.[key] ?? I18N.en?.[key];
  return value == null ? key : fill(pick(value, vars), vars);
}

// A message the server sent as [key, args] (brewdash.py MESSAGES). The server also sends its English text,
// which is used when the chosen language has no translation for the message or for a message inside it.
function tm(msg, english) {
  const render = (m) => {
    if (!Array.isArray(m)) return m;
    const [key, args] = m;
    const value = I18N[LANG]?.["srv." + key];
    if (value == null) return null;
    const vars = {};
    for (const [k, v] of Object.entries(args || {})) { vars[k] = render(v); if (vars[k] == null) return null; }
    return fill(pick(value, { n: vars.count, ...vars }), vars);
  };
  if (LANG === "en" || !msg) return english;
  return render(msg) ?? english;
}

// Elements in index.html carry data-i18n (text) and data-i18n-title (tooltip) with a catalog key.
function translateStatic(root = document) {
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : LANG;
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
}

function setLang(lang) {
  LANG = LANGS[lang] ? lang : "en";
  try { localStorage.setItem("brewdash-lang", LANG); } catch (e) { /* private mode: the choice lasts this page only */ }
  translateStatic();
}
