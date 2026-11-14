# Services other than Cairn Core reach its built-in schema registry over the core API address.
if [ -z "${KAFKA_SCHEMAREGISTRY_URL-}" ] && [ -n "${DATAHUB_GMS_HOST-}" ]; then
  export KAFKA_SCHEMAREGISTRY_URL="${DATAHUB_GMS_PROTOCOL:-http}://${DATAHUB_GMS_HOST}:${DATAHUB_GMS_PORT:-8080}/schema-registry/api/"
fi
if [ -z "${SCHEMA_REGISTRY_URL-}" ] && [ -n "${KAFKA_SCHEMAREGISTRY_URL-}" ]; then
  export SCHEMA_REGISTRY_URL="$KAFKA_SCHEMAREGISTRY_URL"
fi
