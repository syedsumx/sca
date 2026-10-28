"""Automations: selector + actions, previews, additive runs as undoable batches under the
automation's identity, schedules and notifications."""

import pytest

from cairn.governance.automations import AutomationError, AutomationService, AutomationSpec, changes_for, identity
from cairn.governance.bulk import BulkService

FIN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,finance.public.ledger,PROD)"
FIN2 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,finance.public.payments,PROD)"
MKT = "urn:li:dataset:(urn:li:dataPlatform:snowflake,marketing.public.leads,PROD)"
SPEC = {"name": "Finance schema", "select": {"attr": "resource.urn", "op": "contains", "value": "finance."},
        "actions": [{"type": "set_domain", "value": "finance"}, {"type": "add_owner", "value": "fin-lead"},
                    {"type": "add_tag", "value": "finance"}, {"type": "notify", "recipients": ["fin-lead"]}]}


@pytest.fixture
def svc(engine, graph):
    graph.aspects[(FIN, "globalTags")] = {"tags": [{"tag": "urn:li:tag:gold"}]}
    graph.aspects[(FIN2, "domains")] = {"domains": ["urn:li:domain:finance"]}
    graph.aspects[(FIN2, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:fin-lead", "type": "DATAOWNER"}]}
    graph.aspects[(FIN2, "globalTags")] = {"tags": [{"tag": "urn:li:tag:finance"}]}

    def datasets():
        return [(u, {"urn": u}, {n: graph.get_aspect(u, n) for n in
                                 ("globalTags", "glossaryTerms", "ownership", "domains")}) for u in (FIN, FIN2, MKT)]

    sent = []
    s = AutomationService(engine, BulkService(engine, graph), datasets, lambda row, to, urns: sent.append((to, urns)))
    return s, graph, sent


def test_spec_validation_and_short_names():
    spec = AutomationSpec.model_validate(SPEC)
    assert [a.value for a in spec.actions[:3]] == ["urn:li:domain:finance", "urn:li:corpuser:fin-lead", "urn:li:tag:finance"]
    for bad in ({**SPEC, "select": {"attr": "resource.urn", "op": "nope", "value": 1}},
                {**SPEC, "actions": [{"type": "add_tag"}]},
                {**SPEC, "every_minutes": 1},
                {**SPEC, "actions": [{"type": "set_domain", "value": "a"}, {"type": "set_domain", "value": "b"}]}):
        with pytest.raises(ValueError):
            AutomationSpec.model_validate(bad)


def test_only_missing_metadata_is_written():
    spec = AutomationSpec.model_validate(SPEC)
    got = changes_for(spec, FIN, {"globalTags": {"tags": [{"tag": "urn:li:tag:gold"}]}})
    assert {c["what"]: c.get("add", c["to"]) for c in got} == {
        "tags": ["urn:li:tag:finance"], "owners": ["urn:li:corpuser:fin-lead"], "domain": "urn:li:domain:finance"}
    assert [c for c in got if c["what"] == "tags"][0]["to"] == ["urn:li:tag:finance", "urn:li:tag:gold"]   # gold kept


def test_preview_run_undo(svc):
    s, graph, sent = svc
    pre = s.preview(AutomationSpec.model_validate(SPEC))
    assert pre["matched"] == 2 and pre["would_change"] == 1          # FIN2 already complies
    a = s.create(AutomationSpec.model_validate(SPEC), "urn:li:corpuser:admin")
    run = s.run(a["id"])
    assert run["state"] == "APPLIED" and run["matched"] == 2 and run["changed"] == 1
    assert graph.aspects[(FIN, "domains")] == {"domains": ["urn:li:domain:finance"]}
    assert {t["tag"] for t in graph.aspects[(FIN, "globalTags")]["tags"]} == {"urn:li:tag:gold", "urn:li:tag:finance"}
    props = graph.emitted[-1]["systemMetadata"]["properties"]
    assert props["cairn_source"] == "automation" and props["cairn_edited_by"] == identity(a["id"])
    assert sent == [(["urn:li:corpuser:fin-lead"], [FIN])]
    again = s.run(a["id"])
    assert again["state"] == "NOOP" and again["changed"] == 0 and len(sent) == 1     # nothing new to tell
    s.undo(run["id"], "urn:li:corpuser:admin")
    assert graph.aspects[(FIN, "globalTags")] == {"tags": [{"tag": "urn:li:tag:gold"}]}
    assert s.get_run(run["id"])["state"] == "UNDONE"
    with pytest.raises(AutomationError):
        s.undo(run["id"], "x")
    assert [r["state"] for r in s.runs(a["id"])] == ["NOOP", "UNDONE"]


def test_schedule_and_disable(svc):
    s, graph, sent = svc
    a = s.create(AutomationSpec.model_validate({**SPEC, "every_minutes": 60}), "admin")
    assert s.sweep(now_ms=10**13) == {a["id"]: "APPLIED"}
    assert s.sweep(now_ms=10**13 + 1000) == {}                       # not due yet
    s.set_enabled(a["id"], False, "admin")
    assert s.sweep(now_ms=10**13 + 4_000_000) == {}
    assert s.get(a["id"])["enabled"] is False and s.get(a["id"])["last_run"]["trigger"] == "schedule"
    s.delete(a["id"], "admin")
    with pytest.raises(AutomationError):
        s.get(a["id"])
