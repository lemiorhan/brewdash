# Changelog

All notable changes to brewdash are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). brewdash has no
numbered releases yet; changes are listed by date.

## Unreleased

### Added
- brewdash as a Mac app: the dashboard in its own window, with a menu bar,
  keyboard shortcuts and a save dialog for the Brewfile. `install.sh` builds
  it by default; `--browser` installs the app that opens the browser instead.
  Only one kind is installed at a time, and an update keeps the kind.
- The Mac app signed with a Developer ID and notarized by Apple, published on
  GitHub Releases as `brewdash-macos.zip`: a `vX.Y.Z` tag gets its own
  release, any other ref replaces the one `beta` prerelease. Install it with
  `brew trust lemiorhan/brewdash && brew install --cask lemiorhan/brewdash/brewdash`,
  or download it from the latest release.
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
