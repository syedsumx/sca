# syntax=docker/dockerfile:1.7
# Cairn's functions in Trino SQL (ai_search): the plugin jar, compiled inside the Trino image it runs in (its JDK
# and SPI), in a small image whose job is to copy it into Trino's plugin directory (Helm: cairn.trino.functions,
# an init container). Rebuild it with each Trino version (TRINO_IMAGE matches cairn.trino.image).
#
#   docker build -f docker/trino-functions.Dockerfile -t cairn/trino-functions:0.1.0 .

ARG TRINO_IMAGE=trinodb/trino:484
ARG RUNTIME_IMAGE=busybox:1.37

FROM ${TRINO_IMAGE} AS build
COPY plugins/java/cairn-trino-functions/ /tmp/src/
RUN /usr/bin/bash /tmp/src/build.sh /tmp/out

FROM ${RUNTIME_IMAGE}
COPY --from=build /tmp/out/cairn-trino-functions.jar /plugin/cairn-functions/
USER 10001:10001
# Copies the plugin into PLUGIN_DIR (default /target: an emptyDir mounted at /usr/lib/trino/plugin/cairn-functions).
ENTRYPOINT ["/bin/sh", "-c", "cp /plugin/cairn-functions/*.jar \"${PLUGIN_DIR:-/target}/\""]
