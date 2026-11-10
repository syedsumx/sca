# syntax=docker/dockerfile:1.7
# Airflow 3 for the Cairn lakehouse: the official Apache Airflow image plus
#   * cairn-airflow-provider: the `cairn` connection, CairnTrinoOperator, CairnDbtOperator and sign-in
#     through Cairn (FAB auth manager),
#   * the FAB and OpenLineage providers (lineage and run history go to Cairn), and pytest (`cairn airflow dev pytest`),
#   * `cairn dbt` with dbt-core and dbt-trino in their own virtualenv (/opt/cairn-dbt), so dbt's
#     dependencies never meet Airflow's.
# Provider versions are pinned by Airflow's own constraints file for this Airflow and Python.
#   docker build -f docker/airflow.Dockerfile -t cairn/airflow:0.1.0 .

ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.12
FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION}
ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.12
ARG DBT_VERSION=1.12.6
ARG DBT_TRINO_VERSION=1.11.0
LABEL org.opencontainers.image.title="cairn-airflow" \
      org.opencontainers.image.description="Airflow on the Cairn lakehouse: tasks as Cairn identities, lineage and sign-in through Cairn"
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
USER root
RUN mkdir -p /opt/cairn-dbt && chown -R airflow: /opt/cairn-dbt
USER airflow
# The sources are bind-mounted for the install only and copied to a scratch directory removed in the same
# step, so they never land in a layer.
RUN --mount=type=secret,id=extra_ca,required=false,uid=50000 \
    --mount=type=bind,target=/mnt/cairn \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca; fi \
 && src="$(mktemp -d)" && cp -r /mnt/cairn/pyproject.toml /mnt/cairn/src /mnt/cairn/airflow-provider /mnt/cairn/spark "${src}/" \
 && constraints="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt" \
 && pip install --constraint "${constraints}" "apache-airflow==${AIRFLOW_VERSION}" \
      apache-airflow-providers-fab apache-airflow-providers-openlineage apache-airflow-providers-cncf-kubernetes \
      apache-airflow-providers-trino apache-airflow-providers-common-sql trino pytest \
 && pip install --no-deps "${src}/airflow-provider" "${src}/spark" \
 && python -m venv /opt/cairn-dbt \
 && /opt/cairn-dbt/bin/pip install "${src}" "dbt-core==${DBT_VERSION}" "dbt-trino==${DBT_TRINO_VERSION}" \
 && rm -rf "${src}"
ENV CAIRN_DBT_BIN=/opt/cairn-dbt/bin/cairn \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    AIRFLOW__CORE__AUTH_MANAGER=airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager \
    AIRFLOW__CORE__LOAD_EXAMPLES=false \
    AIRFLOW__OPENLINEAGE__NAMESPACE=airflow
