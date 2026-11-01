"use strict";
// Loaded in <head> so the chosen theme is set before the first paint.
const THEMES = ["auto", "light", "dark"];
const THEME_LABEL = { auto: "◐ auto", light: "☀ light", dark: "☾ dark" };

function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = "auto";
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("brewdash-theme", theme); } catch (e) { /* private mode: the choice lasts this page only */ }
  const btn = document.getElementById("btn-theme");
  if (btn) { btn.textContent = THEME_LABEL[theme]; btn.title = "Theme: " + theme + ". Click to change."; }
  return theme;
}

function currentTheme() {
  try { return localStorage.getItem("brewdash-theme") || "auto"; } catch (e) { return "auto"; }
}

applyTheme(currentTheme());
