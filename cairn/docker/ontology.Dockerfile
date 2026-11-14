# syntax=docker/dockerfile:1.7
# Cairn Ontology: the ontology compiler (Rust; a port of NVIDIA auto-ontology's compiler, see ontology/NOTICE).
# State lives in PostgreSQL (CAIRN_ONTOLOGY_DATABASE_URL) or, for development, a SQLite file in /data.
# Build context: cairn/ (see docker-compose.cairn.yml, service cairn-ontology).
#   docker build -f docker/ontology.Dockerfile -t cairn/ontology:0.1.0 .
# Behind a TLS-intercepting proxy pass its CA as a BuildKit secret (it is never stored in a layer):
#   --secret id=extra_ca,src=/path/to/ca.crt --build-arg HTTPS_PROXY=...
ARG RUST_IMAGE=rust:1-alpine
ARG RUN_IMAGE=alpine:3.20

FROM ${RUST_IMAGE} AS build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY ontology/Cargo.toml ontology/Cargo.lock ./
COPY ontology/src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    if [ -f /run/secrets/extra_ca ]; then export CARGO_HTTP_CAINFO=/run/secrets/extra_ca; fi \
 && cargo build --release --locked \
 && cp target/release/cairn-ontology /cairn-ontology

FROM ${RUN_IMAGE}
RUN addgroup -S -g 10001 cairn && adduser -S -u 10001 -G cairn cairn \
 && mkdir -p /data && chown cairn:cairn /data
COPY --from=build /cairn-ontology /usr/local/bin/cairn-ontology
COPY ontology/NOTICE ontology/LICENSE /usr/share/doc/cairn-ontology/
USER 10001
VOLUME /data
ENV CAIRN_ONTOLOGY_DATABASE_URL=/data/ontology.db CAIRN_ONTOLOGY_ADDR=:8590
EXPOSE 8590
HEALTHCHECK --interval=10s --timeout=4s --retries=6 CMD ["cairn-ontology", "healthcheck"]
ENTRYPOINT ["cairn-ontology"]
CMD ["serve"]
