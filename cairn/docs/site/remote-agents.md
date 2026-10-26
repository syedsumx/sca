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

## Set up a pool

1. An administrator creates a pool and its allowlist: `POST /cairn/api/agents/pools`.
2. Mint a one-time enrolment token: `POST /cairn/api/agents/pools/{pool}/enrollment-tokens`.
3. Install the agent in the private network with that token (Helm chart `cairn-remote-agent`, or
   the `cairn/remote-agent` container).
4. In **Data Sources**, create an ingestion source and set its executor to the pool name. Runs are
   picked up by the agents in that pool; their status and reports appear in Data Sources as usual.
