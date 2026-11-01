#!/bin/bash
# Builds brewdash.app from a brewdash source folder, with tools every Mac has.
#   bash macos/build-app.sh [source folder] [output .app]     defaults: this checkout, build/brewdash.app
set -euo pipefail

src="$(cd "${1:-$(dirname "$0")/..}" && pwd)"
out="${2:-$src/build/brewdash.app}"
case "$out" in
  *.app) ;;
  *) echo "the output must be a .app path: $out" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$out")"
rm -rf "$out"
osacompile -s -o "$out" "$src/macos/brewdash.applescript"

res="$out/Contents/Resources"
mkdir "$res/brewdash"
cp "$src/brewdash.py" "$src/macos/launch.sh" "$res/brewdash/"
cp -R "$src/web" "$res/brewdash/web"
find "$res/brewdash/web" -name '.*' -type f -delete

# osacompile's icon lives in Assets.car, which macOS prefers over applet.icns.
cp "$src/macos/brewdash.icns" "$res/applet.icns"
rm -f "$res/Assets.car"
plist="$out/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier io.github.lemiorhan.brewdash" "$plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string io.github.lemiorhan.brewdash" "$plist"

# Changing the bundle broke osacompile's signature; Apple silicon runs nothing unsigned.
codesign --force --sign - "$out"
touch "$out"
echo "$out"
