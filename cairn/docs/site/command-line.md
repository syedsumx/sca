---
title: Command line
order: 8.5
---
# Command line

`cairn` is Cairn from a terminal: sign in once in your browser, then query data, search the catalog, look at an
asset and ask questions, as yourself. Cairn decides what you may see and do, exactly as in the web app: your masks,
row filters and refusals are the same.

```bash
cairn login --url https://cairn.example.com
cairn query "SELECT region, count(*) AS orders FROM sales.orders GROUP BY region"
cairn search orders --type dataset
cairn show sales.orders
cairn ask "how many orders per region last month?"
```

## Install

Pick one. All give the same `cairn` command.

| How | Command | Notes |
|---|---|---|
| pipx | `pipx install ./cli` from `cairn/` in a checkout (`pipx install cairn-cli` once published) | Client libraries only; add Parquet output with `pipx inject cairn-cli pyarrow` (or install `cairn-cli[parquet]`) |
| Release binary | download `cairn-<os>-<arch>` from the `cli-v*` GitHub release, check it against `SHA256SUMS` | One file, no Python. Linux x86_64 and arm64, macOS arm64 and x86_64, Windows x86_64. No Parquet output |
| Install script | `curl -fsSL https://raw.githubusercontent.com/syedsumx/sca/main/cairn/deploy/cli/install.sh \| sh` | Linux and macOS: picks the binary, verifies its checksum, installs to `~/.local/bin` (`CAIRN_INSTALL_DIR`, `CAIRN_VERSION`) |
| Homebrew | `brew tap <your-org>/cairn && brew install cairn` | The formula is attached to each release (`cairn.rb`); see [Homebrew tap](#homebrew-tap) |

The full server package (`cairn-platform`) also has every command here, beside the operators' ones; install
`cairn-platform[cli]` there for the OS keychain.

**Unsigned binaries.** Until the release workflow has signing secrets, the binaries are not signed. macOS asks before
running one downloaded with a browser (the install script clears the quarantine flag, and Homebrew's downloads have
none); otherwise run
`xattr -d com.apple.quarantine ./cairn-macos-arm64`. Windows SmartScreen may warn ("More info", "Run anyway").
Always check the checksum first: `sha256sum -c SHA256SUMS --ignore-missing` (Linux), `shasum -a 256 -c SHA256SUMS
--ignore-missing` (macOS), `Get-FileHash cairn-windows-x86_64.exe` (Windows, compare with SHA256SUMS).

## Sign in

```bash
cairn login --url https://cairn.example.com
```

`cairn login` uses the OAuth 2.0 device flow (RFC 8628). It prints an address and a code, and opens your browser
(`--no-browser` does not):

```
To sign in, open https://cairn.example.com/cairn/device and enter the code BCDF-GHJK
```

On that page, signed in to Cairn as usual, check the code and the device (its name says the computer and the
profile), then **Approve**. The terminal finishes by itself. The page never asks for your password; only approve a
code you started yourself.

What the CLI gets acts as you: your access, nothing more. It is short-lived: an access token for an hour (refreshed
by itself) and a refresh token for 30 days unused (both set by your administrator); after 90 days you sign in again.

**Where tokens are kept.** In your operating system's keychain: macOS Keychain, Windows Credential Manager, the
Secret Service (GNOME Keyring, KWallet) on Linux. Without one (a server, a container), they go to
`~/.config/cairn/credentials.json`, readable by you only (0600), and `cairn` says so. If that file can be read by
others, `cairn` refuses to use it until you `chmod 600` it. `CAIRN_CREDENTIAL_STORE=keyring|file` chooses. Tokens are
never printed.

**Signing out.** `cairn logout` signs the device out in Cairn (its tokens stop working at once) and removes them here.
Your profile page lists your **Signed-in devices**, each with **Sign out**, for a laptop you no longer have. An
expired or signed-out device gets exit code 3 and "run `cairn login`".

**CI and scripts.** Give a token on standard input, never on the command line:

```bash
printf '%s\n' "$CAIRN_PAT" | cairn login --url https://cairn.example.com --profile ci --token-stdin
```

or skip profiles altogether with environment variables: `CAIRN_URL` and `CAIRN_TOKEN` (or `CAIRN_TOKEN_FILE`, a file
holding it) win over every profile. `CAIRN_CA_FILE` (or `--ca-file` at login) trusts a private certificate authority.

## Profiles

Several Cairns, one per profile, in `~/.config/cairn/config.toml` (no secrets in it):

```bash
cairn login --profile prod --url https://cairn.example.com
cairn login --profile dev --url https://cairn.dev.example.com
cairn profiles list          # * marks the default
cairn profiles use dev       # the default from now on
cairn --profile prod whoami
cairn profiles remove dev    # signs out and forgets it
```

Which profile a command uses: `--profile`, else `CAIRN_PROFILE`, else the default profile. `CAIRN_URL` with
`CAIRN_TOKEN` win over all of them. A profile's tokens are only ever sent to that profile's Cairn: `CAIRN_URL` alone,
pointing elsewhere, is refused. The configuration directory is `CAIRN_CONFIG_DIR`, else `$XDG_CONFIG_HOME/cairn`,
else `%APPDATA%\cairn` on Windows, else `~/.config/cairn`.

`cairn whoami` shows who you are (user and groups), the profile, the Cairn, and when the access token and the
sign-in expire.

## Commands

Global options work before or after the command: `--profile NAME`, `--output json` (machine-readable output),
`-q`/`--quiet` (no notes on standard error), `--no-color` (also `NO_COLOR=1`). Colour and tables only appear on a
terminal; piped output is plain. A mistyped command gets "did you mean …?".

### query

```bash
cairn query "SELECT id, email, region FROM sales.orders ORDER BY id"
cairn query -f report.sql --format csv --out report.csv
cat report.sql | cairn query --format jsonl
cairn query "SELECT * FROM sales.orders" --limit 100 --format parquet --out orders.parquet
cairn query "SELECT …" --warehouse finance
```

Runs one read-only statement on Trino through Cairn, as you: Cairn checks it first (tables in the catalog, your
`VIEW_DATASET_PROFILE` on each, aggregation and projection policies), then Trino asks Cairn again and applies your
masks and row filters. Results stream page by page, so any size works; `--limit` stops after that many rows.
`--format table|csv|json|jsonl|parquet` (default: a table on a terminal, CSV otherwise; Parquet needs pyarrow),
`--out FILE`. `--warehouse NAME` runs it on a team's warehouse (needs `USE_WAREHOUSE`); `--connection` picks another
Trino connection. **Ctrl-C cancels the query in Trino** before `cairn` exits (code 130). Masked columns are named on
standard error.

A table you may not read: exit code 4 and the reason, e.g. `permission denied: you may not query hr.salaries: reading
its rows needs VIEW_DATASET_PROFILE on urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)`.

### search and show

```bash
cairn search revenue                       # everything you may see that matches
cairn search orders --type dataset --type dashboard --limit 50
cairn show sales.orders                    # or a urn
cairn show urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD) --output json
```

`show` prints the description, owners, glossary terms, tags, domain, the schema (each column's type, terms and
description) and a lineage summary (what it is built from, what is built from it). A name that matches several assets
lists them; give the urn.

### ask

```bash
cairn ask "revenue by region last month"
cairn ask "orders per region" --dataset "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
cairn ask "top customers" --no-run          # the SQL only
```

Ask-your-data: Cairn writes the SQL (with your organisation's approved definitions), prints it, runs it as you and
prints the answer. Needs a model provider configured in Cairn.

### flows, iac, ontology

The other groups, under the same verbs, as the signed-in person:

```bash
cairn flows list
cairn flows run nightly --wait
cairn iac validate cairn.yaml               # here, without Cairn
cairn iac plan cairn.yaml                   # = cairn bundle plan
cairn iac apply cairn.yaml
cairn iac export --format yaml --out exported.yaml   # = cairn export
cairn ontology status
cairn ontology compile --wait
```

`iac plan` and `apply` need `MANAGE_SYSTEM_OPERATIONS` (a bundle's state is Cairn's). In the server package,
`cairn ontology` is the operator's in-process command (it needs Cairn's database); in `cairn-cli` and the binaries it
calls the API.

## Shell completion

```bash
source <(cairn completion bash)                           # ~/.bashrc
source <(cairn completion zsh)                            # ~/.zshrc, after compinit
cairn completion fish > ~/.config/fish/completions/cairn.fish
cairn completion powershell | Out-String | Invoke-Expression   # $PROFILE
```

Commands, options and choices complete, and so do profile names (`--profile`, `profiles use`) and flow names
(`flows run`, asked from Cairn as you, within two seconds). Homebrew installs the bash, zsh and fish scripts itself.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | an error: the request failed, Cairn could not be reached, Cairn refused the input |
| 2 | usage: unknown command or option, missing argument |
| 3 | not signed in, or the sign-in expired or was revoked: run `cairn login` |
| 4 | permission denied: Cairn refused it as you (the message names the privilege and what it is on) |
| 5 | not found: no such asset, profile, warehouse or query |
| 130 | interrupted (Ctrl-C); a running query is cancelled in Cairn first |

`iac plan --detailed-exitcode` keeps `cairn bundle`'s meaning: 2 when there are changes.

## In CI

```yaml
- run: pipx install ./cairn/cli          # or download the binary and check SHA256SUMS
- run: cairn query -f checks/orders.sql --format json --out orders.json
  env:
    CAIRN_URL: https://cairn.example.com
    CAIRN_TOKEN: ${{ secrets.CAIRN_TOKEN }}   # a personal access token or an agent key; never echoed
```

## Release binaries

The workflow `.github/workflows/cairn-cli-release.yml` builds the binaries with PyInstaller on GitHub's runners
(Ubuntu x86_64 and arm64, macOS arm64 and Intel, Windows), checks each one (`cairn --version`, `completion bash`,
`login --help`, `whoami` exiting 3), and on a tag `cli-vX.Y.Z` publishes a release with the binaries, `SHA256SUMS`
and the filled-in Homebrew formula. Build one yourself from `cairn/`:

```bash
python -m venv /tmp/cli && /tmp/cli/bin/pip install ./cli pyinstaller
/tmp/cli/bin/pyinstaller --clean --noconfirm cli/cairn.spec     # → dist/cairn (dist/cairn.exe on Windows)
```

### Signed binaries

Signing turns on when its secrets are set on the repository (nothing else to change):

- **macOS**: `APPLE_CERTIFICATE_P12` (base64 of a "Developer ID Application" certificate and key, .p12),
  `APPLE_CERTIFICATE_PASSWORD`, `APPLE_DEVELOPER_ID` (the identity's name). The binary is signed with the hardened
  runtime. With `APPLE_NOTARY_KEY` (an App Store Connect API key, the .p8 text), `APPLE_NOTARY_KEY_ID` and
  `APPLE_NOTARY_ISSUER` it is also notarized.
- **Windows**: `WINDOWS_CERT` (base64 of a code-signing .pfx) and `WINDOWS_CERT_PASSWORD`; signed with an RFC 3161
  timestamp.

### Homebrew tap

Make a repository `<your-org>/homebrew-cairn` and copy each release's `cairn.rb` to `Formula/cairn.rb` there. People
then run `brew tap <your-org>/cairn` and `brew install cairn`; `brew upgrade cairn` follows your releases.
