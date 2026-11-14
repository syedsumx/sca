# cairn-cli

The Cairn command line for people: sign in to your Cairn in the browser, then query, search, show and ask as
yourself. Cairn decides what you may see and do, exactly as in the web app.

```
pipx install ./cli                         # from cairn/ in a checkout (or a release binary, Homebrew, install.sh)
cairn login --url https://cairn.example.com
cairn query "SELECT region, count(*) FROM sales.orders GROUP BY region"
cairn search orders --type dataset
cairn show sales.orders
cairn ask "revenue by region last month"
```

Everything about it (install options, profiles, every command, completion, exit codes, CI use) is in the
"Command line" page of Cairn's documentation (`docs/site/command-line.md`).

This folder builds two things from `../src`:

- the `cairn-cli` wheel (`pip wheel --no-deps ./cli`): the client only, no server dependencies;
- single-file binaries with PyInstaller: `pyinstaller --clean --noconfirm cli/cairn.spec` gives `dist/cairn`
  (`dist/cairn.exe` on Windows). `.github/workflows/cairn-cli-release.yml` builds them for Linux (x86_64, arm64),
  macOS (arm64, x86_64) and Windows (x86_64).
