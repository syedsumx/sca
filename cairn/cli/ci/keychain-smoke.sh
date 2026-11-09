#!/usr/bin/env bash
# The built `cairn` keeps a sign-in in the operating system's own keychain: macOS Keychain, Windows Credential
# Manager, or the Secret Service on Linux (run it inside a D-Bus session with an unlocked GNOME Keyring). Against a
# stand-in Cairn (standin_cairn.py, here), it signs in with `cairn login --token-stdin`, reads the sign-in back with
# `cairn whoami --output json`, checks that the keychain (not the credentials.json fallback) holds it, finds the item
# with the OS's own tool (security, cmdkey, secret-tool), signs out and checks that the item is gone.
#
#   cli/ci/keychain-smoke.sh dist/cairn          # from cairn/; the release workflow runs it after each build
#
# CAIRN_CREDENTIAL_STORE=keyring makes the CLI fail rather than fall back to the file. The token is random, read
# from a 0600 file, and never on a command line or in the output.
set -euo pipefail

exe=${1:?usage: keychain-smoke.sh path/to/cairn}
here=$(cd "$(dirname "$0")" && pwd)
py=${PYTHON:-python3}
command -v "$py" >/dev/null 2>&1 || py=python

case "$(uname -s)" in
  Darwin) os=macos; want=keyring.backends.macOS.Keyring ;;
  Linux) os=linux; want=keyring.backends.SecretService.Keyring ;;
  MINGW* | MSYS* | CYGWIN*) os=windows; want=keyring.backends.Windows.WinVaultKeyring ;;
  *) echo "keychain-smoke: unknown OS $(uname -s)" >&2; exit 1 ;;
esac

umask 077
work=$(mktemp -d)
# Windows' programs (python.exe, cairn.exe) get C:/... paths, not Git Bash's /tmp/...
[ "$os" = windows ] && work=$(cygpath -m "$work")
pid=""
cleanup() {
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

"$py" -c 'import secrets, sys; sys.stdout.write("pat-keychain-smoke-" + secrets.token_hex(20))' > "$work/token"
"$py" "$here/standin_cairn.py" --token-file "$work/token" --port-file "$work/port" &
pid=$!
for _ in $(seq 1 100); do [ -s "$work/port" ] && break; sleep 0.1; done
[ -s "$work/port" ] || { echo "keychain-smoke: the stand-in Cairn did not start" >&2; exit 1; }
url="http://127.0.0.1:$(cat "$work/port")"
profile="keychain-smoke-$$"

unset CAIRN_URL CAIRN_TOKEN CAIRN_TOKEN_FILE CAIRN_PROFILE CAIRN_CA_FILE PYTHON_KEYRING_BACKEND
export CAIRN_CONFIG_DIR="$work/config" CAIRN_CREDENTIAL_STORE=keyring NO_COLOR=1
# The stand-in is local: no proxy in between.
export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"

# Is the item in the keychain? Asked of the OS's own tool, never printing the secret.
native() {
  case "$os" in
    macos) security find-generic-password -s cairn-cli -a "$profile" >/dev/null 2>&1 ;;
    linux) [ "$(secret-tool lookup service cairn-cli username "$profile" 2>/dev/null | wc -c)" -gt 0 ] ;;
    windows) MSYS2_ARG_CONV_EXCL='*' cmdkey /list:cairn-cli 2>/dev/null | tr -d '\r' | grep -Eq "User: ${profile}[[:space:]]*\$" ;;
  esac
}

"$exe" login --url "$url" --profile "$profile" --token-stdin --output json < "$work/token" > "$work/login.json"
"$exe" whoami --profile "$profile" --output json > "$work/whoami.json"
"$py" - "$work" "$want" <<'EOF'
import json, os, sys
work, want = sys.argv[1], sys.argv[2]
token = open(os.path.join(work, "token")).read()
for name in ("login.json", "whoami.json"):
    text = open(os.path.join(work, name)).read()
    assert token not in text, f"{name} shows the token"
    doc = json.loads(text)
    print(f"{name}: credential_store={doc.get('credential_store')} credential_backend={doc.get('credential_backend')}")
    assert doc["credential_store"] == "keyring", doc
    assert doc["credential_backend"] == want, (doc["credential_backend"], want)
who = json.loads(open(os.path.join(work, "whoami.json")).read())
assert who["user"] == "urn:li:corpuser:keychain-smoke" and who["via"] == "token" and who["source"] == "profile", who
EOF
[ ! -e "$work/config/credentials.json" ] || { echo "keychain-smoke: the tokens went to credentials.json" >&2; exit 1; }
native || { echo "keychain-smoke: the $os keychain has no cairn-cli item for $profile" >&2; exit 1; }
echo "keychain-smoke: the $os keychain holds the sign-in ($want)"

"$exe" logout --profile "$profile"
if native; then echo "keychain-smoke: the item is still in the $os keychain after logout" >&2; exit 1; fi
set +e
"$exe" whoami --profile "$profile" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" = 3 ] || { echo "keychain-smoke: whoami after logout should exit 3, got $rc" >&2; exit 1; }
echo "keychain-smoke: signed out; the keychain item is gone"
