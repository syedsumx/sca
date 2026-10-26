# Cairn Authorizer (Cairn Core plugin)

A Cairn Core `Authorizer` plugin that sends every Cairn Core authorization decision to the Cairn Policy Decision
Point (PDP). The PDP combines ABAC and RBAC rules with deny-overrides.
Class: `io.cairn.authorization.CairnAuthorizer`.

## How it fits into Cairn Core

Cairn Core builds an `AuthorizerChain` in this order: **custom authorizers first, then the
native policy authorizer** (only when `CAIRN_NATIVE_POLICIES_ENABLED=true`). The **first ALLOW wins**.

| Mode | Cairn Core setting | Effect |
|---|---|---|
| `additive` | `CAIRN_NATIVE_POLICIES_ENABLED=true` | The plugin can only *grant*. A PDP `DENY` or `NOT_APPLICABLE` lets the native policies decide. Use this for gradual rollout. |
| `authoritative` | `CAIRN_NATIVE_POLICIES_ENABLED=false` | The plugin is the only authorizer and the PDP is the source of truth. The PDP imports native policy entities (`dataHubPolicy`) as its RBAC layer. |

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
  "environment": {"channel": "core", "mode": "additive"}
}
```

Response: `{"decision": "ALLOW|DENY|NOT_APPLICABLE", "reason": "...", "policy_ids": [], "obligations": [], "ttl_seconds": 30}`.

The plugin resolves resource attributes with the Cairn Core `EntitySpecResolver`, which uses the same cached
field resolvers as the native authorizer. Each field is resolved independently, so a failure in one
field does not hide the others.

## Caching and resilience

* TTL cache keyed by `(actor, privilege, resource, sha256(sorted sub-resources))`. The default TTL is
  5 s. The PDP can shorten it with `ttl_seconds`, and `0` means "do not cache".
  The size is bounded by `cacheMaxEntries`.
* Consecutive-failure circuit breaker: after `failureThreshold` failures the circuit opens for
  `openSeconds`. It then lets one half-open probe through, and a successful probe closes it again.
* The plugin has **no runtime dependencies**: it uses `java.net.http`, and JSON is handled in-house by
  `MiniJson`. It therefore cannot clash with the Jackson or Caffeine copies inside Cairn Core, and no shading
  is required.

## Build

```bash
./gradlew test pluginBundle
# → build/libs/cairn-authorizer.jar
# → build/distributions/cairn-authorizer-0.1.0.zip  (jar + config.yml in Cairn Core layout)
```

The build compiles against the Cairn Core authorization API 1.5.0 (coordinates in `build.gradle`), the newest release whose classes are
Java 17 bytecode, and emits `--release 17` bytecode. The two accessors added in later releases
(`ResolvedEntitySpec#getStructuredPropertyValues`, `AuthorizationRequest#getActorGroupMembership`)
are looked up reflectively. That way one jar runs on Cairn Core 1.5 through 1.8+ (Cairn Core ≥ 1.6 runs on Java 25,
which loads Java 17 bytecode).
To compile against a newer API with a JDK 25 toolchain:
`./gradlew build -PcoreAuthApiVersion=1.7.0.17 -PjavaToolchain=25`.

## Install

```text
<core plugin dir>/auth/       # baked into the cairn/core image (core/Dockerfile, target core)
├── config.yml                                   # see ./config.yml
└── cairn-authorizer/
    └── cairn-authorizer.jar   # Cairn Core loads <plugin-name>.jar
```

Set `CAIRN_SERVICE_TOKEN` (and optionally `CAIRN_PDP_URL` and `CAIRN_AUTHZ_MODE`) in the Cairn Core environment,
then restart Cairn Core. The compose file and Helm chart in this repository do this for you; see
`docker-compose.cairn.yml` (`cairn-core` service) and `helm/cairn/values.yaml`
(`catalog.cairn-core.extraInitContainers`).
