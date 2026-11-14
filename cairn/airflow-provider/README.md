# cairn-airflow-provider

Airflow 3 on the Cairn lakehouse: a `cairn` connection (a pipeline user's Cairn token), `CairnTrinoOperator`,
`CairnDbtOperator`, and sign-in to Airflow's UI through Cairn. Installed in the `cairn/airflow` image; see
"Airflow" in Cairn's administration guide.

Airflow drives Cairn, too: `cairn_airflow.flow_runs.CairnFlowRunOperator` starts a Cairn flow with parameters as the
connection's Cairn identity and waits for it (deferrable, on the triggerer), failing the task when the run fails and
cancelling the run when the task is killed; `CairnFlowRunSensor` waits for a run (by id, or a flow's latest).
