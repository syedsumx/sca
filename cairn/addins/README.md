# Cairn in Excel and Google Sheets

Two add-ins with the same functions, each running in Cairn as the person signed in to it (masks, row filters and
table permissions apply as for them):

| Excel (`excel/`) | Google Sheets (`sheets/`) |
|---|---|
| `=CAIRN.QUERY(sql, [connection])` | `=CAIRN_QUERY(sql, [connection])` |
| `=CAIRN.METRIC(name, [dimensions], [filters])` | `=CAIRN_METRIC(name, [dimensions], [filters])` |
| `=CAIRN.ASK(question)` | `=CAIRN_ASK(question)` |

Sign-in is OAuth 2.0 authorization code with PKCE against Cairn; the clients `agt_excel_addin` and
`agt_sheets_addon` exist once the operator configures them (`CAIRN_EXCEL_ADDIN_ORIGINS`,
`CAIRN_SHEETS_ADDON_SCRIPT_IDS`). The operator manual (`docs/site/administration.md`, "Excel and Google Sheets")
has the whole setup.

## Files

- `excel/manifest.xml`: the add-in-only manifest (task pane, custom functions, shared runtime).
- `excel/src/`: `cairn.js` (the client: PKCE, tokens, one refresh at a time), `functions.js` and `functions.json`
  (the custom functions), `taskpane.*` (sign in, browse metrics, insert), `dialog.*` and `callback.*` (the sign-in
  dialog), `config.js` (written by the build).
- `sheets/`: the Apps Script project: `appsscript.json`, `Code.gs` (menu, sign-in, custom functions, the sidebar's
  server side), `Sidebar.html`.

## Build

No dependencies (Node 18 or later):

```
node build.mjs --cairn-url https://cairn.example.com --addin-url https://addins.example.com
```

writes `dist/excel` (serve it at the add-in URL over https; sideload or deploy `dist/excel/manifest.xml`) and
`dist/sheets` (push with `clasp push`; set the script property `CAIRN_API_URL`). Options: `--api-base` when Cairn's
API is not at `<cairn-url>/cairn/api`, `--id` for your own manifest GUID, `--out`.

## Test

`npm test` runs the unit tests. `tests/test_spreadsheet_addins.py` starts a real Cairn, builds the add-ins for it
and runs the live tests too (`test/*.live.test.mjs`): sign-in through Cairn's consent page, then the functions.
