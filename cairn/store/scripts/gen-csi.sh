#!/bin/sh
# Regenerates src/csi/csi_v1.rs from proto/csi.proto (needs protoc, or `pip install grpcio-tools`).
set -e
cd "$(dirname "$0")/.."
work=$(mktemp -d)
mkdir -p "$work/src" "$work/proto" "$work/out"
cp proto/csi.proto "$work/proto/"
cat > "$work/Cargo.toml" <<'T'
[package]
name = "csigen"
version = "0.1.0"
edition = "2021"
[build-dependencies]
tonic-prost-build = "0.14"
T
echo 'fn main() {}' > "$work/src/main.rs"
cat > "$work/build.rs" <<'T'
fn main() {
    tonic_prost_build::configure().out_dir(std::env::var("GEN_OUT").unwrap()).compile_protos(&["proto/csi.proto"], &["proto"]).unwrap();
}
T
if ! command -v protoc >/dev/null; then
  inc=$(python3 -c "import grpc_tools,os;print(os.path.join(os.path.dirname(grpc_tools.__file__),'_proto'))")
  printf '#!/bin/sh\nexec python3 -m grpc_tools.protoc -I%s "$@"\n' "$inc" > "$work/protoc"; chmod +x "$work/protoc"; export PROTOC="$work/protoc"
fi
(cd "$work" && GEN_OUT="$work/out" cargo build -q)
{ echo "// Generated from proto/csi.proto (CSI spec v1.10.0, Apache-2.0) by tonic-prost-build 0.14; regenerate with scripts/gen-csi.sh."
  echo "#![allow(clippy::all, missing_docs, dead_code)]"; cat "$work/out/csi.v1.rs"; } > src/csi/csi_v1.rs
rm -rf "$work"
