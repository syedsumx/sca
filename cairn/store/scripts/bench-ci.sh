#!/usr/bin/env bash
# Store throughput at a realistic object count: a release cairn-store (embedded metadata, fsync on) on local disk,
# then `cairn-store bench` with many small objects and with large ones (put, get, list, delete), every byte checked.
# Writes one JSON report per run and a summary (Markdown) into OUT. Usage: bench-ci.sh <cairn-store> <OUT>
# Sizes: SMALL_OBJECTS (50000) x SMALL_SIZE (16KiB) and LARGE_OBJECTS (256) x LARGE_SIZE (8MiB).
set -euo pipefail
BIN=${1:?the cairn-store binary}
OUT=${2:?a directory for the reports}
mkdir -p "$OUT"
DATA=$(mktemp -d)
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
umask 077
# Generated for this run; passed through the environment only, never on a command line.
CAIRN_STORE_ACCESS_KEY="bench-$(openssl rand -hex 6)"
CAIRN_STORE_SECRET_KEY="$(openssl rand -hex 24)"
export CAIRN_STORE_ACCESS_KEY CAIRN_STORE_SECRET_KEY
CAIRN_STORE_DATA="$DATA" CAIRN_STORE_ADDR="127.0.0.1:$PORT" CAIRN_STORE_META="" "$BIN" >"$OUT/store.log" 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; rm -rf "$DATA"' EXIT
for _ in $(seq 1 100); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  sleep 0.1
done
export CAIRN_STORE_ENDPOINT="http://127.0.0.1:$PORT"
run() {   # name objects size concurrency
  echo "== $1: $2 objects of $3, $4 at a time"
  "$BIN" bench --bucket "bench-$1" --objects "$2" --size "$3" --concurrency "$4" >"$OUT/$1.json"
}
run small "${SMALL_OBJECTS:-50000}" "${SMALL_SIZE:-16KiB}" "${SMALL_CONCURRENCY:-32}"
run large "${LARGE_OBJECTS:-256}" "${LARGE_SIZE:-8MiB}" "${LARGE_CONCURRENCY:-8}"
python3 - "$OUT" <<'EOF'
import json, os, sys
out = sys.argv[1]
lines = ["| run | objects | size | op | ops/s | MiB/s | p50 ms | p99 ms | errors |", "|---|---|---|---|---|---|---|---|---|"]
for name in ("small", "large"):
    r = json.load(open(os.path.join(out, f"{name}.json")))
    for p in r["phases"]:
        lines.append(f"| {name} | {r['objects']} | {r['size_bytes']} | {p['op']} | {p['ops_per_second']:.0f} | "
                     f"{p.get('mib_per_second', 0):.1f} | {p['latency_ms']['p50']:.2f} | {p['latency_ms']['p99']:.2f} | "
                     f"{p['errors']} |")
        assert p["errors"] == 0, (name, p)
open(os.path.join(out, "summary.md"), "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
EOF
