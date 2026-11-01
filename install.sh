#!/bin/bash
# Installs brewdash.app, or updates it in place:
#   curl -fsSL https://raw.githubusercontent.com/lemiorhan/brewdash/master/install.sh | bash
#   wget -qO- https://raw.githubusercontent.com/lemiorhan/brewdash/master/install.sh | bash
# Removes it (your settings and Brewfile snapshots stay):
#   curl -fsSL https://raw.githubusercontent.com/lemiorhan/brewdash/master/install.sh | bash -s -- --uninstall
#
# BREWDASH_APP_DIR   where the app goes; default /Applications if you can write to it, else ~/Applications
# BREWDASH_REF       tag, branch or commit to install; default the highest vX.Y.Z tag, or master if there is none
# BREWDASH_TARBALL   a source archive URL to install from instead (file:// works); that app does not update itself
# BREWDASH_NO_OPEN   set to install without starting the app
set -euo pipefail

REPO="lemiorhan/brewdash"
API="https://api.github.com/repos/$REPO"
BUNDLE_ID="io.github.lemiorhan.brewdash"
work=""

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mbrewdash: %s\033[0m\n' "$*" >&2; exit 1; }

app_dir() {
  if [ -n "${BREWDASH_APP_DIR:-}" ]; then echo "$BREWDASH_APP_DIR"
  elif [ -w /Applications ]; then echo /Applications
  else echo "$HOME/Applications"
  fi
}

# Only ever replace or remove an app that is brewdash.
check_ours() {
  [ -e "$1" ] || return 0
  local id
  id="$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$1/Contents/Info.plist" 2>/dev/null || true)"
  [ "$id" = "$BUNDLE_ID" ] || fail "$1 is not brewdash's app (its bundle id is '${id:-none}'); move it away first."
}

quit_running() {
  if [ "$(osascript -e "application id \"$BUNDLE_ID\" is running" 2>/dev/null)" = "true" ]; then
    say "Quitting the running brewdash"
    osascript -e "tell application id \"$BUNDLE_ID\" to quit"
  fi
}

# The stable channel: the highest vX.Y.Z tag, or nothing.
latest_tag() {
  curl -fsSL "$API/tags?per_page=100" | python3 -c '
import json, re, sys
tags = [t["name"] for t in json.load(sys.stdin) if re.match(r"^v[0-9]+[.][0-9]+[.][0-9]+$", t.get("name", ""))]
print(max(tags, key=lambda n: tuple(map(int, n[1:].split(".")))) if tags else "")'
}

# What the app's version.json says, so the app can tell whether GitHub has something newer.
version_json() {
  local ref="$1" sha
  sha="$(curl -fsSL -H "Accept: application/vnd.github.sha" "$API/commits/$ref")" || fail "GitHub does not know $ref."
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail "GitHub did not answer with a commit for $ref."
  python3 -c 'import json, re, sys; print(json.dumps({"sha": sys.argv[1], "tag": sys.argv[2] if re.match(r"^v[0-9]+[.][0-9]+[.][0-9]+$", sys.argv[2]) else None}))' "$sha" "$ref"
}

main() {
  [ "$(uname -s)" = Darwin ] || fail "brewdash runs on macOS only."
  local app
  app="$(app_dir)/brewdash.app"

  if [ "${1:-}" = "--uninstall" ]; then
    [ -e "$app" ] || fail "there is no brewdash.app in $(dirname "$app")."
    check_ours "$app"
    quit_running
    rm -rf "$app"
    say "Removed $app. Settings and Brewfile snapshots stay in ${XDG_STATE_HOME:-$HOME/.local/state}/brewdash."
    return
  fi

  command -v brew >/dev/null || [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ] \
    || fail "Homebrew is not installed. Install it first: https://brew.sh"
  { command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; } \
    || fail "brewdash needs Python 3.9 or newer. Install the Command Line Tools: xcode-select --install"
  check_ours "$app"

  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  local tarball version=""
  if [ -n "${BREWDASH_TARBALL:-}" ]; then
    tarball="$BREWDASH_TARBALL"
  else
    local ref="${BREWDASH_REF:-}"
    if [ -z "$ref" ]; then
      ref="$(latest_tag)" || fail "could not read brewdash's tags from GitHub."
      ref="${ref:-master}"
    fi
    version="$(version_json "$ref")"
    tarball="https://github.com/$REPO/archive/$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["sha"])' "$version").tar.gz"
  fi
  say "Downloading $tarball"
  mkdir "$work/src"
  curl -fsSL "$tarball" | tar -xz -C "$work/src" --strip-components=1
  say "Building brewdash.app"
  local log
  if ! log="$(BREWDASH_VERSION="$version" bash "$work/src/macos/build-app.sh" "$work/src" "$work/brewdash.app" 2>&1)"; then
    printf '%s\n' "$log" >&2
    fail "building the app failed."
  fi

  quit_running
  mkdir -p "$(dirname "$app")"
  rm -rf "$app"
  mv "$work/brewdash.app" "$app"
  say "Installed $app. Open it from Launchpad, Spotlight or the Dock; quit it to stop brewdash."
  [ -n "${BREWDASH_NO_OPEN:-}" ] || open "$app"
}

# Tests read the functions above without installing anything.
[ -n "${BREWDASH_SOURCE_ONLY:-}" ] || main "$@"
