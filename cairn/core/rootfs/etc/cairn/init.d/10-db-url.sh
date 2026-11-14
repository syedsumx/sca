# Builds the JDBC URL from CAIRN_DB_TYPE / CAIRN_DB_HOST / CAIRN_DB_NAME when no explicit URL is given.
# CAIRN_DB_TYPE: mysql (default) or postgres. The driver follows the type (the image bakes the MySQL
# one), unless CAIRN_DB_DRIVER names another.
case "${CAIRN_DB_TYPE:-mysql}" in
  postgres|postgresql)
    export EBEAN_DATASOURCE_DRIVER="${CAIRN_DB_DRIVER:-org.postgresql.Driver}"
    if [ -n "${CAIRN_DB_HOST-}" ] && [ -z "${EBEAN_DATASOURCE_URL-}" ]; then
      export EBEAN_DATASOURCE_URL="jdbc:postgresql://${CAIRN_DB_HOST}/${CAIRN_DB_NAME:-cairn_catalog}?sslmode=${CAIRN_DB_SSLMODE:-prefer}"
    fi
    ;;
  mysql)
    export EBEAN_DATASOURCE_DRIVER="${CAIRN_DB_DRIVER:-com.mysql.cj.jdbc.Driver}"
    if [ -n "${CAIRN_DB_HOST-}" ] && [ -z "${EBEAN_DATASOURCE_URL-}" ]; then
      export EBEAN_DATASOURCE_URL="jdbc:mysql://${CAIRN_DB_HOST}/${CAIRN_DB_NAME:-cairn}?verifyServerCertificate=false&useSSL=true&useUnicode=yes&characterEncoding=UTF-8&rewriteBatchedStatements=true&enabledTLSProtocols=TLSv1.2"
    fi
    ;;
  *)
    echo "cairn: CAIRN_DB_TYPE must be mysql or postgres, not '${CAIRN_DB_TYPE}'" >&2
    exit 1
    ;;
esac
