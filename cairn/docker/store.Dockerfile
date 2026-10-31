# syntax=docker/dockerfile:1.7
# Cairn Store: Cairn's S3-compatible object store (single node). Data lives in /data.
# Build context: cairn/ (see docker-compose.cairn.yml, service cairn-store).
#   docker build -f docker/store.Dockerfile -t cairn/store:0.1.0 .
# Behind a TLS-intercepting proxy pass its CA as a BuildKit secret (it is never stored in a layer):
#   --secret id=extra_ca,src=/path/to/ca.crt --build-arg HTTPS_PROXY=...
ARG GO_IMAGE=golang:1.25-alpine
ARG RUN_IMAGE=alpine:3.20

FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY store/go.mod store/go.sum ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi \
 && go mod download
COPY store/ ./
ARG VERSION=0.1.0
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/cairn-store ./cmd/cairn-store

FROM ${RUN_IMAGE}
RUN addgroup -S -g 10001 cairn && adduser -S -u 10001 -G cairn cairn \
 && mkdir -p /data && chown cairn:cairn /data
COPY --from=build /out/cairn-store /usr/local/bin/cairn-store
USER 10001
VOLUME /data
ENV CAIRN_STORE_DATA=/data CAIRN_STORE_ADDR=:8333
EXPOSE 8333
HEALTHCHECK --interval=10s --timeout=4s --retries=6 CMD ["cairn-store", "healthcheck"]
ENTRYPOINT ["cairn-store"]
CMD ["serve"]
