# syntax=docker/dockerfile:1.7
# Cairn Catalog: Cairn's Iceberg REST catalog (Rust, iceberg-rust). State lives in /data; table
# metadata files go to the object store (Cairn Store).
# Build context: cairn/ (see docker-compose.cairn.yml, service cairn-catalog).
#   docker build -f docker/catalog.Dockerfile -t cairn/catalog:0.1.0 .
# Behind a TLS-intercepting proxy pass its CA as a BuildKit secret (it is never stored in a layer):
#   --secret id=extra_ca,src=/path/to/ca.crt --build-arg HTTPS_PROXY=...
ARG RUST_IMAGE=rust:1-alpine
ARG RUN_IMAGE=alpine:3.20

FROM ${RUST_IMAGE} AS build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY catalog/Cargo.toml catalog/Cargo.lock ./
COPY catalog/src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    if [ -f /run/secrets/extra_ca ]; then export CARGO_HTTP_CAINFO=/run/secrets/extra_ca; fi \
 && cargo build --release --locked \
 && cp target/release/cairn-catalog /cairn-catalog

FROM ${RUN_IMAGE}
RUN addgroup -S -g 10001 cairn && adduser -S -u 10001 -G cairn cairn \
 && mkdir -p /data && chown cairn:cairn /data
COPY --from=build /cairn-catalog /usr/local/bin/cairn-catalog
USER 10001
VOLUME /data
ENV CAIRN_CATALOG_DATA=/data CAIRN_CATALOG_ADDR=:8181
EXPOSE 8181
HEALTHCHECK --interval=10s --timeout=4s --retries=6 CMD ["cairn-catalog", "healthcheck"]
ENTRYPOINT ["cairn-catalog"]
CMD ["serve"]
