# Changelog

All notable changes to brewdash are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are
tagged `vX.Y.Z`; changes from before the first one are listed by date.

## Unreleased

### Added
- Plugins: a separate tool that writes a JSON report gets its own tab, in a
  group above History, and its findings show next to the packages, taps and
  shell files they concern. The brewdash security plugin is in the built-in
  catalog; another tool joins with a manifest in
  `~/.local/state/brewdash/plugins/`. The README's "Plugins" section has the
  manifest and report formats.
- A plugin's tab groups its findings by category and topic, worst first, with
  search, filters for severity, category and effort, and the accepted findings
  and info lines on request. It also shows each kept run's open findings and
  what came and went, and the checks that failed or were skipped. The report
  (schema 3) carries its texts in several languages, and the tab shows the one
  you chose.
- **Scan now** on the Security tab runs `/usr/bin/sudo -A
  /usr/local/bin/secure-it scan --exit-zero` after you confirm. Only a plugin
  in brewdash's catalog can be started this way, and only when root alone can
  change the plugin's files and every folder above them. On an Intel Mac, where
  Homebrew gives `/usr/local/bin` to your user, the tab says the plugin cannot
  be installed safely.

### Changed
- The Security tab is now the tab of the brewdash security plugin, the new
  name of secure-it. Its command and paths keep the old name. brewdash reads
  its report at `~/.local/state/brewdash/plugins/secure-it/report.json`, in
  report schema 3 only. With an older version, or with only its old
  `~/secure-it/report.json`, the tab says to update it and gives the commands.
- The Mac app's window is at least 620 points tall, so the built-in tabs
  still fit in the sidebar with the plugin group's line.
- **Bring into brew** sets the app aside as a backup, then runs
  `brew install --cask` into the app's own folder, instead of
  `brew install --cask --adopt`, which refuses an app whose version is not the
  cask's unless the cask updates itself. The backup is removed only when brew
  succeeds and an app is back at its path; otherwise it is put back, unless
  brew left something there. The confirm dialog asks you to quit the app first.
  Only the `.app` moves; the app's settings in `~/Library` are not touched.
  An app the last scan saw as newer than its cask, by the leading numbers of
  both versions, is not offered and is refused.

### Fixed
- The install command trusts brewdash's tap first, with
  `brew trust lemiorhan/brewdash`: Homebrew refuses a cask from a tap you have
  not trusted. The README installs with the cask too, and keeps `install.sh`
  for building the app on your Mac.
- The app no longer fails to open where looking up 127.0.0.1's name in DNS is
  slow: the server starts without that lookup.

### Removed
- The `brewdash.app` that opens the dashboard in your browser, and
  `install.sh --browser` and `--app` with it. `install.sh` builds the Mac app
  from every release, and an app of the browser kind becomes the Mac app at its
  next update, on a Mac that has `swiftc`. `python3 brewdash.py` still serves
  the dashboard to your browser, for development.

## [0.1.0] - 2026-11-03

### Added
- brewdash as a Mac app: the dashboard in its own window, with a menu bar,
  keyboard shortcuts and a save dialog for the Brewfile. `install.sh` builds
  it by default; `--browser` installs the app that opens the browser instead.
  Only one kind is installed at a time, and an update keeps the kind.
- The Mac app signed with a Developer ID and notarized by Apple, published on
  GitHub Releases as `brewdash-macos.zip`: a `vX.Y.Z` tag gets its own
  release, any other ref replaces the one `beta` prerelease. Install it with
  the `lemiorhan/brewdash/brewdash` cask, or download it from the latest
  release.
- The app updates itself from those releases. Stable is the latest release
  and beta the `beta` prerelease, no longer every commit on master. The Mac
  app installs the release's ZIP once Gatekeeper accepts it, instead of being
  built on the Mac.
- **Security** tab with the findings of
  [secure-it](https://github.com/lemiorhan/secure-it), read from
  `~/secure-it/report.json`. Findings appear in *Needs attention*, in the
  detail panel of the package they concern, and on the tap they flag.
- Listening ports of running brew services, with ports open to the network
  flagged.
- App signatures in the Apps tab, from `codesign`; unsigned and ad-hoc signed
  apps are flagged.
- The Shell tab flags a cloud-synced folder (Dropbox, iCloud Drive,
  `~/Library/CloudStorage`) ahead of the system folders in `PATH`.
- Light and dark themes, following the system or chosen with the theme button.
- Every page starts with what it shows and what to do there; the Overview
  explains brewdash itself.
- `scripts/demo.py` runs brewdash on made-up data; `scripts/screenshots.py`
  takes the README screenshots from it.
- Apache-2.0 license, contributing guide, code of conduct, security policy,
  and issue and pull request templates.

### Changed
- *Needs attention* is ordered worst first across all sources.
- The project page hands out the Homebrew cask and a download link instead of
  the install script.

## 2026-11-01

### Added
- Overview start page with totals, a *Needs attention* list and charts.
- Apps, Shell, bulk actions and Brewfile export.
- The first version: packages, dependencies, casks, services, taps, health,
  history and install, with a dependency graph and per-package advice.

### Fixed
- Uninstalling a package other packages still need now offers to remove them
  too, or to uninstall anyway and name what breaks.

[0.1.0]: https://github.com/lemiorhan/brewdash/releases/tag/v0.1.0
