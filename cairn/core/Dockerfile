# syntax=docker/dockerfile:1.7
# Cairn Core images, one target per service:
#
#   core      metadata service (API, search, graph, authorization, schema registry)
#   web       web application and login
#   setup     one-shot schema / index / seed-data job, run before every upgrade
#   executor  ingestion executor (runs UI-scheduled ingestion recipes)
#
# Built by deploy/build-images.sh. Each target starts from the pinned engine release
# (core/UPSTREAM_VERSION), relabels its user-facing text, bakes in the Cairn defaults and
# authorizer plugin, and takes CAIRN_* settings through /usr/local/bin/cairn-env.
#
# The web target needs core/web/build/web-ui-assets.jar (core/web/build.sh).

ARG UPSTREAM_REGISTRY=acryldata
ARG UPSTREAM_VERSION=v1.8.0
ARG UPSTREAM_JAR_VERSION=1.8.0
ARG PYTHON_IMAGE=python:3.12-slim
ARG PLUGIN_IMAGE=cairn/authorizer:0.1.0
ARG CAIRN_VERSION=0.1.0

FROM ${UPSTREAM_REGISTRY}/datahub-gms:${UPSTREAM_VERSION} AS upstream-core
FROM ${UPSTREAM_REGISTRY}/datahub-frontend-react:${UPSTREAM_VERSION} AS upstream-web
FROM ${UPSTREAM_REGISTRY}/datahub-upgrade:${UPSTREAM_VERSION} AS upstream-setup
FROM ${UPSTREAM_REGISTRY}/datahub-actions:${UPSTREAM_VERSION}-slim AS upstream-executor
FROM ${PLUGIN_IMAGE} AS plugin

# ---------------------------------------------------------------- relabeling
FROM ${PYTHON_IMAGE} AS relabel-core
COPY core/tools/relabel.py /relabel.py
COPY --from=upstream-core /datahub/datahub-gms/bin/war.war /in/war.war
RUN mkdir /out && python3 /relabel.py /in/war.war /out/war.war && rm -rf /in

FROM ${PYTHON_IMAGE} AS relabel-setup
COPY core/tools/relabel.py /relabel.py
COPY --from=upstream-setup /datahub/datahub-upgrade/bin/datahub-upgrade.jar /in/setup.jar
RUN mkdir /out && python3 /relabel.py /in/setup.jar /out/datahub-upgrade.jar && rm -rf /in

FROM ${PYTHON_IMAGE} AS relabel-web
ARG UPSTREAM_JAR_VERSION
COPY core/tools/relabel.py /relabel.py
COPY core/tools/overlay_jar.py /overlay_jar.py
COPY core/web/build/web-ui-assets.jar /in/ui-assets.jar
COPY --from=upstream-web /datahub-frontend/lib/datahub-frontend-${UPSTREAM_JAR_VERSION}.jar /in/web.jar
COPY --from=upstream-web /datahub-frontend/lib/datahub-frontend-${UPSTREAM_JAR_VERSION}-assets.jar /in/web-assets.jar
RUN mkdir /out && python3 /relabel.py /in/web.jar /out/datahub-frontend-${UPSTREAM_JAR_VERSION}.jar \
    && python3 /overlay_jar.py /in/web-assets.jar /in/ui-assets.jar /out/datahub-frontend-${UPSTREAM_JAR_VERSION}-assets.jar \
    && rm -rf /in

# ---------------------------------------------------------------- core
FROM upstream-core AS core
ARG CAIRN_VERSION
LABEL org.opencontainers.image.title="Cairn Core" org.opencontainers.image.vendor="Cairn" \
      org.opencontainers.image.version="${CAIRN_VERSION}"
COPY --from=relabel-core /out/war.war /datahub/datahub-gms/bin/war.war
COPY --from=plugin /plugin/ /etc/datahub/plugins/auth/
COPY core/rootfs/usr/local/bin/cairn-env /usr/local/bin/cairn-env
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/cairn/THIRD_PARTY_NOTICES.md
COPY core/rootfs/etc/cairn/env.d/00-common.map core/rootfs/etc/cairn/env.d/10-core.map /etc/cairn/env.d/
COPY core/rootfs/etc/cairn/init.d/10-db-url.sh core/rootfs/etc/cairn/init.d/15-schema-registry.sh /etc/cairn/init.d/
ENV DATAHUB_TELEMETRY_ENABLED=false \
    ENTITY_REGISTRY_CONFIG_PATH=/datahub/datahub-gms/resources/entity-registry.yml \
    ENTITY_SERVICE_ENABLE_RETENTION=true \
    ENTITY_VERSIONING_ENABLED=true \
    EBEAN_DATASOURCE_DRIVER=com.mysql.jdbc.Driver \
    ELASTICSEARCH_PORT=9200 \
    ELASTICSEARCH_USE_SSL=false \
    ELASTICSEARCH_IMPLEMENTATION=opensearch \
    GRAPH_SERVICE_IMPL=elasticsearch \
    GRAPH_SERVICE_DIFF_MODE_ENABLED=true \
    SCHEMA_REGISTRY_TYPE=INTERNAL \
    KAFKA_SCHEMAREGISTRY_URL=http://localhost:8080/schema-registry/api/ \
    MAE_CONSUMER_ENABLED=true \
    MCE_CONSUMER_ENABLED=true \
    PE_CONSUMER_ENABLED=true \
    MCL_CONSUMER_BATCH_ENABLED=true \
    MCP_CONSUMER_BATCH_ENABLED=true \
    UI_INGESTION_ENABLED=true \
    STRICT_URN_VALIDATION_ENABLED=true \
    THEME_V2_DEFAULT=true \
    ZDU_STAGE_10=true \
    ZDU_STAGE_20=true \
    METADATA_SERVICE_AUTH_ENABLED=true \
    AUTH_POLICIES_ENABLED=true \
    VIEW_AUTHORIZATION_ENABLED=true \
    REACT_APP_TITLE=Cairn \
    REACT_APP_LOGO_URL=/assets/brand/cairn-logo.svg \
    REACT_APP_FAVICON_URL=/assets/icons/favicon.ico \
    JAVA_OPTS="-Xms1g -Xmx1g"
ENTRYPOINT ["/usr/local/bin/cairn-env"]
CMD ["/datahub/datahub-gms/scripts/start.sh"]

# ---------------------------------------------------------------- web
FROM upstream-web AS web
ARG CAIRN_VERSION
ARG UPSTREAM_JAR_VERSION
LABEL org.opencontainers.image.title="Cairn Web" org.opencontainers.image.vendor="Cairn" \
      org.opencontainers.image.version="${CAIRN_VERSION}"
USER root
COPY --from=relabel-web /out/ /datahub-frontend/lib/
COPY core/web/build/web-ui-assets.jar /datahub-frontend/lib/datahub-web-react-${UPSTREAM_JAR_VERSION}-assets.jar
COPY core/rootfs/usr/local/bin/cairn-env /usr/local/bin/cairn-env
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/cairn/THIRD_PARTY_NOTICES.md
COPY core/rootfs/etc/cairn/env.d/00-common.map core/rootfs/etc/cairn/env.d/10-web.map /etc/cairn/env.d/
COPY core/rootfs/etc/cairn/init.d/20-admin-login.sh /etc/cairn/init.d/
COPY deploy/mfe/mfe.config.yaml /etc/cairn/mfe.config.yaml
# No sample accounts: the admin login is written from CAIRN_ADMIN_PASSWORD at start.
RUN : > /datahub-frontend/conf/user.props && chown datahub:datahub /datahub-frontend/conf/user.props \
    && chmod 600 /datahub-frontend/conf/user.props
USER datahub
ENV CAIRN_WEB_USERS_FILE=/datahub-frontend/conf/user.props \
    MFE_CONFIG_FILE_PATH=/etc/cairn/mfe.config.yaml \
    DATAHUB_APP_VERSION=${CAIRN_VERSION} \
    DATAHUB_PLAY_MEM_BUFFER_SIZE=10MB \
    DATAHUB_TRACKING_TOPIC=DataHubUsageEvent_v1 \
    METADATA_SERVICE_AUTH_ENABLED=true \
    THEME_V2_DEFAULT=true \
    JAVA_MEMORY_OPTS="-Xms512m -Xmx512m"
ENTRYPOINT ["/usr/local/bin/cairn-env"]
CMD ["/start.sh"]

# ---------------------------------------------------------------- setup
FROM upstream-setup AS setup
ARG CAIRN_VERSION
LABEL org.opencontainers.image.title="Cairn Setup" org.opencontainers.image.vendor="Cairn" \
      org.opencontainers.image.version="${CAIRN_VERSION}"
COPY --from=relabel-setup /out/datahub-upgrade.jar /datahub/datahub-upgrade/bin/datahub-upgrade.jar
COPY core/rootfs/usr/local/bin/cairn-env /usr/local/bin/cairn-env
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/cairn/THIRD_PARTY_NOTICES.md
COPY core/rootfs/etc/cairn/env.d/00-common.map /etc/cairn/env.d/
COPY core/rootfs/etc/cairn/init.d/10-db-url.sh core/rootfs/etc/cairn/init.d/15-schema-registry.sh /etc/cairn/init.d/
ENV ENTITY_REGISTRY_CONFIG_PATH=/datahub/datahub-gms/resources/entity-registry.yml \
    ENTITY_VERSIONING_ENABLED=true \
    EBEAN_DATASOURCE_DRIVER=com.mysql.jdbc.Driver \
    ELASTICSEARCH_PORT=9200 \
    ELASTICSEARCH_USE_SSL=false \
    ELASTICSEARCH_IMPLEMENTATION=opensearch \
    GRAPH_SERVICE_IMPL=elasticsearch \
    SCHEMA_REGISTRY_TYPE=INTERNAL \
    DATAHUB_PRECREATE_TOPICS=true \
    DATAHUB_SQL_SETUP_ENABLED=true \
    SCHEMA_REGISTRY_SYSTEM_UPDATE=true \
    SPRING_KAFKA_PROPERTIES_AUTO_REGISTER_SCHEMAS=true \
    SPRING_KAFKA_PROPERTIES_USE_LATEST_VERSION=true \
    USE_CONFLUENT_SCHEMA_REGISTRY=false \
    ELASTICSEARCH_BUILD_INDICES_CLONE_INDICES=false \
    ELASTICSEARCH_INDEX_BUILDER_MAPPINGS_REINDEX=true \
    ELASTICSEARCH_INDEX_BUILDER_SETTINGS_REINDEX=true \
    BACKFILL_BROWSE_PATHS_V2=true \
    REPROCESS_DEFAULT_BROWSE_PATHS_V2=false \
    ZDU_STAGE_10=true \
    ZDU_STAGE_20=true
ENTRYPOINT ["/usr/local/bin/cairn-env", "/datahub/datahub-upgrade/scripts/start.sh"]
CMD ["-u", "SystemUpdate"]

# ---------------------------------------------------------------- executor
FROM upstream-executor AS executor
ARG CAIRN_VERSION
LABEL org.opencontainers.image.title="Cairn Executor" org.opencontainers.image.vendor="Cairn" \
      org.opencontainers.image.version="${CAIRN_VERSION}"
COPY core/rootfs/usr/local/bin/cairn-env /usr/local/bin/cairn-env
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/cairn/THIRD_PARTY_NOTICES.md
COPY core/rootfs/etc/cairn/env.d/00-common.map /etc/cairn/env.d/
COPY core/rootfs/etc/cairn/init.d/15-schema-registry.sh /etc/cairn/init.d/
ENV DATAHUB_GMS_PROTOCOL=http \
    DATAHUB_SYSTEM_CLIENT_ID=__datahub_system \
    KAFKA_PROPERTIES_SECURITY_PROTOCOL=PLAINTEXT \
    METADATA_CHANGE_LOG_VERSIONED_TOPIC_NAME=MetadataChangeLog_Versioned_v1
ENTRYPOINT ["/usr/local/bin/cairn-env"]
CMD ["/bin/bash", "/start_datahub_actions.sh"]
