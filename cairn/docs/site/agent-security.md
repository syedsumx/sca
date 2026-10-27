---
title: Remote agent security
order: 6.5
---
# Remote agent security

This is the threat model of the remote-agent protocol: what each part is trusted with, what an
attacker in each position can try, and what stops them. Every attack below has a test that tries it
against the real gateway.

## Parts and trust

| Part | Runs in | Holds | Trusted with |
|---|---|---|---|
| **Agent** | your private network | its own Ed25519 signing key and X25519 encryption key; the source credentials (resolved locally) | reading the sources in its pool's recipes |
| **Gateway** | Cairn | its own key pair; a catalog token | checking every request and every metadata change before it reaches the catalog |
| **Catalog** | Cairn | the metadata | nothing an agent sends directly: agents never hold a catalog token |
| **Administrator** | Cairn UI or API | `MANAGE_INGESTION` | creating pools, enrolment tokens, tasks; revoking agents |

Agents connect out to the gateway only. A compromised agent is the main threat the protocol is
designed for: it holds valid keys, so everything it sends is checked on content, not just identity.

## Assets

* **Source credentials.** They never leave the agent's network. Recipes containing literal secrets
  are refused when enqueued; recipes refer to `${SECRET}` names the agent resolves locally.
* **The catalog's integrity**, especially what drives access decisions: users and groups,
  policies, classification terms, column labels.
* **Metadata in transit**, including column profiles and sample values.
* **The audit trail** of what agents did.

## Threats and mitigations

### Network attacker (between agent and gateway, including a TLS-terminating proxy)

| Attack | Mitigation |
|---|---|
| Read batches, tasks or logs | Sealed boxes: ephemeral X25519 → HKDF-SHA256 → AES-256-GCM, end to end between agent and gateway, so they stay encrypted through any proxy. |
| Modify a request | Ed25519 signature over method, path, timestamp, nonce and SHA-256 of the body. |
| Replay a request | 128-bit nonce, stored for twice the allowed clock skew; timestamps outside ±5 minutes are refused. |
| Replay or reorder a batch | The ciphertext is bound (AEAD associated data) to agent, run and sequence number; the gateway accepts only increasing sequence numbers per run. |
| Move a batch into another run | Same binding: decryption fails. |

### Someone with a leaked enrolment token

Tokens are random, stored only as SHA-256 hashes, single-use (enforced atomically) and expire
after one hour by default. Enrolment records the agent's address, host name and version in the
audit log. **Residual risk:** a token used before its real owner gets a working agent in that pool,
so treat tokens like passwords and revoke unexpected agents
(`POST /cairn/api/agents/<agent id>/revoke`).

### A compromised or malicious agent

| Attack | Mitigation |
|---|---|
| Act as another agent | Requests are verified with the key registered for the agent in the path; a valid signature for another agent's endpoint is refused (403). |
| Write into another agent's run | Runs belong to the agent that leased them (404 otherwise). |
| Change who has access (users, groups, roles, policies, tokens, secrets, settings) | Those entity types are refused from agents, whatever the pool allows. |
| Change curated vocabulary, for example redefine the PII classification terms masking relies on | Glossary terms and nodes, domains, structured properties, data products, forms, views and similar definitions are refused from agents. |
| Remove labels people added, for example a column's PII classification | User-owned `editable*` aspects are refused from agents. |
| Write metadata for systems outside its pool | With `allowed_platforms`, every write must be provably on an allowed platform: from the URN, or for containers and view queries from their platform record or the tables they read (in the same batch or already stored). Anything else is refused, not guessed. |
| Delete metadata | `DELETE` changes are refused. |
| Exhaust memory with large requests | Request bodies over 32 MiB are refused before they are read (`CAIRN_GATEWAY_MAX_BODY_BYTES`); decompressed batches are capped at 64 MiB; log lines at 2,000 characters and 2,000 lines per run. |
| Crash the gateway with malformed payloads | Malformed JSON or fields get 400; nothing reaches the catalog. |
| Hide its address in the audit log | `X-Forwarded-For` counts only from proxies listed in `CAIRN_GATEWAY_TRUSTED_PROXIES`. |
| Claim a run succeeded when batches were lost | A run that reports success with a final sequence number the gateway has not received is marked failed and retried. |

Every refused change is counted on the run and written to the audit log with the reason.

**Residual risk.** Within its allowed platforms and aspects, an agent can write what ingestion
normally writes: schemas, descriptions, tags and terms on assets, ownership, lineage, profiles.
A compromised agent can therefore mislabel or mis-own assets in its own scope. Limit that scope:
one pool per network or system, `allowed_platforms` always set, `allowed_aspects` where a source
needs only a few, and profiling off (`allow_profiling: false`) where sample values are not needed.

### A compromised gateway host

The gateway holds a catalog token and decrypts batches: whoever controls it can read what agents
send and write to the catalog with the gateway's (least-privilege) rights. It never sees source
credentials. Run it as its own service, rotate its token (automatic) and its keys if the host is
compromised: rotating the gateway key requires agents to re-enrol.

## Operating checklist

* Put the gateway behind TLS (the edge or your ingress) even though payloads are already
  encrypted: it protects the signed headers' metadata and the enrolment exchange.
* Set `allowed_platforms` on every pool.
* Keep enrolment tokens short-lived and revoke agents that are no longer used.
* Watch the audit log for `agent.enrolled`, `agent.ip_changed` and `batch.rejected_mcps`.
