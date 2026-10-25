# syntax=docker/dockerfile:1.7
# Static host for the DHE consumer workspace (Module Federation remote) and the embed SDK.
#   /dhe-ui/remoteEntry.js      → loaded by datahub-frontend's MFE host (mfe.config.yaml)
#   /dhe-ui/<chunk>.<hash>.js   → immutable chunks
#   /dhe-ui/embed/sdk.js        → <dhe-asset-card>/<dhe-quality-badge>/<dhe-lineage> web components
# Build context: datahub-enterprise/ (see docker-compose.enterprise.yml, service dhe-consumer-ui).
#   docker build -f docker/consumer-ui.Dockerfile -t dhe/consumer-ui:0.1.0 .
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
COPY --from=build --chown=101:101 /src/dist/mfe/ /usr/share/nginx/html/dhe-ui/
COPY --from=build --chown=101:101 /src/dist/embed/ /usr/share/nginx/html/dhe-ui/embed/
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD wget -qO- http://localhost:8080/dhe-ui/remoteEntry.js >/dev/null || exit 1
