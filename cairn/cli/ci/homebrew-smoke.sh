#!/usr/bin/env bash
# The Homebrew formula (deploy/homebrew/cairn.rb) installs from a real tap: a local tap made with `brew tap-new`, the
# formula rendered as the release job renders it but pointing at the binary just built (a file:// URL and its
# sha256), then `brew install`, `cairn --version`, `brew test`, the shell completions, `brew uninstall`.
#
#   cli/ci/homebrew-smoke.sh out/cairn-macos-arm64     # from cairn/, on macOS (the release workflow's macOS builds)
set -euo pipefail

src=${1:?usage: homebrew-smoke.sh path/to/cairn-<platform>}
bin="$(cd "$(dirname "$src")" && pwd)/$(basename "$src")"
name=$(basename "$bin")                                 # cairn-macos-arm64: the release asset's name
key=$(printf '%s' "${name#cairn-}" | tr 'a-z-' 'A-Z_')   # MACOS_ARM64
version=$("$bin" --version | awk '{print $NF}')
sha=$(shasum -a 256 "$bin" | cut -d' ' -f1)
zero=0000000000000000000000000000000000000000000000000000000000000000
here=$(cd "$(dirname "$0")" && pwd)
template="$here/../../deploy/homebrew/cairn.rb"
tap=local/cairn

grep -q "@@SHA256_${key}@@" "$template" || { echo "homebrew-smoke: the formula has no $name" >&2; exit 1; }
brew untap "$tap" >/dev/null 2>&1 || true
brew tap-new --no-git "$tap"
cleanup() { brew untap "$tap" >/dev/null 2>&1 || true; }
trap cleanup EXIT
formula="$(brew --repository "$tap")/Formula/cairn.rb"
sed -e "s|https://github.com/@@REPOSITORY@@/releases/download/cli-v@@VERSION@@/$name|file://$bin|" \
    -e "s|@@SHA256_${key}@@|$sha|" -e "s|@@SHA256_[A-Z0-9_]*@@|$zero|g" \
    -e "s|@@VERSION@@|$version|g" -e "s|@@REPOSITORY@@|${GITHUB_REPOSITORY:-cairn/cairn}|g" "$template" > "$formula"
! grep -q "@@" "$formula"
grep -q "file://$bin" "$formula"

brew install --formula "$tap/cairn"
prefix=$(brew --prefix)
"$prefix/bin/cairn" --version | grep -qx "cairn $version"
brew test "$tap/cairn"
# generate_completions_from_executable: bash, zsh and fish.
for f in etc/bash_completion.d/cairn share/zsh/site-functions/_cairn share/fish/vendor_completions.d/cairn.fish; do
  [ -s "$prefix/$f" ] || { echo "homebrew-smoke: no $f" >&2; exit 1; }
done
grep -q _cairn_complete "$prefix/etc/bash_completion.d/cairn"
brew uninstall --formula "$tap/cairn"
[ ! -e "$prefix/bin/cairn" ] || { echo "homebrew-smoke: cairn is still installed" >&2; exit 1; }
echo "homebrew-smoke: installed $name $version from the tap $tap, tested, uninstalled"
