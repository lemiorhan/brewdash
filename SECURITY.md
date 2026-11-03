# Security policy

brewdash runs Homebrew commands on your Mac, so a flaw in it can matter. Thank
you for reporting one responsibly.

## Reporting a vulnerability

Please do **not** open a public issue. Report it privately through GitHub:
**Security → Report a vulnerability** on
<https://github.com/lemiorhan/brewdash/security/advisories/new>.

Include what you did, what happened, and what you expected, with the brewdash
commit, macOS version and browser if they matter. Once a fix is released, the
advisory is published with credit to you, unless you prefer otherwise.

## Supported versions

Only the latest commit on `master` gets security fixes.

## What is in scope

- Anything that lets a web page, another local user or a crafted file make
  brewdash run a command the person at the keyboard did not confirm.
- Bypasses of the token or `Host` checks on the local API.
- Script injection into the page through data brewdash reads: package
  metadata, app names, shell files, or the brewdash security plugin's
  `report.json`.
- Reading files outside `web/` through the static file server.

## Design notes

- The server binds to `127.0.0.1` only. Every API call needs the random token
  generated at start, and a `Host` of `127.0.0.1:<port>` or `localhost:<port>`.
- Every brew action goes through a confirmation dialog that shows the exact
  commands. Commands from the brewdash security plugin's report are shown for
  you to copy and are never run by brewdash.
- Data from brew, the file system and other tools is escaped with `esc()`
  before it goes into the page.
