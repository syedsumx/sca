#!/bin/sh
# Writes the edge's public servers (included by nginx.conf) before nginx starts.
#   HTTP  :8080  always; redirects to HTTPS when CAIRN_TLS_REDIRECT=true and TLS is on
#   HTTPS :8443  when /etc/cairn/tls/tls.crt and tls.key exist (TLS 1.2+, HSTS)
set -eu
out=/tmp/cairn-edge
tls_dir="${CAIRN_TLS_DIR:-/etc/cairn/tls}"
mkdir -p "$out"
rm -f "$out"/*.conf

common='
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Strict-Transport-Security $cairn_hsts always;

    location = /healthz {
      access_log off;
      return 200 "ok\n";
    }
    include /etc/nginx/cairn/locations.conf;'

tls=false
if [ -f "$tls_dir/tls.crt" ] && [ -f "$tls_dir/tls.key" ]; then
  tls=true
  cat > "$out/https.conf" <<CONF
  server {
    listen 8443 ssl;
    http2 on;
    ssl_certificate     $tls_dir/tls.crt;
    ssl_certificate_key $tls_dir/tls.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:cairn_tls:10m;
    ssl_session_timeout 1d;
$common
  }
CONF
  echo "cairn-edge: HTTPS on 8443 with $tls_dir/tls.crt"
fi

if [ "$tls" = true ] && [ "${CAIRN_TLS_REDIRECT:-false}" = true ]; then
  port="${CAIRN_TLS_PUBLIC_PORT:-443}"
  suffix=""; [ "$port" = 443 ] || suffix=":$port"
  cat > "$out/http.conf" <<CONF
  server {
    listen 8080;
    location = /healthz {
      access_log off;
      return 200 "ok\n";
    }
    location / {
      return 308 https://\$host$suffix\$request_uri;
    }
  }
CONF
else
  cat > "$out/http.conf" <<CONF
  server {
    listen 8080;
$common
  }
CONF
fi
