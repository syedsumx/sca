Put the edge's certificate here as `tls.crt` (PEM, full chain) and `tls.key` (PEM private key), or
point `CAIRN_TLS_DIR` at another directory. The edge then serves HTTPS on port 8443
(`CAIRN_MAPPED_EDGE_TLS_PORT`, default 9443). Never commit these files.
