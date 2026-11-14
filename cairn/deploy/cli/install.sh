#!/bin/sh
# Installs the `cairn` command line from a GitHub release: picks the binary for this computer (Linux or macOS,
# x86_64 or arm64), checks it against the release's SHA256SUMS, and puts it in ~/.local/bin (or $CAIRN_INSTALL_DIR).
#
#   curl -fsSL https://raw.githubusercontent.com/syedsumx/sca/main/cairn/deploy/cli/install.sh | sh
#   CAIRN_VERSION=0.2.0 sh install.sh           # a given release (default: the latest cli-v* release)
#
# Settings: CAIRN_VERSION, CAIRN_INSTALL_DIR (~/.local/bin), CAIRN_CLI_REPO (owner/name of the GitHub repository),
# CAIRN_DOWNLOAD_URL (an https mirror holding cairn-<os>-<arch> and SHA256SUMS, instead of the GitHub release).
# Windows: download cairn-windows-x86_64.exe from the release page and check it against SHA256SUMS.
set -eu

REPO="${CAIRN_CLI_REPO:-syedsumx/sca}"
DIR="${CAIRN_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${CAIRN_VERSION:-}"

fail() {
    echo "cairn install: $*" >&2
    exit 1
}

need() {
    command -v "$1" >/dev/null 2>&1 || fail "needs $1"
}

download() {     # url, file
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --https-only -O "$2" "$1"
    else
        fail "needs curl or wget"
    fi
}

case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=macos ;;
    *) fail "no binary for $(uname -s): use pipx install cairn-cli, or the Windows binary from the release page" ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    arm64 | aarch64) arch=arm64 ;;
    *) fail "no binary for $(uname -m)" ;;
esac
asset="cairn-$os-$arch"

if [ -n "${CAIRN_DOWNLOAD_URL:-}" ]; then
    case "$CAIRN_DOWNLOAD_URL" in https://*) ;; *) fail "CAIRN_DOWNLOAD_URL must be https" ;; esac
    VERSION="${VERSION:-from $CAIRN_DOWNLOAD_URL}"
elif [ -z "$VERSION" ]; then
    tmpjson="$(mktemp)"
    download "https://api.github.com/repos/$REPO/releases?per_page=50" "$tmpjson"
    VERSION="$(sed -n 's/.*"tag_name": *"cli-v\([^"]*\)".*/\1/p' "$tmpjson" | head -n 1)"
    rm -f "$tmpjson"
    [ -n "$VERSION" ] || fail "no cli-v* release in $REPO"
fi
base="${CAIRN_DOWNLOAD_URL:-https://github.com/$REPO/releases/download/cli-v$VERSION}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM
echo "Downloading cairn $VERSION ($os, $arch) from $base"
download "$base/$asset" "$work/$asset"
download "$base/SHA256SUMS" "$work/SHA256SUMS"

expected="$(awk -v f="$asset" '$2 == f || $2 == "*" f { print $1 }' "$work/SHA256SUMS")"
[ -n "$expected" ] || fail "SHA256SUMS has no line for $asset"
if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$work/$asset" | awk '{ print $1 }')"
else
    need shasum
    actual="$(shasum -a 256 "$work/$asset" | awk '{ print $1 }')"
fi
[ "$expected" = "$actual" ] || fail "checksum mismatch for $asset (expected $expected, got $actual): not installed"

mkdir -p "$DIR"
chmod 0755 "$work/$asset"
mv "$work/$asset" "$DIR/cairn"
if [ "$os" = macos ]; then
    xattr -d com.apple.quarantine "$DIR/cairn" 2>/dev/null || true
fi
echo "Installed $("$DIR/cairn" --version) to $DIR/cairn"
case ":$PATH:" in
    *":$DIR:"*) ;;
    *) echo "Add $DIR to your PATH, e.g.: echo 'export PATH=\"$DIR:\$PATH\"' >> ~/.profile" ;;
esac
echo "Next: cairn login --url https://<your Cairn>"
