# brewdash

A local web dashboard for Homebrew: what is installed, what it is for, what is
outdated, what depends on what. Uninstall, upgrade, pin, start services and
install new packages from the browser.

```sh
python3 brewdash.py            # opens the browser
python3 brewdash.py --port 8765 --no-browser
```

Python 3 standard library only. Cytoscape.js is vendored in `web/vendor/`.

## What it shows

- **Installed by you / Dependencies / Casks**: version, latest version, disk
  size, install date, how many packages use it, description. Filters for
  outdated, orphan, pinned, missing dependency and service.
- **Detail panel**: homepage, tap, caveats,
  "depends on" and "used by" lists, and a dependency graph (1–3 hops).
- **What to do**: for an outdated, pinned, deprecated, disabled or broken
  package, the detail panel says whether brew can still update it, whether a
  newer version or Homebrew's replacement is already installed, what to install
  if not, and whether it is safe to remove. Each suggestion is a button that
  goes through the normal confirm dialog.
- **Services, Taps, Health** (`brew doctor`, `brew cleanup -n`, orphans,
  missing dependencies), **History**, **Install** (`brew search` + preview of
  new dependencies).
- **Apps**: every `.app` in `/Applications` and `~/Applications`, with its
  version and how it got there: brew, the App Store, or by hand. A hand-installed
  app is matched to a cask only when the cask's name is the app's name and
  `brew info --cask` shows that the cask installs exactly that `.app`.
  **Bring into brew** runs `brew install --cask --adopt <cask>`; from then on
  the app upgrades and uninstalls like any cask. App Store apps are only
  listed. An app with no match gets **Search cask**.
- **Shell**: the `PATH` lines and the tools started (`source`, `.`, `eval`) in
  `.zprofile`, `.zshenv`, `.zshrc`, `.profile`, `.bash_profile` and `.bashrc`.
  It flags directories that are missing, listed twice, pinned to one Cellar
  version, or missing under `/usr/local` when Homebrew lives elsewhere, and says what to
  write instead. If brew has a formula or cask for a tool installed some other
  way, it offers to install it. Only those lines are read, and brewdash never
  writes to these files.
- **Export**: a list of what to install on another Mac (see below).

## Selecting several

Tick packages, taps or apps in any tab. The bar above the table shows each
action with the number of selected items it applies to: Upgrade, Pin, Unpin,
Uninstall and Add to list. An action that applies to none of them is
disabled, and its tooltip says why.

## Moving to another Mac

Select what you want to keep and press **Add to list**. The list is stored in
`~/.local/state/brewdash/list.json` and stays between runs. The **Export** tab
groups it into taps, formulae and casks, lets you remove entries, and shows
the Brewfile. Taps that tap-qualified names need are added on their own.
Entries from third-party taps get `trusted: true`, as `brew bundle dump`
writes them. Dependencies do not need to be on the list; brew installs them
anyway.

On the new Mac:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew bundle --file=Brewfile
```

`brew update` is not run on start; use the button. The header shows when the
last update ran.

## Actions

Every action opens a confirmation with the exact command. Uninstall also lists
installed packages that depend on the selection (brew refuses the uninstall
while they are installed) and the dependencies that will become orphans.

One brew command runs at a time; later ones queue. Output streams into the
Jobs panel. A command that needs a `sudo` password fails in the browser and
the panel shows the command to run in Terminal.

Before every uninstall, autoremove and multi-package action, a Brewfile
snapshot is written to `~/.local/state/brewdash/brewfiles/`. Restore with
`brew bundle install --file=<snapshot>`. The action log is
`~/.local/state/brewdash/history.jsonl`.

## Security

The server listens on `127.0.0.1` only. Each start generates a random token
that the browser URL carries; API calls without it, or with a `Host` other
than `127.0.0.1:<port>` / `localhost:<port>`, get 403.

## Tests

```sh
python3 -m unittest discover -s tests
```

`tests/test_ui.py` drives `web/app.js` in headless Chromium against a fake
brew. It needs Playwright for Python and is skipped without it:

```sh
python3 -m venv .venv && .venv/bin/pip install playwright
.venv/bin/playwright install chromium   # only if no matching Chromium is cached
.venv/bin/python -m unittest discover -s tests
```
