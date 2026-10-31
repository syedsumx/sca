# syntax=docker/dockerfile:1.7
# Cairn Store: Cairn's S3-compatible object store (single node, Rust). Data lives in /data.
# Build context: cairn/ (see docker-compose.cairn.yml, service cairn-store).
#   docker build -f docker/store.Dockerfile -t cairn/store:0.1.0 .
# Behind a TLS-intercepting proxy pass its CA as a BuildKit secret (it is never stored in a layer):
#   --secret id=extra_ca,src=/path/to/ca.crt --build-arg HTTPS_PROXY=...
ARG RUST_IMAGE=rust:1-alpine
ARG RUN_IMAGE=alpine:3.20

FROM ${RUST_IMAGE} AS build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY store/Cargo.toml store/Cargo.lock ./
COPY store/src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    if [ -f /run/secrets/extra_ca ]; then export CARGO_HTTP_CAINFO=/run/secrets/extra_ca; fi \
 && cargo build --release --locked \
 && cp target/release/cairn-store /cairn-store

FROM ${RUN_IMAGE}
RUN addgroup -S -g 10001 cairn && adduser -S -u 10001 -G cairn cairn \
 && mkdir -p /data && chown cairn:cairn /data
COPY --from=build /cairn-store /usr/local/bin/cairn-store
USER 10001
VOLUME /data
ENV CAIRN_STORE_DATA=/data CAIRN_STORE_ADDR=:8333
EXPOSE 8333
HEALTHCHECK --interval=10s --timeout=4s --retries=6 CMD ["cairn-store", "healthcheck"]
ENTRYPOINT ["cairn-store"]
CMD ["serve"]
