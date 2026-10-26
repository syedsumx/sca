# Builds the JDBC URL from CAIRN_DB_HOST / CAIRN_DB_NAME when no explicit URL is given.
if [ -n "${CAIRN_DB_HOST-}" ] && [ -z "${EBEAN_DATASOURCE_URL-}" ]; then
  export EBEAN_DATASOURCE_URL="jdbc:mysql://${CAIRN_DB_HOST}/${CAIRN_DB_NAME:-cairn}?verifyServerCertificate=false&useSSL=true&useUnicode=yes&characterEncoding=UTF-8&rewriteBatchedStatements=true&enabledTLSProtocols=TLSv1.2"
fi
