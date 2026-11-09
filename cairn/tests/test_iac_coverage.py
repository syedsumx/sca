"""The coverage gate of "everything as code": every API collection that can be created (or set) and deleted is a
resource type in the registry (cairn.iac.registry: bundles, Terraform, export), or is on the list below with the
reason it is not. A new feature with a create and a delete API and neither fails here.

Routes are read from the source (every router's decorators, with their prefixes), so features that are switched
off in this test's configuration are checked too. Also checked: the registry's own rules, the Terraform provider's
generated resource table and README being up to date, and the examples being valid against their kinds."""

import ast
import os
import re

import pytest

from cairn.iac import registry

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "cairn")
METHODS = {"get", "post", "put", "patch", "delete"}

# Collections that are not resource types, and why. "runtime state, not config": made by using Cairn (runs,
# answers, credentials handed out, people's own bookmarks); "no API": nothing to declare through Cairn's API;
# others say what manages them instead.
EXCLUDED: dict[str, str] = {
    "/ai/artifacts/{}": "runtime state, not config: what assistants produce in a conversation",
    "/ai/memory/{}": "runtime state, not config: notes an assistant keeps for a person",
    "/ai/scheduled-tasks/{}": "runtime state, not config: a person's own assistant tasks, made in conversation",
    "/collab/bookmarks/{}": "runtime state, not config: a person's bookmarks",
    "/collab/follows/{}": "runtime state, not config: what a person follows",
    "/embed/v1/tokens/{}": "runtime state, not config: short-lived embedding credentials, minted per use",
    "/scim/v2/Groups/{}": "owned by the identity provider: SCIM pushes users and groups; declare access to groups "
                          "with policies, personas and grants instead",
    "/scim/v2/Users/{}": "owned by the identity provider (SCIM)",
    "/sql/comments/{}": "runtime state, not config: people's comments",
    "/sql/dashboards/{}/actions/{}": "part of a dashboard's design, edited with it in the UI; a dashboard's tiles and "
                                     "controls are in cairn_dashboard",
    "/sql/folders/{}/items/{}/{}": "runtime state, not config: where a person files an item in their folders",
    "/sql/semantic-models/{}": "runtime state, not config: drafts the semantic-model autopilot proposes (accepted "
                               "ones become metrics, cairn_metric)",
    "/storage/v1/buckets/{}/quota": "Cairn Store's own administration (cairn store CLI / the store's Helm values), "
                                    "answered only where the store is deployed with an admin key",
    "/storage/v1/keys/{}": "credentials: Cairn Store access keys are minted by the store and their secret shown "
                           "once; service identities are cairn_agent_identity and workload bindings",
    "/v1/agent-apps/{}": "a release, not a declaration: each deploy uploads a code bundle and makes a new version "
                         "(cairn agent deploy, from CI); its identity and access are an agent identity and policies",
    "/v1/agent-memory/{}": "runtime state, not config: what agents remember about the people they serve",
    "/v1/me/agent-memories/{}": "runtime state, not config: a person's own agent memories",
    "/v1/app-installs/{}": "runtime state, not config: installing a published app package (a version) into a "
                           "workspace, upgraded in place by its publisher's releases",
    "/v1/databases/{}": "runtime state, not config: a database provisioned on demand, with credentials handed out "
                        "to its owner; declare who may create them with policies",
    "/v1/ml/finetunes/{}": "runtime state, not config: a fine-tuning run",
    "/v1/shares/{}/agents/{}": "part of a share, an act between two parties (a credential handed out once, revoked "
                               "with POST /v1/shares/{id}/revoke), not configuration kept in git",
    "/v1/tables/{}/refs/{}": "runtime state, not config: branches and tags of a table's data, like git refs",
    "/v1/volumes/{}/files/{}": "data, not config: the files in a volume",
    "/v1/ml/feature-tables/{}/online": "set up with the feature table's online store, a run: sync on demand",
    "/v1/ml/models/{}/monitor": "set up from the model's registry page with its baseline data; runs on MLflow",
    "/admin/licence": "deployment, not workspace config: the licence is a Kubernetes Secret (Helm values)",
}


def const_str(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = const_str(node.left, consts), const_str(node.right, consts)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            inner = v.value if isinstance(v, ast.Constant) else const_str(v.value, consts)
            if inner is None:
                return None
            out += inner
        return out
    return None


def visit(body, scope, consts, path, out, unresolved):
    """Routes in ``body``: decorators of APIRouters (``scope``: router variable -> prefix), nested functions too."""
    for n in body:
        for sub in [n] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ast.walk(n):
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call) and \
                    isinstance(sub.value.func, ast.Name) and sub.value.func.id == "APIRouter":
                pre = next((const_str(kw.value, consts) for kw in sub.value.keywords if kw.arg == "prefix"), "")
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        scope[t.id] = pre
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in METHODS \
                        and isinstance(d.func.value, ast.Name) and d.args:
                    sub = const_str(d.args[0], consts)
                    if scope.get(d.func.value.id) is None or sub is None:
                        unresolved.append(f"{path}:{d.lineno}")
                        continue
                    out.add((d.func.attr.upper(), scope[d.func.value.id] + sub))
            visit(n.body, dict(scope), consts, path, out, unresolved)
        elif isinstance(n, ast.ClassDef):
            visit(n.body, dict(scope), consts, path, out, unresolved)
        elif isinstance(getattr(n, "body", None), list):
            visit(n.body, scope, consts, path, out, unresolved)
            visit(getattr(n, "orelse", []) or [], scope, consts, path, out, unresolved)


def routes() -> set[tuple[str, str]]:
    """Every (METHOD, path) the routers declare, with their prefixes, from the source."""
    out: set[tuple[str, str]] = set()
    unresolved = []
    for dp, _, files in os.walk(SRC):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dp, fn)
            tree = ast.parse(open(path).read())
            consts = {}
            for n in tree.body:
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    v = const_str(n.value, consts)
                    if v is not None:
                        consts[n.targets[0].id] = v

            visit(tree.body, {"app": ""}, consts, path, out, unresolved)
    assert not unresolved, f"routes the gate cannot read (give their paths as constants): {unresolved}"
    return out


def norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.split("?")[0])


def configurable(rs: set[tuple[str, str]]) -> set[str]:
    """Paths that can be deleted and were created (POST to the collection) or set (PUT/PATCH on themselves)."""
    ops: dict[str, set[str]] = {}
    for m, p in rs:
        ops.setdefault(norm(p), set()).add(m)
    out = set()
    for p, ms in ops.items():
        if "DELETE" not in ms:
            continue
        parent = p.rsplit("/", 1)[0]
        if ms & {"PUT", "PATCH"} or (p.endswith("{}") and "POST" in ops.get(parent, set())):
            out.add(p)
    return out


def covered() -> set[str]:
    out = set()
    for k in registry.kinds():
        for o in (k.create, k.update, k.delete):
            if o is not None:
                out.add(norm(o.path))
    return out


def test_every_configurable_collection_is_code_or_excluded_with_a_reason():
    rs = routes()
    assert len(rs) > 800, len(rs)                                   # the whole API, switched on or not
    todo = configurable(rs)
    missing = sorted(p for p in todo - covered() if p not in EXCLUDED)
    assert not missing, ("these can be created and deleted through the API but are neither a resource type in "
                         "cairn.iac.registry (bundles, Terraform, export) nor on EXCLUDED with a reason: "
                         + ", ".join(missing))
    stale = sorted(p for p in EXCLUDED if p not in todo)
    assert not stale, f"no longer configurable, take them off EXCLUDED: {stale}"
    assert not set(EXCLUDED) & covered(), "both a resource type and excluded"
    assert all(len(why) > 20 for why in EXCLUDED.values())


def test_every_kind_s_paths_are_real_routes():
    known = {(m, norm(p)) for m, p in routes()}
    for k in registry.kinds():
        assert ("GET", norm(k.read)) in known, (k.name, k.read)
        for o in (k.create, k.update, k.delete):
            if o is not None:
                assert (o.method, norm(o.path)) in known, (k.name, o.method, o.path)


def test_the_registry_s_rules_and_examples():
    kinds = registry.kinds()
    registry.check(kinds)
    assert len(kinds) >= 50
    assert set(registry.FAMILIES) == {k.family for k in kinds}
    for k in kinds:
        names = {f.name for f in k.fields}
        for ex in (k.example, k.example_update):
            if ex is None:
                continue
            assert set(ex) <= names, (k.name, set(ex) - names)
        if k.example is not None:
            assert set(k.required) <= set(k.example), (k.name, "the example lacks", set(k.required) - set(k.example))
        for f in k.fields:
            if f.write_only:
                assert f.sensitive, (k.name, f.name, "a write-only value is a secret")
    schema = registry.schema()
    assert [x["name"] for x in schema["kinds"]] == [k.name for k in kinds]


def test_the_terraform_provider_is_generated_from_the_registry():
    """CI fails when zz_generated_kinds.go or the README's resource list was not regenerated."""
    from cairn.iac.terraform import GO_FILE, README, go_source, readme

    assert open(GO_FILE).read() == go_source(), "run: python -m cairn.iac.terraform"
    assert open(README).read() == readme(open(README).read()), "run: python -m cairn.iac.terraform"
    for k in registry.kinds():
        assert f'typeName: "{k.tf}"' in go_source()


def test_the_ui_s_copy_as_code_names_registry_types():
    """Areas of the UI's platform hubs that offer "Copy as Terraform" name a type the registry has, with its id."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui", "src", "consumer", "resources", "catalog.ts")
    kinds = re.findall(r'"iac": \{"kind": "([a-z_]+)", "id": "([a-z_]+)"\}', open(path).read())
    assert len(kinds) > 20
    for kind, ident in kinds:
        k = registry.kind(kind)
        assert not k.parents and ident == k.id, (kind, ident, k.id)


@pytest.mark.parametrize("name", ["pipes", "secrets", "agent_abuse_settings", "secret_scope_acls"])
def test_ids_split_back_into_parents(name):
    from cairn.iac.engine import split_id, tf_id

    k = registry.kind(name)
    rec = {**{p: "p1" for p in k.parents}, k.id: "x:y/z" if not k.singleton else None}
    parents, ident = split_id(k, tf_id(k, rec))
    assert parents == {p: "p1" for p in k.parents}
    assert ident == ("" if k.singleton else "x:y/z")
