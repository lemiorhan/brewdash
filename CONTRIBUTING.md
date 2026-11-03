# Contributing to brewdash

Thanks for your interest. Bug reports, ideas and pull requests are all
welcome. By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting a bug or asking for a feature

Open an issue with the template that fits. For a bug, include your macOS
version, `brew --version`, the brewdash version, and what you saw. A screenshot helps.
Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Setting up

```sh
git clone https://github.com/lemiorhan/brewdash.git
cd brewdash
python3 brewdash.py                  # run it against your own Homebrew
python3 scripts/demo.py              # or against made-up data
```

For the browser tests and the icons you need Playwright:

```sh
python3 -m venv .venv && .venv/bin/pip install playwright
.venv/bin/playwright install chromium   # only if no matching Chromium is cached
.venv/bin/python -m unittest discover -s tests
```

## Ground rules

- **Standard library only.** brewdash runs with the `python3` that macOS
  ships with (3.9). Do not add Python packages to the runtime, and keep the
  code working on 3.9.
- **Nothing runs without confirmation.** Every action that changes the system
  goes through the confirmation dialog, which shows the exact commands.
- **brewdash never edits your files.** It reads shell files, app bundles and
  secure-it's report; it writes only under `~/.local/state/brewdash/`.
- **Escape everything** that comes from brew, the file system or another tool
  before it goes into the page (`esc()` in `web/app.js`).
- **Tests do not read this Mac's Homebrew, home folder, apps or processes.**
  Use `FakeBrew` in place of `brew`, a fake `sysrun` in place of `lsof` and
  `codesign`, and a temporary home and app folders (`store_of` in
  `tests/test_brewdash.py` sets all of these).

## Pull requests

1. Fork, and branch from `master`.
2. Keep a pull request to one change. Add or update tests for it: a test
   should fail if the line it protects is removed.
3. Run the whole suite, including the browser tests, before you push.
4. If the change is visible, run `scripts/screenshots.py` and include the
   updated screenshots. It opens the Mac app on the demo and needs `swiftc`
   and Screen Recording for the app you run it from.
5. Add a line under *Unreleased* in [CHANGELOG.md](CHANGELOG.md).
6. Fill in the pull request template.

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
