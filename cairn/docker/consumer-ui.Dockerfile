# syntax=docker/dockerfile:1.7
# Static host for the Cairn consumer workspace (Module Federation remote) and the embed SDK.
#   /cairn-ui/remoteEntry.js      → loaded by cairn-web's MFE host (mfe.config.yaml)
#   /cairn-ui/<chunk>.<hash>.js   → immutable chunks
#   /cairn-ui/embed/sdk.js        → <cairn-asset-card>/<cairn-quality-badge>/<cairn-lineage> web components
#   /docs/                        → product documentation (docs/site, built by ui/scripts/build-docs.mjs)
# Build context: cairn/ (see docker-compose.cairn.yml, service cairn-consumer-ui).
#   docker build -f docker/consumer-ui.Dockerfile -t cairn/consumer-ui:0.1.0 .
# Behind a TLS-intercepting proxy pass its CA as a BuildKit secret (it is never stored in a layer):
#   --secret id=extra_ca,src=/path/to/ca.crt --build-arg HTTPS_PROXY=...

FROM node:20.18-alpine AS build
WORKDIR /src
COPY ui/package.json ui/package-lock.json ./
ENV NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca NPM_CONFIG_CAFILE=/run/secrets/extra_ca; fi \
 && npm ci \
 && test -x node_modules/.bin/webpack
COPY ui/ ./
COPY docs/site/ /docs-src/
RUN npm run typecheck && npm run build:mfe && npm run build:embed && node scripts/build-docs.mjs /docs-src dist/docs

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY docker/consumer-ui/nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=101:101 /src/dist/mfe/ /usr/share/nginx/html/cairn-ui/
COPY --from=build --chown=101:101 /src/dist/embed/ /usr/share/nginx/html/cairn-ui/embed/
COPY --chown=101:101 ui/brand/ /usr/share/nginx/html/cairn-ui/brand/
COPY --from=build --chown=101:101 /src/dist/docs/ /usr/share/nginx/html/docs/
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD wget -qO- http://127.0.0.1:8080/cairn-ui/remoteEntry.js >/dev/null || exit 1
