"use strict";
// Loaded in <head> so the chosen theme is set before the first paint.
const THEMES = ["auto", "light", "dark"];
const THEME_ICON = { auto: "◐", light: "☀", dark: "☾" };

function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = "auto";
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("brewdash-theme", theme); } catch (e) { /* private mode: the choice lasts this page only */ }
  const btn = document.getElementById("btn-theme");
  if (btn) {
    const name = t("theme." + theme);
    btn.textContent = `${THEME_ICON[theme]} ${name}`;
    btn.title = t("theme.title", { name });
  }
  tellWindow(theme);
  return theme;
}

// In the Mac app the window takes the theme and the panel colour; a browser has no such handler.
function tellWindow(theme) {
  const handler = window.webkit?.messageHandlers?.brewdash;
  if (handler) handler.postMessage({ theme, panel: getComputedStyle(document.documentElement).getPropertyValue("--panel") });
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => tellWindow(currentTheme()));

function currentTheme() {
  try { return localStorage.getItem("brewdash-theme") || "auto"; } catch (e) { return "auto"; }
}

applyTheme(currentTheme());
