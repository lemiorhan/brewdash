# Releasing the Mac app

[`.github/workflows/macos-release.yml`](.github/workflows/macos-release.yml)
builds the Mac app kind of `brewdash.app` for Apple silicon and Intel Macs,
signs it with the Developer ID Application certificate, has Apple notarize it,
staples the ticket to the app and uploads `brewdash-<tag or commit>-macos.zip`
to the run. The minimum macOS is `min_macos` in
[`macos/build-app.sh`](macos/build-app.sh). The workflow does not publish a
GitHub Release.

`install.sh` still builds the app on the user's Mac and signs it ad-hoc.

## Secrets

Set these under *Settings → Secrets and variables → Actions → Repository
secrets*, or with `gh secret set` from a checkout:

| Secret | What it holds | How to get it |
|---|---|---|
| `APPLE_CERTIFICATE_BASE64` | the Developer ID Application certificate with its private key, as a `.p12`, in Base64 | Keychain Access → *My Certificates* → *Developer ID Application: …* → *Export* as `.p12`, then `base64 -i DeveloperID.p12 \| gh secret set APPLE_CERTIFICATE_BASE64` |
| `APPLE_CERTIFICATE_PASSWORD` | the password you gave the `.p12` | `gh secret set APPLE_CERTIFICATE_PASSWORD` |
| `APPLE_API_KEY_P8` | the App Store Connect API key, the whole `.p8` file with its `BEGIN` and `END` lines | App Store Connect → *Users and Access* → *Integrations* → *Team Keys*, a key with the Developer role or higher; the `.p8` downloads once. `gh secret set APPLE_API_KEY_P8 < AuthKey_XXXXXXXXXX.p8` |
| `APPLE_API_KEY_ID` | that key's ID | the *Key ID* column of the same page |
| `APPLE_API_ISSUER_ID` | the team's issuer ID | *Issuer ID*, above the list of keys |
| `APPLE_TEAM_ID` | the ten-character team ID | developer.apple.com → *Membership*; it is also the part in parentheses in the certificate's name |

Only the steps that need a secret get it: the keychain step the certificate
and the team ID, the signature check the team ID, the notarize step the API key.

No push or pull request starts the workflow. Anyone with write access can
start it by hand, though, on any branch, and it runs that branch's copy of the
workflow file with these secrets. To allow it only on `master` and release
tags, move the secrets into an environment (*Settings → Environments*) whose
deployment rule names those refs, and add `environment: <its name>` to the job.

## Running it

The *Run workflow* button appears once the workflow file is on `master`.

1. *Actions* → **macos-release** → **Run workflow**.
2. Under *Use workflow from*, pick a branch or a tag. A `vX.Y.Z` tag makes the
   app's version `X.Y.Z`; anything else names the app after its commit.
3. When the run is green, the ZIP is under *Artifacts* at the bottom of the run
   page, with its SHA-256 in the run summary. Download it there, in the
   browser.

To start it from a terminal instead: `gh workflow run macos-release.yml --ref
v0.1.0`, then `gh run watch`.

Unzip it and move `brewdash.app` to `/Applications` before you open it.
Opened from Downloads, macOS runs it from a hidden read-only copy, and an
update has nowhere to put the new app.

The app carries the same `version.json` an `install.sh` install does, so it
offers updates the same way: an update runs `install.sh`, which builds the new
version on the Mac, signs it ad-hoc and replaces the app. That this replacing
works on a Developer ID signed, notarized app has not been tried yet; try it
on a test Mac before handing the ZIP out.

## When the first run fails

The workflow's own checks stop with an `Error:` annotation on the run page.
When a tool it runs fails instead, such as `shasum`, `codesign --verify` or
`stapler validate`, the tool's own message is in the log of the step that
failed, above GitHub's `Process completed with exit code`.

| Step | What it says | What to do |
|---|---|---|
| Run the tests | a failing test | Fix it; nothing is signed before the tests pass. |
| Put the Developer ID certificate in a temporary keychain | `the APPLE_… secret is not set` | Check the secret's name against the table above. |
| 〃 | `MAC verification failed during PKCS12 import` or `Unknown format in import` | Wrong password, a damaged Base64 value, or a `.p12` that macOS cannot read: OpenSSL 3 encrypts with AES by default. Export it again from Keychain Access, or with `openssl pkcs12 -export -legacy`. |
| 〃 | `APPLE_CERTIFICATE_BASE64 holds no code signing certificate with its private key` | The `.p12` holds certificates without the private key. In Keychain Access, under *My Certificates*, export the *Developer ID Application* row with the private key under it, on the Mac that holds that key. |
| 〃 | `no single valid Developer ID Application identity of APPLE_TEAM_ID` | The identities found are printed above it, and one that is not valid has the reason after it, such as `CSSMERR_TP_NOT_TRUSTED`. They may be *Apple Development* or *Developer ID Installer* certificates, they may belong to another team than `APPLE_TEAM_ID`, or the certificate may have expired or been revoked. Export the *Developer ID Application* certificate together with its private key. |
| 〃 | `shasum: WARNING: 1 computed checksum did NOT match` | Apple's `DeveloperIDG2CA.cer` changed. Check that the new file chains to Apple Root CA: `openssl x509 -inform DER -in DeveloperIDG2CA.cer -out g2.pem`, then `security find-certificate -a -c "Apple Root CA" -p /System/Library/Keychains/SystemRootCertificates.keychain > roots.pem` and `openssl verify -CAfile roots.pem g2.pem`. If it does, put its `shasum -a 256` in the step. |
| Build and sign brewdash.app | a `swiftc` error | *Show the toolchain* prints the runner's Xcode. On a Mac with the same Xcode, run `BREWDASH_MODE=app BREWDASH_UNIVERSAL=1 bash macos/build-app.sh`. |
| 〃 | `The timestamp service is not available` | Apple's timestamp server did not answer. Run the job again. |
| Check the signature | `there is no … slice` | The app was not built for both architectures. The build step sets `BREWDASH_UNIVERSAL=1`; check that it still does. |
| 〃 | `not signed with the Developer ID Application certificate`, `the team is not APPLE_TEAM_ID`, `the hardened runtime is off`, `no secure timestamp` | The `codesign --display` output above the error shows what the app was signed with. If it looks right and the step still fails, the step's patterns no longer match how `codesign` prints. |
| 〃 | `the signature does not carry macos/brewdash.entitlements`, `the get-task-allow entitlement is set` | The entitlements the app was signed with are printed above the error. Compare them with `macos/brewdash.entitlements`. |
| Notarize | `notarization ended as '-'` with `401` or `Unable to authenticate` above it | Key ID, issuer ID and `.p8` do not belong together, the key was revoked, or the `.p8` lost its `BEGIN`/`END` lines. |
| 〃 | `A required agreement is missing or has expired` | Accept the latest agreement at developer.apple.com → *Account*. |
| 〃 | `notarization ended as 'Invalid'` | The notary log printed below lists each problem under `issues`, with the file it concerns. |
| 〃 | `notarization ended as 'In_Progress'` | Apple did not finish within the step's `--timeout`. The first submissions of a team can take longer. Look at it later with `xcrun notarytool info <submission> --key AuthKey.p8 --key-id … --issuer …` and run the job again. |
| Staple the ticket and pack the ZIP | `stapler staple` fails although notarization said `Accepted` | The ticket can take a few minutes to reach Apple's servers after acceptance. Run the job again. |
| Check the ZIP as a user unpacks it | `Gatekeeper rejects the app` or `does not see a notarized Developer ID app` | The `spctl` output above the error names what Gatekeeper saw. `source=Unnotarized Developer ID` means the ticket is missing. |

Every run removes its temporary keychain and key files, whether it succeeded
or not. The runner is discarded after the job.
