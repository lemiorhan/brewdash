<img src="docs/assets/logo.svg" width="72" alt="">

# brewdash

**A local web dashboard for Homebrew.** See what is installed on your Mac, why
it is there, what is outdated or broken, and what needs your attention. Then
fix it from the browser, with every command shown to you before it runs.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/overview-dark.png">
  <img alt="The brewdash overview: totals, a Needs attention list ordered worst first, and charts" src="docs/screenshots/overview-light.png">
</picture>

## Why

`brew list` tells you *what* is installed. It does not tell you why a package
is there, what still needs it, whether removing it breaks something, which
services listen on your network, or which apps you installed by hand that
brew could manage. brewdash answers those questions on one page, and every
line of its **Needs attention** list opens the page where you can act on it.

## Features

- **Overview**: totals, disk use, outdated packages, running services, and a
  *Needs attention* list ordered worst first. Charts show what is installed,
  how your apps got there, the largest packages, the most depended-on
  packages, and installs per month.
- **Packages** (*Installed by you*, *Dependencies*, *Casks*): version, latest
  version, size on disk, install date, and how many packages use each one.
  Filter by outdated, orphan, pinned, missing dependency or service.
- **Detail panel**: description, homepage, tap, caveats, what a package
  depends on and what depends on it (directly or indirectly), and an
  interactive dependency graph.
- **Advice**: for an outdated, pinned, deprecated, disabled or broken package,
  brewdash says whether brew can still update it, whether a replacement is
  already installed, and whether it is safe to remove. Each suggestion is a
  button.
- **Services**: start, stop and restart `brew services`, and see the ports each
  one listens on. A port open to the network is flagged.
- **Taps**: where your formulae come from, which third-party taps are in use,
  and which are not used by anything. Select taps to untap them.
- **Health**: `brew doctor`, missing dependencies, deprecated packages, orphans
  and what `brew cleanup` would free.
- **Security**: findings from [secure-it](https://github.com/lemiorhan/secure-it),
  shown next to the packages, taps and shell files they concern (see below).
- **Apps**: every app in `/Applications` and `~/Applications`, how it got there
  (brew, App Store or by hand), and who signed it. **Bring into brew** lets
  Homebrew take over an app you installed by hand. Uninstall a brew app, or
  move an app you installed by hand to the Trash.
- **Shell**: the `PATH` lines and tools set up in your shell startup files.
  brewdash flags entries that are missing, duplicated, pinned to one version,
  left over from an Intel Mac, or that put a cloud-synced folder (Dropbox,
  iCloud Drive, `~/Library/CloudStorage`) ahead of the system folders.
- **Install**: search Homebrew and preview what an install would bring with it.
- **Export**: build a Brewfile to set up another Mac.
- **History**: every action, with a Brewfile snapshot before each removal.
- **Settings**: the update channel, automatic updates, and where to find the
  source and report bugs.
- **Light and dark mode**: follows your system, or pick one with the theme
  button.

| | |
|---|---|
| ![Package list with the detail panel of node](docs/screenshots/packages-light.png) | ![secure-it findings in the Security tab](docs/screenshots/security-light.png) |
| ![Services with their listening ports](docs/screenshots/services-dark.png) | ![Apps with how they were installed and who signed them](docs/screenshots/apps-dark.png) |

## Install

brewdash needs macOS, [Homebrew](https://brew.sh) and Python 3.9 or newer. The
`python3` that ships with the Xcode Command Line Tools is enough. It uses the
standard library only; Cytoscape.js is vendored in `web/vendor/`.

```sh
curl -fsSL https://raw.githubusercontent.com/lemiorhan/brewdash/master/install.sh | bash
# or
wget -qO- https://raw.githubusercontent.com/lemiorhan/brewdash/master/install.sh | bash
```

The script builds `brewdash.app` on your Mac with tools macOS already has,
puts it in `/Applications` (or `~/Applications` when you cannot write to
`/Applications`), and opens it. From then on:

- **Open** brewdash from Launchpad, Spotlight or the Dock. It starts the
  server and opens the dashboard in your browser.
- **Click its Dock icon** to open the dashboard again.
- **Quit** it (⌘Q) to stop the server. If the app stops without quitting, the
  server notices and stops as well.

To remove the app, run the install command with `--uninstall`; your settings
and Brewfile snapshots stay in `~/.local/state/brewdash`:

```sh
curl -fsSL https://raw.githubusercontent.com/lemiorhan/brewdash/master/install.sh | bash -s -- --uninstall
```

The top of [`install.sh`](install.sh) lists the settings it reads, such as
`BREWDASH_APP_DIR` for another folder and `BREWDASH_REF` for a tag, branch or
commit other than the latest release.

### Updates

The app updates itself from GitHub. Pick how on the **Settings** page:

- **Stable** installs releases: the highest `vX.Y.Z` tag. This is the default.
- **Beta** installs every commit on `master` as soon as it is pushed.
- **Update by itself** (on by default) checks GitHub a minute after the app
  starts and every hour after that, and installs a newer version when there
  is one. The intervals are `UPDATE_FIRST_CHECK_SECONDS` and
  `UPDATE_CHECK_SECONDS` in `brewdash.py`.
- **Check now** asks GitHub straight away, and **Update to …** installs what
  the channel is at. When you switch from Beta back to Stable, the release can
  be older than what you have; the app never goes back on its own, only when
  you press **Go back to …**.

An update runs the `install.sh` inside the app with the version to install. It
builds the new app, quits brewdash, replaces the app and opens it again. If it
fails, the Settings page shows the last line of its log,
`~/.local/state/brewdash/update.log`. Your choices are kept in
`~/.local/state/brewdash/settings.json`.

An app installed before brewdash could update itself, an app built from
`BREWDASH_TARBALL`, and a copy run from a git checkout do not update
themselves. Run the install command once more to get an app that does.

### From the source

```sh
git clone https://github.com/lemiorhan/brewdash.git
cd brewdash
python3 brewdash.py                            # opens your browser
python3 brewdash.py --port 8765 --no-browser   # fixed port, no browser
bash macos/build-app.sh                        # build brewdash.app into build/
```

To look around without touching your own setup, run the demo. It serves
made-up data and only prints the commands it would run:

```sh
python3 scripts/demo.py
```

brewdash does not run `brew update` on start. Use the **brew update** button;
the header shows when it last ran.

## How it works

brewdash is one Python file that serves a small web page. It reads Homebrew's
own JSON output (`brew info --json=v2 --installed`, `brew outdated`,
`brew services`, `brew tap-info`) and builds the model the page shows. Disk
sizes, `brew doctor`, the app scan, signatures and ports load in the
background after the first view.

### Actions

Every action opens a confirmation dialog with the exact commands it will run.
Uninstall also lists the dependencies that would become orphans. If installed
packages still need what you are removing, brew refuses a plain uninstall, so
the dialog offers two ways instead: **Uninstall them too** adds those packages,
and **Uninstall anyway and break them** runs
`brew uninstall --ignore-dependencies` and names what may stop working.

One brew command runs at a time; later ones queue, and their output streams
into the Jobs panel. When a command needs your `sudo` password, macOS asks for
it in a password dialog (brewdash sets `SUDO_ASKPASS`, which brew passes on to
`sudo`). If you cancel the dialog, the command fails and the panel shows the
command to run in Terminal instead.

**Untap** applies only to taps nothing is installed from; uninstall their
packages first. **Move to Trash** has Finder move an app brew did not install
to the Trash, where you can still get it back until you empty it. It is not
offered for Apple's own apps or for brewdash, and the settings and caches the
app left in `~/Library` stay where they are.

Before every uninstall, autoremove and multi-package action, brewdash writes a
Brewfile snapshot to `~/.local/state/brewdash/brewfiles/`. Restore one with
`brew bundle install --file=<snapshot>`. The action log is
`~/.local/state/brewdash/history.jsonl`.

### Selecting several

Tick packages, taps or apps in any tab. The bar above the table shows each
action with the number of selected items it applies to: Upgrade, Pin, Unpin,
Uninstall and Add to list. An action that applies to none of them is disabled,
and its tooltip says why.

### Moving to another Mac

Select what you want to keep and press **Add to list**. The list is stored in
`~/.local/state/brewdash/list.json` and kept between runs. The **Export** tab
groups it into taps, formulae and casks and shows the Brewfile. Taps that
tap-qualified names need are added on their own. Entries from third-party
taps get `trusted: true`, as `brew bundle dump` writes them. Dependencies do
not need to be on the list; brew installs them anyway.

On the new Mac:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew bundle --file=Brewfile
```

### Apps and signatures

An app installed by hand is matched to a cask only when the cask's name is the
app's name and `brew info --cask` shows that the cask installs exactly that
`.app`. **Bring into brew** runs `brew install --cask --adopt <cask>`; from
then on the app upgrades and uninstalls like any other cask.

The signature column comes from `codesign -dv`: who signed the app, as the app
declares it. brewdash does not verify the signature and does not check
notarization (`spctl --assess` does both, but takes minutes for a full
Applications folder). Apps that are not signed at all, or signed ad-hoc with
no identity behind them, are listed under *Needs attention*. brewdash.app
itself is signed ad-hoc too, because `install.sh` builds it on your Mac; it
shows as *built on this Mac (ad-hoc)* and is left out of that list.

### Listening ports

For each running brew service, brewdash asks `lsof` which TCP and UDP ports
its process listens on. A port bound to every interface (`*`) or to a
non-loopback address can be reached from other machines on your network, so
it is flagged.

### Shell files

brewdash reads only the `PATH`, `source`, `.` and `eval` lines of `.zprofile`,
`.zshenv`, `.zshrc`, `.profile`, `.bash_profile` and `.bashrc`, and it never
writes to them. A cloud-synced folder ahead of `/usr/bin` in `PATH` is
flagged even when it is harmless today: a file that arrives through sync, from
another device or a shared folder, would run in place of the system command
of the same name.

## secure-it integration

[secure-it](https://github.com/lemiorhan/secure-it) is a separate macOS
security scanner that runs as root. After each scan it writes
`~/secure-it/report.json`, and brewdash shows those findings in the
**Security** tab:

- high and critical findings appear as *critical* in *Needs attention*,
  medium as *warning*, low as *info*;
- a finding about a brew service or a Homebrew launchd job appears in that
  package's detail panel;
- a finding about a third-party tap marks the tap in **Taps**;
- PATH and shell findings link to **Shell**.

brewdash only reads that file, and treats it as untrusted: every field is
type-checked before brewdash uses it, and escaped before it is shown. Each
finding shows its fix command and the
`secure-it accept` command for you to copy; brewdash never runs them. Without
secure-it, the Security tab says how to install it, and the rest of brewdash
works as before.

## Security

The server listens on `127.0.0.1` only. Each start generates a random token
that the browser URL carries; API calls without it, or with a `Host` other
than `127.0.0.1:<port>` / `localhost:<port>`, get 403. To report a
vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```sh
python3 -m unittest discover -s tests          # unit tests; the UI tests skip without Playwright
python3 -m venv .venv && .venv/bin/pip install playwright
.venv/bin/playwright install chromium          # only if no matching Chromium is cached
.venv/bin/python -m unittest discover -s tests # unit and browser tests
.venv/bin/python scripts/screenshots.py        # regenerate docs/screenshots from the demo
.venv/bin/python scripts/icons.py              # regenerate the favicons and app icon from web/logo.svg
```

The project page is `docs/index.html`, served by GitHub Pages from `docs/`.

`tests/test_ui.py` drives `web/app.js` in headless Chromium against a fake
brew. No test calls the real `brew`, `lsof` or `codesign`. See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

[Apache License 2.0](LICENSE). Cytoscape.js in `web/vendor/` is © The
Cytoscape Consortium, under the MIT license; see [NOTICE](NOTICE).
