# Cairn Desktop

Cairn's web workspace as an app for macOS, Windows and Linux (Electron 45): several workspaces, each in its own
browser session; the `cairn` command line inside, signed in with one click; OS notifications; `cairn://` links; a
tray icon with the unread count; and the dashboards and reports a person opened, readable offline. It loads Cairn's
own web UI (`/cairn-ui/app.html`) and adds nothing to it. People's guide: `docs/site/desktop.md`; operators':
`docs/site/administration.md` ("Profiles, the installable app and Cairn Desktop").

## Layout

| File | What it is |
|---|---|
| `src/app.js` | The main process: window, top bar and panel views, workspaces, sign-in, notifications, offline, CLI, menus, tray |
| `src/logic.js` | Rules without Electron: addresses, `cairn://` links, the navigation allowlist, permissions, notification targets |
| `src/profiles.js` | Workspaces are the CLI's profiles: reads and writes its `config.toml` (byte for byte as the CLI does) |
| `src/offline-cache.js` | Encrypted (safeStorage) per-workspace cache of viewed pages, with limits and expiry |
| `src/snapshot.js` | The read-only copy of a page (no scripts, links or handlers), made in an isolated world |
| `src/policy.js` | The computer's policy file, workspace settings and preferences merged strictly; update decisions |
| `src/updater.js` | electron-updater: GitHub's `desktop-latest` release or a generic feed |
| `src/cli.js` | The bundled CLI: "Sign in the command line too", "Install command line tool" |
| `src/menu.js`, `src/tray-model.js`, `src/window-state.js` | Menus, the tray's state, the remembered window |
| `src/preload-workspace.js`, `src/preload-local.js` | The two preloads: workspace pages (notifications only) and the app's own pages |
| `src/ui/` | The top bar and the panel (add a workspace, offline, settings) |
| `build/` | Icon and macOS entitlements (hardened runtime, used once signed) |
| `resources/cli/` | Where the release workflow puts the `cairn` binary before packaging |

## Develop

Node 22, then in this folder:

```
npm ci
npm start                          # workspaces come from ~/.config/cairn/config.toml, or Add workspace
CAIRN_DESKTOP_CLI=/path/to/cairn npm start      # a development run's CLI (a packaged app uses its own)
npm test                           # unit tests, and the real app under xvfb when Electron is installed
npx electron-builder --linux dir   # an unpacked build in dist/ (put the cairn binary in resources/cli/ first)
```

The end-to-end test (`test/e2e/desktop.e2e.js`) is started by `tests/test_desktop_e2e.py`, which runs two real
Cairns, GNOME Keyring in a D-Bus session and a virtual display.

Versions: Electron 45.2.3, electron-builder 26.18.0, electron-updater 6.8.11, pinned exactly.

## Release

`.github/workflows/cairn-desktop-release.yml` builds on macOS (arm64, x64), Windows and Linux, CLI first, smoke-tests
each build, and on a `desktop-v*` tag publishes the release and refreshes `desktop-latest` (the update feed). The
packaged app's fuses keep debuggers (and Playwright) out, so `test/electron.smoke.test.js` reads the app's own
self-test report (`CAIRN_DESKTOP_SELF_TEST=<absolute file>`: tray, notifications received, safeStorage) and checks
what `CAIRN_SMOKE_EXPECT` asks (`tray`, `keychain`); `CAIRN_SMOKE_ARGS` adds app arguments
(`--password-store=gnome-libsecret` for the .deb in a D-Bus session with GNOME Keyring).
Signing turns on when its secrets exist (`docs/site/desktop.md`, "Signing").
