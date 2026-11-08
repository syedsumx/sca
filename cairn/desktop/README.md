# Cairn Desktop

A minimal desktop app for Cairn's workspace (Electron): one window on your Cairn's `/cairn-ui/app.html`, `cairn://`
deep links, and the operating system's notifications. It is kept out of the main build: nothing here is installed
by `cairn/ui` or the Python package.

## What it does

- **Opens your Cairn.** The address comes from `--cairn-url=https://cairn.example.com`, else `CAIRN_URL`, else the
  setup page on first start (saved in the app's user data as `config.json`). https only (http only to this computer).
  People sign in on Cairn's own sign-in page, as in a browser.
- **Stays on Cairn.** Windows only show Cairn's origin; other links open in the default browser (web and mail links
  only). The page gets no Node.js (context isolation, sandbox); the setup page's only bridge saves the address.
- **Deep links.** `cairn://open/<section>[/<rest>][?query]` opens that page of the workspace, e.g.
  `cairn://open/asset/<urn>`, `cairn://open/search?q=revenue`, `cairn://open/profile`. Sections are the workspace's
  (discover, search, asset, sql, dashboards, profile, …); anything else opens nothing. A link given to a second launch
  is handed to the running window (one instance).
- **Notifications.** Only Cairn's origin may ask for the notification permission (every other permission is
  refused); the workspace's notifications and push messages show as the OS's notifications.

## Build

Node 18+, then in this folder:

```
npm install            # downloads Electron and electron-builder (needs access to the npm registry and GitHub)
npm start -- --cairn-url=https://cairn.example.com
npm run dist           # installers in dist/: NSIS (Windows), dmg and zip (macOS), AppImage and deb (Linux)
```

Signing: set electron-builder's usual variables in CI (`CSC_LINK`/`CSC_KEY_PASSWORD` for Windows and macOS,
`APPLE_ID`/`APPLE_APP_SPECIFIC_PASSWORD`/`APPLE_TEAM_ID` for notarisation), from your secret store, never on a
command line. Each installer registers the `cairn:` scheme.

To roll it out with the address set, deploy the installer and set `CAIRN_URL` for users (or a `config.json` with
`{"cairnUrl": "https://cairn.example.com"}` in the app's user-data folder: `%APPDATA%\Cairn`,
`~/Library/Application Support/Cairn`, `~/.config/Cairn`).

## Test

`npm test` runs the main process against a stand-in for Electron (no download), and, once `npm install` has fetched
Electron, a smoke test of the real app (on Linux under `xvfb-run` when there is no display): it opens a Cairn and a
deep link given to a second launch opens in the first window.
