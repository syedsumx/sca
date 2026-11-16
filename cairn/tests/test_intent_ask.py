"""Intent-based security on ``POST /data/ask`` (review finding on L109; intents are tested end to end through the HTTP
API in tests/test_agent_intents.py)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cairn.common.security import Principal
from cairn.dataaccess.api import build_router
from cairn.identity.intents import IntentGuard, parse_intent

SALES = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.orders,PROD)"
HR = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.hr.salaries,PROD)"


class Ask:
    configured = True

    def __init__(self):
        self.asked = []

    def ask(self, actor, question, can_view, urns, run, limit):
        self.asked.append(urns)
        picked = urns or [HR]                            # Cairn picks the datasets when none are named
        return {"question": question, "tables": picked, "sql": "SELECT 1",
                "result": {"columns": ["salary"], "rows": [[1]], "tables": picked}}


class Hub:
    audit = None


def client(principal: Principal, ask: Ask) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(None, lambda: principal, ask=ask, can_view=lambda p: (lambda u: True),
                                    intents=IntentGuard(Hub())))
    return TestClient(app)


def agent() -> Principal:
    return Principal(urn="urn:li:corpuser:ana", via="agent", agent="agt_1", scopes=("query_data",),
                     intent=parse_intent({"purpose": "summarise revenue", "actions": ["query"],
                                          "tables": ["lake.sales.*"]}))


def test_an_agent_cannot_ask_about_tables_outside_its_intent():
    """Review finding: /data/ask checked an agent's intent by route only, so an agent declared for sales tables read
    hr.salaries by naming it (or by letting Cairn pick it) in the question's datasets."""
    ask = Ask()
    named = client(agent(), ask).post("/data/ask", json={"question": "What are the salaries?", "urns": [HR]})
    assert named.status_code == 403 and "outside this run's declared intent" in named.text
    assert ask.asked == []                               # refused before anything was read
    picked = client(agent(), ask).post("/data/ask", json={"question": "What are the salaries?"})
    assert picked.status_code == 403 and "salary" not in picked.text


def test_an_agent_asks_about_tables_inside_its_intent_and_people_are_untouched():
    ok = client(agent(), Ask()).post("/data/ask", json={"question": "What is revenue?", "urns": [SALES]})
    assert ok.status_code == 200 and ok.json()["tables"] == [SALES]
    person = client(Principal(urn="urn:li:corpuser:ana", via="session"), Ask())
    assert person.post("/data/ask", json={"question": "What are the salaries?", "urns": [HR]}).status_code == 200


def test_an_agent_cannot_ask_through_the_spreadsheet_route_about_tables_outside_its_intent():
    """Review finding (L118): /data/sheets/ask (scope query_data, as /data/ask) let Cairn pick the datasets and
    returned their rows without checking them against the agent's intent."""
    from cairn.dataaccess.sheets import SheetsService, build_router as build_sheets

    class Data:
        max_rows = 1000

    def sheets(principal: Principal) -> TestClient:
        app = FastAPI()
        svc = SheetsService(Data(), lambda: None, lambda: None, lambda: Ask(), default_connection="lake")
        app.include_router(build_sheets(svc, lambda: principal, lambda p: (lambda u: True), intents=IntentGuard(Hub())))
        return TestClient(app)
    r = sheets(agent()).post("/data/sheets/ask", json={"question": "What are the salaries?"})
    assert r.status_code == 403 and "salary" not in r.text
    person = sheets(Principal(urn="urn:li:corpuser:ana", via="session"))
    assert person.post("/data/sheets/ask", json={"question": "What are the salaries?"}).json()["values"] == [["salary"], [1]]
