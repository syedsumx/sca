# syntax=docker/dockerfile:1.7
# Tiny image that carries the DHE authorizer jar + config.yml. Used as an initContainer on the
# datahub-gms pod (Helm) to populate /etc/datahub/plugins/auth without rebuilding the GMS image.
#   docker build -f docker/authorizer-plugin.Dockerfile -t dhe/enterprise-authorizer:0.1.0 .

FROM gradle:8.14.3-jdk21 AS build
WORKDIR /src
COPY plugins/java/enterprise-authorizer/ ./
RUN gradle --no-daemon -q test jar

FROM busybox:1.37
COPY --from=build /src/build/libs/ /plugin/dhe-enterprise-authorizer/
COPY plugins/java/enterprise-authorizer/config.yml /plugin/config.yml
USER 10001:10001
# `install` copies the plugin into the mounted GMS plugin dir (default /etc/datahub/plugins/auth).
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}\" && cp -r /plugin/dhe-enterprise-authorizer \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/\" && if [ ! -f \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/config.yml\" ]; then cp /plugin/config.yml \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}/\"; fi && ls -R \"${PLUGIN_DIR:-/etc/datahub/plugins/auth}\""]
