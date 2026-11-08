#!/usr/bin/env bash
# Builds Cairn's Trino functions plugin (ai_search) against the Trino it runs in: run it inside the Trino image,
# whose JDK and SPI are the ones the plugin is loaded by.
#
#   docker run --rm -v "$PWD/plugins/java/cairn-trino-functions:/src:ro" -v "$OUT:/out" \
#     --entrypoint /usr/bin/bash trinodb/trino:484 /src/build.sh /out
#
# Writes <out>/cairn-trino-functions.jar: copy it into a directory under Trino's plugin directory
# (/usr/lib/trino/plugin/cairn-functions/). docker/trino-functions.Dockerfile does this for Helm.
set -euo pipefail
OUT="${1:?usage: build.sh <output directory>}"
SRC="$(cd "$(dirname "$0")" && pwd)"
JAVA_BIN="$(dirname "$(readlink -f "$(command -v javac 2>/dev/null || echo /usr/lib/jvm/*/bin/javac)")")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CP=""
for j in /usr/lib/trino/lib/*.jar; do
  case "$(basename "$j")" in
    io.trino_trino-spi-*|io.airlift_slice-*) CP="${CP:+$CP:}$j" ;;
  esac
done
[ -n "$CP" ] || { echo "the Trino SPI is not in /usr/lib/trino/lib: run this in the Trino image" >&2; exit 1; }
mkdir -p "$WORK/classes" "$OUT"
shopt -s globstar nullglob                     # the Trino image has no find
"$JAVA_BIN/javac" -nowarn -proc:none -cp "$CP" -d "$WORK/classes" "$SRC"/src/main/java/**/*.java
cp -r "$SRC/src/main/resources/." "$WORK/classes/"
(cd "$WORK/classes" && "$JAVA_BIN/jar" --create --file "$OUT/cairn-trino-functions.jar" .)
echo "built $OUT/cairn-trino-functions.jar"
