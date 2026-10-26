# syntax=docker/dockerfile:1.7
# Static host for the Aegis consumer workspace (Module Federation remote) and the embed SDK.
#   /aegis-ui/remoteEntry.js      → loaded by datahub-frontend's MFE host (mfe.config.yaml)
#   /aegis-ui/<chunk>.<hash>.js   → immutable chunks
#   /aegis-ui/embed/sdk.js        → <aegis-asset-card>/<aegis-quality-badge>/<aegis-lineage> web components
# Build context: aegis/ (see docker-compose.aegis.yml, service aegis-consumer-ui).
#   docker build -f docker/consumer-ui.Dockerfile -t aegis/consumer-ui:0.1.0 .
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
RUN npm run typecheck && npm run build:mfe && npm run build:embed

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY docker/consumer-ui/nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=101:101 /src/dist/mfe/ /usr/share/nginx/html/aegis-ui/
COPY --from=build --chown=101:101 /src/dist/embed/ /usr/share/nginx/html/aegis-ui/embed/
COPY --chown=101:101 ui/brand/ /usr/share/nginx/html/aegis-ui/brand/
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD wget -qO- http://localhost:8080/aegis-ui/remoteEntry.js >/dev/null || exit 1
