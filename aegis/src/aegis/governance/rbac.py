"""Role-Based Access Control and translation of native DataHub policies.

RBAC roles grant privilege sets, optionally scoped by resource filters, and inherit
from other roles. Bindings attach roles to users or groups. DataHub's own
``dataHubPolicy`` entities are translated into equivalent ABAC ``allow`` policies, so
that in *authoritative* mode (native authorizer disabled) nothing that worked before
stops working, while Aegis deny rules can now override them.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from aegis.governance.abac import Policy, Target

# Built-in DataHub roles (urn:li:dataHubRole:*) mapped to coarse privilege globs. The
# authoritative list lives in GMS bootstrap policies; these keep Aegis usable before the
# native import has run.
BUILTIN_ROLES: dict[str, list[str]] = {
    "urn:li:dataHubRole:Admin": ["*"],
    "urn:li:dataHubRole:Editor": [
        "VIEW_*", "SEARCH_PRIVILEGE", "GET_*", "EDIT_ENTITY*", "EDIT_DATASET_COL_*", "EDIT_LINEAGE",
        "EDIT_DOMAINS_PRIVILEGE", "EDIT_DEPRECATION_PRIVILEGE", "CREATE_TAGS", "MANAGE_GLOSSARIES",
    ],
    "urn:li:dataHubRole:Reader": ["VIEW_ENTITY_PAGE", "VIEW_DATASET_*", "SEARCH_PRIVILEGE", "GET_ENTITY_PRIVILEGE"],
}


class Role(BaseModel):
    urn: str
    privileges: list[str] = Field(default_factory=list)
    inherits: list[str] = Field(default_factory=list)
    resource_types: list[str] = Field(default_factory=list)
    description: str = ""


class RoleBinding(BaseModel):
    role: str
    subjects: list[str] = Field(description="user or group urns")


class RbacModel(BaseModel):
    roles: list[Role] = Field(default_factory=list)
    bindings: list[RoleBinding] = Field(default_factory=list)
    include_builtin_roles: bool = True

    def resolved_roles(self) -> dict[str, Role]:
        roles = {r.urn: r for r in self.roles}
        if self.include_builtin_roles:
            for urn, privs in BUILTIN_ROLES.items():
                roles.setdefault(urn, Role(urn=urn, privileges=privs, description="DataHub built-in"))
        return roles

    def effective_privileges(self, role_urn: str) -> list[str]:
        roles = self.resolved_roles()
        seen: set[str] = set()
        out: list[str] = []
        stack = [role_urn]
        while stack:
            current = stack.pop()
            if current in seen or current not in roles:
                continue
            seen.add(current)
            out.extend(roles[current].privileges)
            stack.extend(roles[current].inherits)
        return sorted(set(out))

    def to_policies(self) -> list[Policy]:
        """Compile RBAC into ABAC allow policies (one per role) for uniform evaluation."""
        roles = self.resolved_roles()
        bound: dict[str, list[str]] = {}
        for b in self.bindings:
            bound.setdefault(b.role, []).extend(b.subjects)
        policies = []
        for urn, role in sorted(roles.items()):
            privs = self.effective_privileges(urn)
            if not privs:
                continue
            subject_clause: list[dict] = [{"attr": "subject.roles", "op": "contains", "value": urn}]
            if bound.get(urn):
                subject_clause += [
                    {"attr": "subject.urn", "op": "in", "value": bound[urn]},
                    {"attr": "subject.groups", "op": "contains_any", "value": bound[urn]},
                ]
            policies.append(Policy(
                id=f"rbac:{urn.split(':')[-1]}",
                description=f"RBAC role {urn}",
                effect="allow",
                actions=privs,
                target=Target(resource_types=role.resource_types),
                condition={"any": subject_clause},
                source="rbac",
            ))
        return policies


# --------------------------------------------------------------------------- native import
_FIELD_MAP = {
    "TYPE": "resource.type",
    "RESOURCE_TYPE": "resource.type",
    "URN": "resource.urn",
    "RESOURCE_URN": "resource.urn",
    "DOMAIN": "resource.domains",
    "TAG": "resource.tags",
    "CONTAINER": "resource.containers",
    "GLOSSARY": "resource.glossary_terms",
    "DATA_PLATFORM_INSTANCE": "resource.platform_instances",
    "STRUCTURED_PROPERTY": "resource.structured_property_urns",
}


def translate_native_policy(policy_urn: str, info: dict) -> Optional[Policy]:
    """Translate a ``dataHubPolicyInfo`` aspect into an ABAC allow policy."""
    if (info.get("state") or "ACTIVE") != "ACTIVE":
        return None
    privileges = info.get("privileges") or []
    if not privileges:
        return None
    actors = info.get("actors") or {}
    actor_any: list[dict] = []
    if actors.get("allUsers") or actors.get("allGroups"):
        actor_any.append({"attr": "subject.urn", "op": "exists"})
    if actors.get("users"):
        actor_any.append({"attr": "subject.urn", "op": "in", "value": actors["users"]})
    if actors.get("groups"):
        actor_any.append({"attr": "subject.groups", "op": "contains_any", "value": actors["groups"]})
    if actors.get("roles"):
        actor_any.append({"attr": "subject.roles", "op": "contains_any", "value": actors["roles"]})
    if actors.get("resourceOwners"):
        owner_types = actors.get("resourceOwnersTypes")
        if owner_types:
            actor_any.append({"all": [
                {"attr": "resource.owners", "op": "contains", "value": {"ref": "subject.urn"}},
                {"attr": "resource.owner_types", "op": "contains_any", "value": owner_types},
            ]})
        else:
            actor_any.append({"attr": "resource.owners", "op": "contains", "value": {"ref": "subject.urn"}})
            # Ownership through a group the subject belongs to.
            actor_any.append({"attr": "resource.owners", "op": "contains_any", "value": {"ref": "subject.groups"}})
    if not actor_any:
        return None

    clauses: list[dict] = [{"any": actor_any}]
    target = Target()
    if info.get("type") == "METADATA":
        resources = info.get("resources") or {}
        if not resources.get("allResources", False):
            if resources.get("type"):
                target.resource_types = [resources["type"]]
            if resources.get("resources"):
                clauses.append({"attr": "resource.urn", "op": "in", "value": resources["resources"]})
            for crit in ((resources.get("filter") or {}).get("criteria") or []):
                clause = _criterion(crit)
                if clause:
                    clauses.append(clause)
    name = policy_urn.split(":")[-1]
    return Policy(
        id=f"native:{name}",
        description=info.get("displayName") or info.get("description") or policy_urn,
        effect="allow",
        actions=privileges,
        target=target,
        condition={"all": clauses} if len(clauses) > 1 else clauses[0],
        source="datahub-native",
    )


def _criterion(crit: dict[str, Any]) -> Optional[dict]:
    path = _FIELD_MAP.get(crit.get("field", ""))
    values = crit.get("values") or []
    if not path or not values:
        return None
    condition = crit.get("condition", "EQUALS")
    if condition == "EQUALS":
        return {"attr": path, "op": "contains_any" if path.endswith("s") else "in", "value": values}
    if condition == "NOT_EQUALS":
        return {"attr": path, "op": "not_in", "value": values}
    if condition == "STARTS_WITH":
        return {"any": [{"attr": path, "op": "starts_with", "value": v} for v in values]}
    return None
