#!/bin/sh
# Builds the enterprise authorizer and lays it out the way GMS expects:
#   $OUT/config.yml
#   $OUT/dhe-enterprise-authorizer/dhe-enterprise-authorizer-<ver>.jar
set -eu
SRC="${SRC:-/src}"
OUT="${OUT:-/plugins/auth}"
cp -r "$SRC" /tmp/build && cd /tmp/build
gradle --no-daemon -q jar
mkdir -p "$OUT/dhe-enterprise-authorizer"
rm -f "$OUT"/dhe-enterprise-authorizer/*.jar
cp build/libs/dhe-enterprise-authorizer.jar "$OUT/dhe-enterprise-authorizer/"
cp config.yml "$OUT/config.yml"
echo "authorizer plugin installed into $OUT"
ls -R "$OUT"
