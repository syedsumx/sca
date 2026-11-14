Edge TLS for the Docker Compose edge (the `edge` service in `docker-compose.cairn.yml`). Put the edge's
certificate here as `tls.crt` (PEM, full chain) and its private key as `tls.key` (PEM), or point `CAIRN_TLS_DIR` at
another directory. The edge then serves HTTPS on port 8443 inside its container, published on the host at
`CAIRN_MAPPED_EDGE_TLS_PORT` (default 9443). Set `CAIRN_TLS_REDIRECT=true` to send plain HTTP to HTTPS.

`.gitignore` excludes `*.crt`, `*.key` and `*.pem` in this folder, but never commit these files anyway. Hosted
tenants do not use this folder: their certificates come from cert-manager (see `deploy/hetzner/README.md`).
