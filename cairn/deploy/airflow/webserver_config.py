# Airflow's web UI signs people in through Cairn (FAB auth manager, OAuth2); Cairn groups map to Airflow
# roles (CAIRN_AIRFLOW_ROLES). See cairn_airflow.auth for the settings it reads.
from cairn_airflow.auth import configure

configure(globals())
