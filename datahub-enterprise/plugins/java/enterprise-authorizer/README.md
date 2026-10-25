# DHE Enterprise Authorizer (DataHub GMS plugin)

A DataHub `Authorizer` plugin that sends every GMS authorization decision to the DHE Policy Decision
Point (PDP). The PDP combines ABAC and RBAC rules with deny-overrides.
Class: `io.dhe.authorization.EnterpriseAuthorizer`.

## How it fits into GMS

GMS builds an `AuthorizerChain` in this order: **custom authorizers first, then the native
`DataHubAuthorizer`** (only when `AUTH_POLICIES_ENABLED=true`). The **first ALLOW wins**.

| Mode | GMS setting | Effect |
|---|---|---|
| `additive` | `AUTH_POLICIES_ENABLED=true` | The plugin can only *grant*. A PDP `DENY` or `NOT_APPLICABLE` lets the native policies decide. Use this for gradual rollout. |
| `authoritative` | `AUTH_POLICIES_ENABLED=false` | The plugin is the only authorizer and the PDP is the source of truth. The PDP imports native `dataHubPolicy` entities as its RBAC layer. |

Decision mapping: `ALLOW` → ALLOW. `DENY` and `NOT_APPLICABLE` → DENY. An unknown decision, any HTTP
error, a timeout, a malformed body, or an open circuit → DENY (fail closed). The only exception is a
privilege listed in `failOpenPrivileges`. `urn:li:corpuser:__datahub_system` and `rootUsers`
(break-glass) are always allowed without calling the PDP.

## Wire protocol

`POST {pdpUrl}/governance/v1/authorize` with header `Authorization: Bearer <service token>`:

```json
{
  "actor": "urn:li:corpuser:alice",
  "privilege": "EDIT_ENTITY_TAGS",
  "resource": {"type": "dataset", "urn": "urn:li:dataset:(...)"},
  "sub_resources": [{"type": "schemaField", "urn": "urn:li:schemaField:(...)"}],
  "resource_attributes": {
    "owners": [], "domains": [], "tags": [], "glossary_terms": [],
    "containers": [], "platform_instances": [], "structured_properties": ["urn:li:structuredProperty:x=v"]
  },
  "actor_groups": ["urn:li:corpGroup:finance"],
  "environment": {"channel": "gms", "mode": "additive"}
}
```

Response: `{"decision": "ALLOW|DENY|NOT_APPLICABLE", "reason": "...", "policy_ids": [], "obligations": [], "ttl_seconds": 30}`.

The plugin resolves resource attributes with the GMS `EntitySpecResolver`, which uses the same cached
field resolvers as the native authorizer. Each field is resolved independently, so a failure in one
field does not hide the others.

## Caching and resilience

* TTL cache keyed by `(actor, privilege, resource, sha256(sorted sub-resources))`. The default TTL is
  5 s. The PDP can shorten it with `ttl_seconds`, and `0` means "do not cache".
  The size is bounded by `cacheMaxEntries`.
* Consecutive-failure circuit breaker: after `failureThreshold` failures the circuit opens for
  `openSeconds`. It then lets one half-open probe through, and a successful probe closes it again.
* The plugin has **no runtime dependencies**: it uses `java.net.http`, and JSON is handled in-house by
  `MiniJson`. It therefore cannot clash with the Jackson or Caffeine copies inside GMS, and no shading
  is required.

## Build

```bash
./gradlew test pluginBundle
# → build/libs/dhe-enterprise-authorizer-0.1.0.jar
# → build/distributions/dhe-enterprise-authorizer-0.1.0.zip  (jar + config.yml in GMS layout)
```

The build compiles against `io.acryl:datahub-auth-api:1.5.0`, the newest release whose classes are
Java 17 bytecode, and emits `--release 17` bytecode. The two accessors added in later releases
(`ResolvedEntitySpec#getStructuredPropertyValues`, `AuthorizationRequest#getActorGroupMembership`)
are looked up reflectively. That way one jar runs on GMS 1.5 through 1.8+ (GMS ≥ 1.6 runs on Java 25,
which loads Java 17 bytecode).
To compile against a newer API with a JDK 25 toolchain:
`./gradlew build -PdatahubAuthApiVersion=1.7.0.17 -PjavaToolchain=25`.

## Install

```text
/etc/datahub/plugins/auth/
├── config.yml                                   # see ./config.yml
└── dhe-enterprise-authorizer/
    └── dhe-enterprise-authorizer-0.1.0.jar
```

Set `DHE_SERVICE_TOKEN` (and optionally `DHE_PDP_URL` and `DHE_AUTHZ_MODE`) in the GMS environment,
then restart GMS. The compose file and Helm chart in this repository do this for you; see
`docker-compose.enterprise.yml` (`datahub-gms` service) and `helm/datahub-enterprise/values.yaml`
(`datahub.datahub-gms.extraInitContainers`).
