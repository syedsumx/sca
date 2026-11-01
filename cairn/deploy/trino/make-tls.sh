#!/bin/sh
# Compose only: a self-signed certificate for Trino's HTTPS port, made once into the trinotls volume
# (keystore.p12 for Trino, ca.pem for clients such as Cairn's SQL editor). Bring your own instead by
# pointing CAIRN_TRINO_TLS_DIR at a directory holding keystore.p12 and ca.pem.
set -eu
d=/etc/trino/tls
if [ -s "$d/keystore.p12" ] && [ -s "$d/ca.pem" ]; then
  echo "trino tls: keeping the certificate in $d"
  exit 0
fi
keytool -genkeypair -alias trino -keyalg EC -groupname secp256r1 -validity 825 -dname "CN=cairn-trino" \
  -ext "SAN=dns:cairn-trino,dns:localhost,ip:127.0.0.1" -storetype PKCS12 -keystore "$d/keystore.p12" \
  -storepass "$CAIRN_TRINO_KEYSTORE_PASSWORD"
keytool -exportcert -rfc -alias trino -keystore "$d/keystore.p12" -storepass "$CAIRN_TRINO_KEYSTORE_PASSWORD" -file "$d/ca.pem"
chown 1000:1000 "$d/keystore.p12" "$d/ca.pem"
chmod 0644 "$d/ca.pem"
chmod 0640 "$d/keystore.p12"
echo "trino tls: made a self-signed certificate for cairn-trino"
