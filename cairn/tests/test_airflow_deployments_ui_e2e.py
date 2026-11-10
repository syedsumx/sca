"""Managed Airflow's tab in the web UI (L146), in Chromium, against real deployments: the built UI bundle on Cairn's
real API, with two deployments run by the Docker runtime on a real Airflow 3.3 image (the fixtures of
tests/test_airflow_deployments_e2e.py).

* a team's environments are cards in promotion order (dev → stage), each with its state in words, its Airflow
  version, live deploy, the identity it runs as and its hours; the first is picked (``aria-pressed``);
* the picked environment scopes everything below it: its deploys (one live, one refused with the reason shown),
  the promotion waiting for an approver, its health read from the deployment, and its logs;
* picking another environment moves all of that to it; the page passes axe and has no sideways scroll on a phone.

Needs docker, the store and catalog binaries, CAIRN_AIRFLOW_IMAGE (cairn/airflow in CI), Playwright with Chromium and
the built UI bundle (ui/dist/mfe)."""

from __future__ import annotations

import os
import re
import socket
import threading
import time

import pytest

from test_accessibility_e2e import AXE, BUNDLE, HOST, audit
from test_airflow_deployments_e2e import ADMIN, IMAGE, af, af_env, api, bundle  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

DEV, STAGE = "uidev", "uistage"
DAG = '''
from datetime import datetime

from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

with DAG("ui_dag", start_date=datetime(2026, 1, 1), schedule=None):
    EmptyOperator(task_id="noop")
'''


def _ui_ready() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and os.path.exists(AXE)


pytestmark = pytest.mark.skipif(not (_ready() and IMAGE and _ui_ready()),
                                reason="needs docker, CAIRN_AIRFLOW_IMAGE, the binaries, Playwright and the built UI")


@pytest.fixture
def ui(af):  # noqa: F811
    """The built UI on Cairn's API (the same services as the deployments), with two deployments, deploys and a
    promotion waiting."""
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app

    r = api(af, "POST", "/v1/airflow/deployments", json={"name": DEV, "environment": "dev", "team": "finance",
                                                        "promotes_to": STAGE})
    assert r.status_code == 201, r.text
    r = api(af, "POST", "/v1/airflow/deployments", json={"name": STAGE, "environment": "stage", "team": "finance",
                                                        "approval": {"required": True}})
    assert r.status_code == 201, r.text
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="afui"))
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys?note=first", content=bundle(tmp, {"ui_dag.py": DAG}),
            headers={"Content-Type": "application/gzip"})
    assert r.status_code == 201, r.text
    broken = bundle(tmp, {"ui_dag.py": DAG, "broken.py": "from airflow.sdk import DAG\n\nwith DAG(\"broken\":\n"})
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys", content=broken, headers={"Content-Type": "application/gzip"})
    assert r.status_code == 422, r.text
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/promote", json={"comment": "ready for stage"})
    assert r.status_code == 201 and r.json()["state"] == "pending", r.text

    app = create_api_app(af["s"])
    app.mount("/ui", StaticFiles(directory=BUNDLE), name="ui")
    app.add_api_route("/host", lambda: HTMLResponse(HOST), methods=["GET"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            break
        except OSError:
            assert time.time() < end
            time.sleep(0.1)
    yield f"http://127.0.0.1:{p}"
    server.should_exit = True
    for name in (DEV, STAGE):
        try:
            af["svc"].delete(ADMIN, name)
        except Exception:  # noqa: BLE001 - already gone
            pass


def test_environments_scope_the_deploys_promotions_and_health(ui):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ADMIN}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{ui}/host#/compute?tab=airflow-deployments", wait_until="networkidle")
            envs = page.get_by_role("region", name="Environments")
            expect(envs.get_by_role("heading", name="finance: dev → stage")).to_be_visible(timeout=30_000)
            dev = envs.get_by_role("button", name=DEV, exact=True)
            stage = envs.get_by_role("button", name=STAGE, exact=True)
            expect(dev).to_have_attribute("aria-pressed", "true")             # the first in promotion order
            expect(stage).to_have_attribute("aria-pressed", "false")
            card = envs.get_by_role("article", name=DEV)
            expect(card.get_by_text("Ready")).to_be_visible()
            expect(card.locator('[data-tone="ok"]').first).to_be_visible()     # a shape and words, not colour alone
            expect(card.get_by_text("Always on")).to_be_visible()
            expect(card.get_by_text(f"svc-af-{DEV}")).to_be_visible()

            deploys = page.get_by_role("region", name=f"Deploys to {DEV}")
            expect(deploys.get_by_text("Live", exact=True)).to_be_visible(timeout=20_000)
            expect(deploys.get_by_text("Refused", exact=True)).to_be_visible()
            expect(deploys.get_by_role("note")).to_contain_text("broken.py")
            waiting = page.get_by_role("region", name="Promotions waiting")
            expect(waiting.get_by_text(re.compile(f"{DEV} → {STAGE}"))).to_be_visible()
            expect(waiting.get_by_text("Pending", exact=True)).to_be_visible()
            expect(waiting.get_by_role("button", name="Approve")).to_be_visible()
            health = page.get_by_role("region", name=f"{DEV} health")
            expect(health.get_by_text(re.compile(r"\d+(\.\d+)? s ago"))).to_be_visible(timeout=60_000)  # the real heartbeat
            expect(page.get_by_role("region", name=f"Logs of {DEV}")).to_be_visible()
            page.wait_for_load_state("networkidle")
            bad = audit(page)
            assert not bad, bad

            # Another environment: the deploys, promotions, health and logs follow it.
            stage.click()
            expect(stage).to_have_attribute("aria-pressed", "true")
            expect(dev).to_have_attribute("aria-pressed", "false")
            expect(page.get_by_role("region", name=f"Deploys to {STAGE}")).to_be_visible()
            expect(page.get_by_role("region", name=f"Deploys to {DEV}")).to_have_count(0)
            expect(page.get_by_role("region", name=f"{STAGE} health")).to_be_visible()
            expect(page.get_by_role("region", name=f"Logs of {STAGE}")).to_be_visible()

            # A phone: the cards stack, nothing scrolls sideways.
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(300)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        finally:
            browser.close()
    assert not errors, errors
