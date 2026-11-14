#!/usr/bin/env bash
# Installs Ubuntu packages, retrying when the package mirror cannot be reached
# (DNS or network failures on the runner). Usage: apt-install.sh pkg [pkg...]
set -u
for i in 1 2 3 4 5; do
  sudo apt-get update -q >/dev/null || true
  if sudo apt-get install -y -q "$@" >/dev/null; then exit 0; fi
  echo "apt-get install $*: the package mirror failed (attempt $i of 5)"
  [ "$i" = 5 ] && exit 1
  sleep $((i * 60))
done
