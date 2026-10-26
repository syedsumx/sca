#!/bin/sh
# Builds the Aegis authorizer and lays it out the way GMS expects:
#   $OUT/config.yml
#   $OUT/aegis-authorizer/aegis-authorizer-<ver>.jar
set -eu
SRC="${SRC:-/src}"
OUT="${OUT:-/plugins/auth}"
cp -r "$SRC" /tmp/build && cd /tmp/build
gradle --no-daemon -q jar
mkdir -p "$OUT/aegis-authorizer"
rm -f "$OUT"/aegis-authorizer/*.jar
cp build/libs/aegis-authorizer.jar "$OUT/aegis-authorizer/"
cp config.yml "$OUT/config.yml"
echo "authorizer plugin installed into $OUT"
ls -R "$OUT"
