---
title: Remote agents
order: 6
---
# Remote agents

Remote agents ingest metadata from networks Cairn cannot reach, such as a private VPC or an
on-premises data centre.

* **Outbound only.** The agent connects out to the Cairn gateway; nothing connects in.
* **Secrets stay local.** Database passwords and keys are resolved inside the agent's network and
  never sent to Cairn. Recipes with inline secrets are rejected.
* **Signed and encrypted.** Every batch is signed by the agent and end-to-end encrypted, and the
  gateway only accepts the platforms and metadata types allowed for the agent's pool.

The protocol's threat model and what the gateway refuses from an agent are on
[Remote agent security](agent-security.html).

## Set up a pool

Everything below can be done in **Workspace settings → Remote agents** (shown to people with the
Manage Ingestion privilege), or through the API.

1. Create a pool and its allowlist (**New pool**, or `POST /cairn/api/agents/pools`).
2. Mint a one-time enrolment token (**Enrollment token** on the pool, or
   `POST /cairn/api/agents/pools/{pool}/enrollment-tokens`). The dialog shows the token once, with
   the `docker run` and Helm commands to start an agent with it.
3. Install the agent in the private network with that token (Helm chart `cairn-remote-agent`, or
   the `cairn/remote-agent` container).
4. In **Settings → Sources**, add a source and choose the pool under **Runs on**. **Test connection**
   runs on the pool's agents. Runs are picked up by the agents in that pool, and their status,
   numbers and logs appear with the source.

## Secrets on the agent

Recipes refer to secrets by name (`${FINANCE_PG_PASSWORD}`). The agent resolves them itself, so the values never leave your network. List the backends in the agent's `secrets` setting (`agent.yml`); they are tried in order:

```
secrets:
  - {type: env}                                      # FINANCE_PG_PASSWORD from the environment
  - {type: directory, path: /var/run/secrets/cairn}  # one file per secret (Kubernetes, CSI)
  - {type: vault, addr: https://vault:8200, mount: secret, path: cairn, token_file: /vault/token}
  - {type: aws_secrets_manager, prefix: cairn/, region: eu-west-1}
  - {type: gcp_secret_manager, project: acme-data, prefix: cairn-}
  - {type: azure_key_vault, vault_url: https://acme.vault.azure.net, prefix: cairn-}
```

Each cloud backend uses the agent's own identity:

- **Google Cloud Secret Manager** reads `projects/<project>/secrets/<prefix>NAME/versions/latest`. It authenticates with workload identity or the VM's service account through the metadata server, or with `token_file`.
- **Azure Key Vault** reads `<vault_url>/secrets/<prefix>NAME`. Key Vault names allow only letters, digits and dashes, so `_` becomes `-`: `FINANCE_PG_PASSWORD` is read as `FINANCE-PG-PASSWORD`. It authenticates with a managed identity, with workload identity federation (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_FEDERATED_TOKEN_FILE`), or with a client secret.
- **AWS Secrets Manager** uses the AWS SDK's credential chain.

## Watch, follow and cancel runs

The **Runs** tab lists every task with its attempts, the metadata changes it delivered and any the
pool policy refused.

* **Logs** follows a run live. The agent streams its log as it runs, encrypted like everything else;
  every secret value it resolved for the run is replaced with `***` before a line leaves the
  network. API: `GET /cairn/api/agents/tasks/{task_id}/logs?after=N` (poll with the returned `next`).
* **Cancel** stops a run. A queued run never starts; a running one stops at the agent's next
  heartbeat or next batch, whichever comes first, and what it already delivered stays. API:
  `POST /cairn/api/agents/tasks/{task_id}/cancel`.

## When the connection drops

If the gateway cannot be reached in the middle of a run, the agent keeps going and spools the
batches it cannot send to its state directory. They are already encrypted for the gateway, so
nothing readable is written to disk. When the gateway answers again the batches are sent in order
and the run completes normally. The spool is capped at `spool_max_bytes` (512 MiB by default) in
the agent configuration; a run that fills it fails and is retried. If the outage outlasts the
run's lease (5 minutes by default), the agent drops the spool and the gateway hands the task to
another agent in the pool.

