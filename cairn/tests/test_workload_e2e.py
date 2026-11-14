"""Workload management end to end (L6): two teams on one real Trino, each in its own resource group, Trino asking
Cairn about every action and sending every finished query to Cairn.

Team A floods Trino with heavy queries; team B's queries still start at once and finish quickly, while A runs at most
its concurrency and the rest of its flood waits in, or is turned away by, its own queue. Then each team's compute
(its queries) and storage (the lakehouse tables it owns, sized by the daily collection) appear per day in the cost
report, with nothing posted by hand. Needs docker and the store and catalog binaries."""

import calendar
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import httpx
import pytest

from test_trino_e2e import CATALOG_TOKEN, KEY, S3_ON, STORE_KEY, TRINO, _ready, cairn, lake, port  # noqa: F401

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

TEAM_A, TEAM_B = "urn:li:corpGroup:team-a", "urn:li:corpGroup:team-b"
MEMBERS = {"ann": TEAM_A, "al": TEAM_A, "bea": TEAM_B, "bo": TEAM_B}
# The chart's groups (deploy/trino/resource-groups.json) with a group per team under them. A team's selector names
# its members (Trino knows people by name; with a Trino group provider, "userGroup" selects a team as a whole).
GROUPS = {
    "rootGroups": [{
        "name": "global", "softMemoryLimit": "90%", "hardConcurrencyLimit": 100, "maxQueued": 1000,
        "subGroups": [
            {"name": "teams", "softMemoryLimit": "80%", "hardConcurrencyLimit": 20, "maxQueued": 200,
             "schedulingPolicy": "fair", "subGroups": [
                 {"name": "team-a", "softMemoryLimit": "40%", "hardConcurrencyLimit": 2, "maxQueued": 4},
                 {"name": "team-b", "softMemoryLimit": "40%", "hardConcurrencyLimit": 2, "maxQueued": 4}]},
            {"name": "adhoc", "softMemoryLimit": "50%", "hardConcurrencyLimit": 50, "maxQueued": 500}]}],
    "selectors": [{"user": "(ann|al)", "group": "global.teams.team-a"},
                  {"user": "(bea|bo)", "group": "global.teams.team-b"},
                  {"group": "global.adhoc"}],
}
# Trino's sequence() stops at 10,000 entries: cross joins make the work. HEAVY takes seconds of all the cores.
HEAVY = ("SELECT count(*) AS n FROM UNNEST(sequence(1, 10000)) AS a(x) CROSS JOIN UNNEST(sequence(1, 10000)) AS b(y) "
         "CROSS JOIN UNNEST(sequence(1, 3)) AS c(z) WHERE (x * 31 + y * 17 + z) % 1013 = 7")
MEDIUM = ("SELECT count(*) AS n FROM UNNEST(sequence(1, 10000)) AS a(x) CROSS JOIN UNNEST(sequence(1, 2000)) AS b(y) "
          "WHERE (x * 31 + y * 17) % 1013 = 7")
LIGHT = "SELECT count(*) AS n FROM UNNEST(sequence(1, 9999)) AS t(x) WHERE x % 3 = 0"


@pytest.fixture(scope="module")
def workload(lake, cairn):  # noqa: F811
    url, s = cairn
    for user, team in MEMBERS.items():                  # the teams, as the catalog knows them (SCIM, SSO groups)
        s.graph.aspects[(f"urn:li:corpuser:{user}", "groupMembership")] = {"groups": [team]}
        s.attributes.invalidate(f"urn:li:corpuser:{user}")
    d = tempfile.mkdtemp(prefix="trino-wl-")
    os.makedirs(f"{d}/catalog")
    tp = port()
    open(f"{d}/catalog/lake.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={lake['catalog']}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={CATALOG_TOKEN}",
        S3_ON, f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}", ""]))
    base = f"{url}/v1/trino/{KEY}"
    open(f"{d}/access-control.properties", "w").write("\n".join([
        "access-control.name=opa", f"opa.policy.uri={base}/allow", f"opa.policy.batched-uri={base}/batch",
        f"opa.policy.row-filters-uri={base}/row-filters", f"opa.policy.column-masking-uri={base}/column-mask", ""]))
    open(f"{d}/usage-listener.properties", "w").write("\n".join([
        "event-listener.name=http", "http-event-listener.log-completed=true",
        f"http-event-listener.connect-ingest-uri={base}/queries", ""]))
    open(f"{d}/resource-groups.properties", "w").write(
        "resource-groups.configuration-manager=file\nresource-groups.config-file=/etc/trino/resource-groups.json\n")
    json.dump(GROUPS, open(f"{d}/resource-groups.json", "w"))
    open(f"{d}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}", "query.max-memory=2GB",
        "event-listener.config-files=/etc/trino/usage-listener.properties", ""]))
    os.chmod(d, 0o755)
    name = f"cairn-trino-wl-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    mounts = []
    for f in ("config.properties", "access-control.properties", "usage-listener.properties",
              "resource-groups.properties", "resource-groups.json"):
        mounts += ["-v", f"{d}/{f}:/etc/trino/{f}:ro"]
    try:
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                        "-v", f"{d}/catalog:/etc/trino/catalog:ro", *mounts, TRINO],
                       check=True, capture_output=True, timeout=600)
        end = time.time() + 240
        while "SERVER STARTED" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr:
            assert time.time() < end, "Trino did not start"
            time.sleep(1)
        yield {"port": tp, "cairn": url, "s": s, "lake": lake}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        shutil.rmtree(d, ignore_errors=True)


def _connect(tp, user):
    import trino

    return trino.dbapi.connect(host="127.0.0.1", port=tp, user=user, catalog="lake", http_scheme="http",
                               request_timeout=600)


def _sql(tp, user, statement):
    cur = _connect(tp, user).cursor()
    cur.execute(statement)
    rows = cur.fetchall()
    return rows, cur.stats


def test_a_flooding_team_is_held_to_its_group_and_the_other_team_is_not_kept_waiting(workload):
    tp = workload["port"]
    results: dict[int, object] = {}

    def flood(i):
        try:
            results[i] = _sql(tp, "ann" if i % 2 else "al", HEAVY)[1]
        except Exception as e:  # noqa: BLE001 - what Trino answered: a full queue is expected
            results[i] = e
    threads = [threading.Thread(target=flood, args=(i,), daemon=True) for i in range(10)]
    for t in threads:
        t.start()
        time.sleep(0.05)

    def team_a_states():
        rows, _ = _sql(tp, "admin", "SELECT state, count(*) FROM system.runtime.queries WHERE "
                                    "array_join(resource_group_id, '.') = 'global.teams.team-a' AND "
                                    "state NOT IN ('FINISHED', 'FAILED') GROUP BY state")
        return dict(rows)
    peaks = {"running": 0, "queued": 0}
    sampling = threading.Event()

    def sample():                       # how many of A's queries Trino ran at once, all through the flood
        while not sampling.is_set():
            states = team_a_states()
            peaks["running"] = max(peaks["running"], sum(v for k, v in states.items() if k != "QUEUED"))
            peaks["queued"] = max(peaks["queued"], states.get("QUEUED") or 0)
            time.sleep(0.2)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    end = time.time() + 120
    while (team_a_states().get("QUEUED") or 0) < 1:      # A's flood is in: some running, more waiting
        assert time.time() < end, ("team A's flood never queued", {k: str(v)[:200] for k, v in results.items()})
        time.sleep(0.3)
    # Team B, while A's queries run and queue: each starts at once and finishes quickly.
    b_stats = []
    for i in range(3):
        t0 = time.time()
        rows, stats = _sql(tp, "bea" if i % 2 == 0 else "bo", LIGHT)
        b_stats.append((time.time() - t0, stats))
        assert rows == [[3333]]
    a_now = team_a_states()
    for elapsed, stats in b_stats:
        assert stats["queuedTimeMillis"] < 2000 and elapsed < 30, (elapsed, stats)
    assert (a_now.get("QUEUED") or 0) >= 1, "A's flood should still be waiting while B ran"  # B did not wait for A
    for t in threads:
        t.join(600)
    sampling.set()
    sampler.join(30)
    # A ran at most two at a time; with four waiting, the rest of its flood was turned away at once.
    rejected = [r for r in results.values() if isinstance(r, Exception)]
    done = [r for r in results.values() if not isinstance(r, Exception)]
    assert peaks["running"] == 2 and 1 <= peaks["queued"] <= 4, peaks
    assert rejected and all("QUERY_QUEUE_FULL" in str(e) or "Too many queued queries" in str(e) for e in rejected), rejected
    assert len(done) >= 6 and len(done) + len(rejected) == 10, (len(done), len(rejected))
    assert max(r["queuedTimeMillis"] for r in done) > 1000          # A's own queries waited for A's slots


def test_each_teams_compute_and_storage_appear_per_day_without_posting(workload, monkeypatch):
    from pyiceberg.catalog.rest import RestCatalog

    from cairn.reports.costs import _day

    tp, s, url = workload["port"], workload["s"], workload["cairn"]
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_CPU_HOUR", "2.0")
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_TB_SCANNED", "5.0")
    monkeypatch.setenv("CAIRN_COST_LAKEHOUSE_USD_PER_TB_MONTH", "300000")   # a test price: the tables are small
    # Each team owns a table in the lakehouse (written through Trino, as Cairn's admin), the larger one A's.
    admin_cat = RestCatalog("admin", uri=workload["lake"]["catalog"], token=CATALOG_TOKEN)
    for team, k in (("team_a", 20), ("team_b", 2)):
        _sql(tp, "admin", f"CREATE SCHEMA IF NOT EXISTS lake.{team}")
        _sql(tp, "admin", f"CREATE TABLE IF NOT EXISTS lake.{team}.events AS SELECT x * 100 + y AS id, "
                          f"cast(x * 7 + y AS varchar) AS v FROM UNNEST(sequence(1, 10000)) AS a(x) "
                          f"CROSS JOIN UNNEST(sequence(1, {k})) AS b(y)")
        s.graph.aspects[(f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{team}.events,PROD)", "ownership")] = {
            "owners": [{"owner": f"urn:li:corpGroup:{team.replace('_', '-')}", "type": "TECHNICAL_OWNER"}]}
    # Both teams query (A's flood ran before this test when the module runs whole; these make it independent).
    for user in ("ann", "bea"):
        _sql(tp, user, LIGHT)
    _sql(tp, "ann", MEDIUM)
    # The daily collection, as the scheduler runs it (here for today): the lakehouse's tables are sized.
    s.costs.lakehouse = ({"uri": workload["lake"]["catalog"], "token": CATALOG_TOKEN},
                         lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")
    today = _day(int(time.time() * 1000))
    collected = s.costs.collect_day(today)
    assert isinstance(collected.get("lakehouse"), int) and collected["lakehouse"] >= 2, collected
    sizes = {t: admin_cat.load_table(f"{t}.events").current_snapshot().summary["total-files-size"]
             for t in ("team_a", "team_b")}

    def report(actor):
        end = time.time() + 60
        while True:                    # Trino sends finished queries a moment after they finish
            r = httpx.get(f"{url}/reports/costs/teams", params={"days": 2}, headers={"X-Cairn-Actor": actor}, timeout=30)
            assert r.status_code == 200, r.text
            rows = {(x["day"], x["team"]): x for x in r.json()["rows"]}
            if all(rows.get((today, t), {}).get("queries") for t in (TEAM_A, TEAM_B)) or time.time() > end:
                return r.json(), rows
            time.sleep(1)
    out, rows = report("urn:li:corpuser:admin")
    assert out["scope"] == "all" and out["priced"]
    a, b = rows[(today, TEAM_A)], rows[(today, TEAM_B)]
    for team, r, size in ((TEAM_A, a, sizes["team_a"]), (TEAM_B, b, sizes["team_b"])):
        assert r["queries"] >= 1 and r["sql_usd"] > 0 and r["cpu_hours"] > 0, (team, r)
        per_day = int(size) * 300000 / 1e12 / calendar.monthrange(int(today[:4]), int(today[5:7]))[1]
        assert r["storage_bytes"] == int(size) and r["storage_usd"] == pytest.approx(per_day, abs=1e-6), (team, r, size)
        assert r["total_usd"] >= r["sql_usd"] + r["storage_usd"] - 1e-6
    assert a["cpu_hours"] > b["cpu_hours"] and a["storage_bytes"] > b["storage_bytes"]
    # A member sees their own team's rows only.
    mine, rows = report("urn:li:corpuser:bea")
    assert mine["scope"] == "mine" and {t for _, t in rows} == {TEAM_B}
