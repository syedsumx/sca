# syntax=docker/dockerfile:1.7
# Spark on the Cairn lakehouse: Apache Spark 4 with Apache Iceberg (REST catalog, S3FileIO), the
# OpenLineage Spark listener, and cairn-spark (`cairn_spark.lakehouse_session()`: signed in as a Cairn
# identity, every table decided by Cairn, per-table temporary storage credentials, lineage to Cairn).
#   docker build -f docker/spark.Dockerfile -t cairn/spark:0.1.0 .

ARG SPARK_IMAGE=apache/spark:4.0.1-scala2.13-java17-python3-ubuntu
FROM ${SPARK_IMAGE}
ARG ICEBERG_VERSION=1.10.0
ARG OPENLINEAGE_VERSION=1.55.0
ARG MAVEN=https://repo1.maven.org/maven2
LABEL org.opencontainers.image.title="cairn-spark" \
      org.opencontainers.image.description="Spark on the Cairn lakehouse, governed table by table by Cairn"
USER root
RUN --mount=type=secret,id=extra_ca,required=false \
    --mount=type=bind,target=/mnt/cairn \
    set -eu; \
    curl_opts="-fsSL --retry 5 --retry-delay 3 --retry-all-errors"; \
    if [ -f /run/secrets/extra_ca ]; then curl_opts="$curl_opts --cacert /run/secrets/extra_ca"; \
      export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    for jar in \
      "org/apache/iceberg/iceberg-spark-runtime-4.0_2.13/${ICEBERG_VERSION}/iceberg-spark-runtime-4.0_2.13-${ICEBERG_VERSION}.jar" \
      "org/apache/iceberg/iceberg-aws-bundle/${ICEBERG_VERSION}/iceberg-aws-bundle-${ICEBERG_VERSION}.jar" \
      "io/openlineage/openlineage-spark_2.13/${OPENLINEAGE_VERSION}/openlineage-spark_2.13-${OPENLINEAGE_VERSION}.jar"; do \
      curl $curl_opts -o "/opt/spark/jars/$(basename "$jar")" "${MAVEN}/${jar}"; \
    done; \
    src="$(mktemp -d)"; cp -r /mnt/cairn/spark "$src/"; \
    python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel; \
    python3 -m pip install --no-cache-dir "$src/spark"; \
    python3 -c "import cairn_spark, httpx"; \
    rm -rf "$src"
USER spark
ENV PYTHONPATH="/opt/spark/python:/opt/spark/python/lib/py4j-0.10.9.9-src.zip" \
    PYSPARK_PYTHON=python3
