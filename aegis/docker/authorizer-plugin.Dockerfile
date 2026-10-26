# syntax=docker/dockerfile:1.7
# Tiny image that carries the Aegis authorizer jar + config.yml. Used as an initContainer on the
# datahub-gms pod (Helm) / one-shot service (compose) to populate /etc/datahub/plugins/auth
# without rebuilding the GMS image.
#
#   # build the jar inside Docker (needs Maven Central from the build):
#   docker build -f docker/authorizer-plugin.Dockerfile -t aegis/authorizer:0.1.0 .
#   # or package a jar built and tested on the host (./gradlew test jar):
#   docker build -f docker/authorizer-plugin.Dockerfile --build-arg JAR_STAGE=prebuilt ...

ARG GRADLE_IMAGE=gradle:8.14.3-jdk21
ARG RUNTIME_IMAGE=busybox:1.37
ARG JAR_STAGE=gradle

FROM ${GRADLE_IMAGE} AS gradle
WORKDIR /src
COPY plugins/java/aegis-authorizer/ ./
RUN gradle --no-daemon -q test jar

FROM scratch AS prebuilt
COPY plugins/java/aegis-authorizer/build/libs/ /src/build/libs/

FROM ${JAR_STAGE} AS jars

FROM ${RUNTIME_IMAGE}
COPY --from=jars /src/build/libs/ /plugin/aegis-authorizer/
COPY plugins/java/aegis-authorizer/config.yml /plugin/config.yml
USER 10001:10001
# Copies the plugin into the mounted GMS plugin dir (default /etc/datahub/plugins/auth).
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}\" && cp -r /plugin/aegis-authorizer \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/\" && if [ ! -f \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/config.yml\" ]; then cp /plugin/config.yml \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/\"; fi && ls -R \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}\""]
