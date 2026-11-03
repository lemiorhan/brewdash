#!/bin/bash
# Builds brewdash.app from a brewdash source folder, with tools every Mac with the Command Line Tools has.
#   bash macos/build-app.sh [source folder] [output .app]     defaults: this checkout, build/brewdash.app
#
# BREWDASH_UNIVERSAL      set: the app runs on Apple silicon and Intel from macOS $min_macos on. Unset: only on Macs
#                         like this one, with its architecture and macOS version.
# BREWDASH_SIGN_IDENTITY  a Developer ID Application identity: signed with it as notarization needs (hardened
#                         runtime, secure timestamp, macos/brewdash.entitlements). Unset: an ad-hoc signature.
set -euo pipefail

# The oldest macOS the Swift sources compile for: NSWindow.toolbarStyle and WKWebView.pageZoom are macOS 11.
min_macos=11.0
src="$(cd "${1:-$(dirname "$0")/..}" && pwd)"
out="${2:-$src/build/brewdash.app}"
identity="${BREWDASH_SIGN_IDENTITY:--}"
case "$out" in
  *.app) ;;
  *) echo "the output must be a .app path: $out" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$out")"
rm -rf "$out"
res="$out/Contents/Resources"
plist="$out/Contents/Info.plist"

mkdir -p "$out/Contents/MacOS" "$res"
exe="$out/Contents/MacOS/brewdash"
swift=(swiftc -O -swift-version 5 -parse-as-library "$src/macos/brewdash.swift" "$src/macos/policy.swift")
if [ -n "${BREWDASH_UNIVERSAL:-}" ]; then
  slices="$(mktemp -d)"
  trap 'rm -rf "$slices"' EXIT
  for arch in arm64 x86_64; do
    "${swift[@]}" -target "$arch-apple-macos$min_macos" -o "$slices/$arch"
  done
  lipo -create -output "$exe" "$slices/arm64" "$slices/x86_64"
else
  "${swift[@]}" -o "$exe"
fi
cp "$src/macos/brewdash.icns" "$res/brewdash.icns"
cat >"$plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>brewdash</string>
  <key>CFBundleIconFile</key><string>brewdash</string>
  <key>CFBundleIdentifier</key><string>io.github.lemiorhan.brewdash</string>
  <key>CFBundleName</key><string>brewdash</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleShortVersionString</key><string>0</string>
  <key>CFBundleVersion</key><string>0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSAppleEventsUsageDescription</key><string>brewdash asks Finder to move the apps you choose to the Trash.</string>
</dict>
</plist>
PLIST
[ -z "${BREWDASH_UNIVERSAL:-}" ] || /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string $min_macos" "$plist"
version="$(printf '%s' "${BREWDASH_VERSION:-}" | python3 -c 'import json, sys
try: v = json.load(sys.stdin)
except ValueError: v = {}
print(v.get("tag") or (v.get("sha") or "")[:7] or "dev")')"
build="${version#v}"
[[ "$build" =~ ^[0-9]+[.][0-9]+[.][0-9]+$ ]] || build=0
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${version#v}" -c "Set :CFBundleVersion $build" "$plist"

mkdir "$res/brewdash"
cp "$src/brewdash.py" "$src/macos/launch.sh" "$src/install.sh" "$res/brewdash/"
# install.sh passes the commit it built, so the app can update itself; it must be in before codesign seals the bundle.
[ -z "${BREWDASH_VERSION:-}" ] || printf '%s\n' "$BREWDASH_VERSION" >"$res/brewdash/version.json"
cp -R "$src/web" "$res/brewdash/web"
find "$res/brewdash/web" -name '.*' -type f -delete

# Apple silicon runs nothing unsigned.
if [ "$identity" = - ]; then
  codesign --force --sign - "$out"
else
  codesign --force --sign "$identity" --options runtime --timestamp --entitlements "$src/macos/brewdash.entitlements" \
    "$out"
fi
touch "$out"
echo "$out"
