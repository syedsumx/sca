#!/usr/bin/env bash
# Checks that the chart refuses a set of values, and for the reason given.
# Usage: helm-refuses.sh "<text of the chart's error>" <helm template arguments...>
# Fails when helm renders them, or when it fails for any other reason (a missing value, a broken template):
# `! helm template ...` alone would pass then, and the check would test nothing.
set -u
want="$1"
shift
if out=$("${HELM:-helm}" template "$@" 2>&1 >/dev/null); then
  echo "helm rendered what it should refuse ($want): helm template $*" >&2
  exit 1
fi
if ! grep -qF -- "$want" <<<"$out"; then
  echo "helm refused, but not because of: $want" >&2
  echo "$out" >&2
  exit 1
fi
echo "refused as expected: $want"
