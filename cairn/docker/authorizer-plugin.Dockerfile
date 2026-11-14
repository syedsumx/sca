# syntax=docker/dockerfile:1.7
# Tiny image that carries the Cairn authorizer jar + config.yml. core/Dockerfile copies /plugin/
# into the cairn/core image; run on its own, it copies the plugin into PLUGIN_DIR.
#
#   # build the jar inside Docker (needs Maven Central from the build):
#   docker build -f docker/authorizer-plugin.Dockerfile -t cairn/authorizer:0.1.0 .
#   # or package a jar built and tested on the host (./gradlew test jar):
#   docker build -f docker/authorizer-plugin.Dockerfile --build-arg JAR_STAGE=prebuilt ...

ARG GRADLE_IMAGE=gradle:8.14.3-jdk21
ARG RUNTIME_IMAGE=busybox:1.37
ARG JAR_STAGE=gradle

FROM ${GRADLE_IMAGE} AS gradle
WORKDIR /src
COPY plugins/java/cairn-authorizer/ ./
RUN gradle --no-daemon -q test jar

FROM scratch AS prebuilt
COPY plugins/java/cairn-authorizer/build/libs/ /src/build/libs/

FROM ${JAR_STAGE} AS jars

FROM ${RUNTIME_IMAGE}
COPY --from=jars /src/build/libs/ /plugin/cairn-authorizer/
COPY plugins/java/cairn-authorizer/config.yml /plugin/config.yml
USER 10001:10001
# Copies the plugin into PLUGIN_DIR (default: the Cairn Core plugin dir).
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}\" && cp -r /plugin/cairn-authorizer \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/\" && if [ ! -f \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/config.yml\" ]; then cp /plugin/config.yml \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/\"; fi && ls -R \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}\""]
