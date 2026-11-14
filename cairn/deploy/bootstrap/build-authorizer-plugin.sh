#!/bin/sh
# Builds the Cairn authorizer and lays it out the way Cairn Core expects:
#   $OUT/config.yml
#   $OUT/cairn-authorizer/cairn-authorizer-<ver>.jar
set -eu
SRC="${SRC:-/src}"
OUT="${OUT:-/plugins/auth}"
cp -r "$SRC" /tmp/build && cd /tmp/build
gradle --no-daemon -q jar
mkdir -p "$OUT/cairn-authorizer"
rm -f "$OUT"/cairn-authorizer/*.jar
cp build/libs/cairn-authorizer.jar "$OUT/cairn-authorizer/"
cp config.yml "$OUT/config.yml"
echo "authorizer plugin installed into $OUT"
ls -R "$OUT"
