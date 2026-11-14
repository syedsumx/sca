#!/usr/bin/env bash
# Pulls container images, retrying when the registry fails (a 5xx, a reset connection).
# Usage: docker-pull.sh image [image...]; fails if an image still cannot be pulled.
set -u
for image in "$@"; do
  for i in 1 2 3 4; do
    if docker pull -q "$image"; then break; fi
    echo "docker pull $image: the registry failed (attempt $i of 4)"
    [ "$i" = 4 ] && exit 1
    sleep $((i * 20))
  done
done
