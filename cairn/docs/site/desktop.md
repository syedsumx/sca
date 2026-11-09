---
title: Cairn Desktop
order: 8.6
---
# Cairn Desktop

Cairn Desktop is Cairn as an app on your computer, for macOS, Windows and Linux. It shows Cairn's own web
workspace (nothing is different inside), and adds what a browser tab cannot: several workspaces side by side, the
`cairn` command line signed in with one click, your operating system's notifications, `cairn://` links, a tray icon
with your unread count, and the dashboards and reports you opened, readable while Cairn cannot be reached.

## Install

Download the installer for your computer from the project's releases (the `desktop-v…` releases; `SHA256SUMS`
there lets you check a download: `shasum -a 256 <file>` on macOS and Linux, `Get-FileHash <file>` in PowerShell).

| Computer | File | Notes |
|---|---|---|
| macOS, Apple silicon | `Cairn-<version>-mac-arm64.dmg` | open it and drag Cairn to Applications |
| macOS, Intel | `Cairn-<version>-mac-x64.dmg` | |
| Windows 10 and 11 | `Cairn-<version>-win-x64.exe` | an installer for you only (no administrator needed) |
| Linux | `Cairn-<version>-linux-x86_64.AppImage` | `chmod +x` it and run it; it updates itself |
| Debian, Ubuntu | `Cairn-<version>-linux-amd64.deb` | `sudo apt install ./Cairn-…deb`; updated with your package manager |

**This version is not code-signed yet**, so your computer warns you the first time:

- **macOS** says Cairn "cannot be opened because the developer cannot be verified". In Finder, right-click (or
  Control-click) Cairn in Applications, choose **Open**, then **Open** again; macOS remembers that. Or, in Terminal:
  `xattr -dr com.apple.quarantine /Applications/Cairn.app`.
- **Windows** SmartScreen says "Windows protected your PC". Click **More info**, then **Run anyway**.
- **Linux** does not ask.

Your IT team may install it for you instead (see [For IT teams](#for-it-teams)).

## Workspaces

The first time, Cairn Desktop asks for your Cairn's address, such as `https://cairn.example.com`. It checks that
Cairn answers there (`/health`, over https; `http://localhost` is allowed for development) and then shows Cairn's
usual sign-in page: sign in as you do in a browser, with your company's single sign-on.

Add more with **File → Add Workspace…** (or the workspace menu at the top left). Switch between them from that
menu, the **Workspace** menu, the tray icon, or with **Ctrl+1…9** (**⌘1…9** on a Mac). Each workspace keeps its
own sign-in: two workspaces never share cookies or storage, even on the same Cairn.

**The command line sees the same workspaces.** They are `cairn`'s profiles: a workspace added in the app is a
profile in `cairn profiles list`, and a profile made with `cairn login` appears in the app's switcher. Both use the
same `config.toml` ([Command line](command-line.html)). It holds no secrets.

**Sign out** (Workspace → Sign Out of This Workspace…) forgets that workspace's sign-in and deletes what the app
kept of it for offline reading. **Remove This Workspace…** also removes the command line's profile and signs the
command line out of it.

## The command line, from the app

Cairn Desktop carries the `cairn` command line inside.

- **Sign in the command line too** (Workspace menu, or Settings): signs `cairn` in to the workspace as you, with one
  click. The app starts `cairn login` and approves its code with your signed-in session, after checking that the
  code is the one it started. The command line's tokens go to your operating system's keychain (Keychain, Credential
  Manager, the Secret Service); the app never sees or stores them. If Cairn asks for a passkey to approve a device,
  the app shows Cairn's approval page with the code filled in instead. Then `cairn whoami --profile <workspace>`.
- **Install Command Line Tool…** (Help menu, or Settings) puts `cairn` on your PATH, after asking: on macOS and Linux
  a link in `/usr/local/bin` when you may write there, else `~/.local/bin` (a copy for the AppImage, which moves on
  each start: install it again after an update); on Windows, the folder with `cairn.exe` is added to your user PATH.
  An existing `cairn` the app did not install (Homebrew, pip) is left as it is.

## Notifications, links and the tray

- **Notifications.** What Cairn tells you (mentions, access requests and decisions, broken contracts…) appears as
  your computer's notifications while the app runs, and clicking one opens the page it is about, in the right
  workspace. The tray (menu bar) icon shows how many are unread; its menu switches workspace and has **Ask Cairn…**,
  which opens the assistant (also **Ctrl+Shift+A** / **⇧⌘A**).
- **Links.** `cairn://<workspace>/<page>` opens a page of a workspace, for example
  `cairn://prod/dashboards?open=d_123`, `cairn://prod/asset/<urn>` or `cairn://prod/search?q=revenue`.
  `cairn://open/<page>` opens it in the workspace on screen. A link to a workspace you do not have asks you to add
  it. Pages are Cairn's sections (discover, search, asset, dashboards, reports, sql, assistant, profile, …); any
  other link opens nothing.
- **Links to other sites** open in your default browser, never inside the app.
- **Show Cairn from anywhere**: a keyboard shortcut you choose in Settings (off until you turn it on).
- The window remembers its size and place; zoom with **Ctrl/⌘ +**, **−** and **0**; spelling is checked as you type
  (right-click a word for suggestions); drop files on a page that takes files (a volume's upload, for example) to
  upload them. Elsewhere the app says "Drop not supported here".
- Camera and microphone are only for your workspace's pages, and only when you allow them; every other permission
  is refused.

## Offline

When a workspace cannot be reached (no network, a VPN down, Cairn being upgraded), the app says so and tries again
on its own. The **dashboards and reports you opened recently** stay readable: the app keeps the last ones you looked
at (20 by default, for 14 days) as they were on your screen, and shows them read-only, marked "as of" the time you
saw them. Nothing else is kept, and nothing you did not open.

What is kept is encrypted with a key in your operating system's keychain, per workspace, and deleted when you sign
out, remove the workspace, turn it off (Settings → Offline), or when the workspace's administrator turns it off. On
Linux it needs a keychain (GNOME Keyring or KWallet); without one, nothing is kept.

## Updates

The app checks for updates when it starts and every six hours (**Check for Updates…** checks now; Settings turns it
off).

- **Windows** and the **Linux AppImage** download the update in the background and install it when you quit (or
  **Restart and update** in Settings). Each download is checked against the SHA-512 in the release's update file
  before it is installed; while the app is unsigned, that check is what protects the update.
- **macOS** installs updates only into a signed app, so until Cairn Desktop is signed it tells you an update is out
  and opens the download page.
- The **.deb** is updated by your package manager; the app only tells you.

## For IT teams

**Installing for everyone.** The Windows installer installs per user; the AppImage and the .deb need no setup; on
macOS deploy the app from the dmg with your MDM.

**The policy file.** A JSON file on each computer, which people cannot override (where both the file and a
workspace set something, the stricter wins):

| Computer | Path |
|---|---|
| Linux | `/etc/cairn/desktop-policy.json` |
| macOS | `/Library/Application Support/Cairn/desktop-policy.json` |
| Windows | `%ProgramData%\Cairn\desktop-policy.json` |

```json
{
  "workspaces": [{ "name": "prod", "url": "https://cairn.example.com" }],
  "allowed_origins": ["https://cairn.example.com"],
  "updates": "auto",
  "pinned_version": "1.4.2",
  "update_feed_url": "https://updates.example.com/cairn-desktop",
  "offline_cache": true,
  "offline_cache_pages": 20,
  "offline_cache_max_age_days": 14,
  "global_shortcut": true,
  "cli_install": true
}
```

- `workspaces`: added for everyone on first start (also as command-line profiles).
- `allowed_origins`: only these Cairns can be added or opened.
- `updates`: `auto` (download and install), `notify` (only say one is out) or `off` (never check).
- `pinned_version`: never update past this version.
- `update_feed_url`: update from your own server instead of GitHub (see below).
- `offline_cache: false` keeps nothing for offline reading; `offline_cache_pages` and `offline_cache_max_age_days`
  lower the limits.
- `global_shortcut: false` and `cli_install: false` turn those features off.

A policy file that exists but cannot be read or is not valid JSON turns updates and the offline cache off until it
is fixed (the app shows why in Settings).

**Hosting updates yourself.** Copy a release's files (installers, `*.blockmap` and `latest*.yml`) to a web server
and set `update_feed_url` to that folder (https). To hold everyone at one version, point it at that version's own
release, `https://github.com/<repository>/releases/download/desktop-v1.4.2`, or set `pinned_version`.

**Workspace settings.** A workspace's administrators can set the same things for their Cairn through its API
(`PUT /admin/desktop/policy`, see [Administration](administration.html)): turn the offline cache off
or lower its limits, have updates only notified or turned off, pin a version, give an update feed, and name the
identity provider's pages the app may show during sign-in.

## Signing

Releases are built by the `cairn-desktop-release` workflow. Signing is wired in and turns on when these repository
secrets are added (nothing else changes):

| Secret | What it is |
|---|---|
| `APPLE_CERTIFICATE_P12` | base64 of a "Developer ID Application" certificate exported with its key as .p12 |
| `APPLE_CERTIFICATE_PASSWORD` | the .p12's password |
| `APPLE_NOTARY_KEY` | an App Store Connect API key (the .p8 file's text), for notarization |
| `APPLE_NOTARY_KEY_ID` | that key's ID |
| `APPLE_NOTARY_ISSUER` | the issuer ID of the App Store Connect team |
| `WINDOWS_CERT` | base64 of a code-signing certificate with its key, as .pfx |
| `WINDOWS_CERT_PASSWORD` | the .pfx's password |

Signed macOS builds run with the hardened runtime (`desktop/build/entitlements.mac.plist`) and install their own
updates.

## Security

- Pages run sandboxed, isolated from the app, without Node.js; a workspace's pages get only a read-only marker that
  they are in the app, and their notifications.
- A workspace's window shows only that workspace's site, and its sign-in's redirects (to your identity provider);
  links elsewhere open in your browser.
- `cairn://` links are checked before anything opens: a known workspace, one of Cairn's sections, no credentials,
  ports, `..` or control characters.
- Electron's fuses are set: no running as plain Node.js, no `NODE_OPTIONS`, no debugger flags, cookies encrypted,
  the app's archive checked for integrity (macOS and Windows) and loaded only from that archive.
- The app's own pages have a strict Content Security Policy; offline copies are shown in a frame that runs nothing.
