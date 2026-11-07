"""Aggregation and projection policies (Snowflake gap H): the SQL checker refuses what would release rows or a
protected column, and adds the group-size floor to every block that reads a protected table, including subqueries,
CTEs and unions. The ways round it that matter are each tried: a star, a window, an ungrouped column, a join that
repeats one person's rows, an expression or a subquery that returns a protected column, and a misconfigured policy.
The rewritten SQL is run on SQLite, so the floors are checked as results, not as text."""

import sqlite3

import pytest
import sqlglot

from cairn.governance.abac import Obligation
from cairn.governance.aggregation import CheckedQueries, PolicyViolation, TablePolicy, enforce, from_obligations

PAY = TablePolicy(min_group=3, policies=["p"])
KEYED = TablePolicy(min_group=3, entity_key="person_id", policies=["p"])
HIDE = TablePolicy(projection={"email"}, policies=["q"])


def check(sql, policies):
    tree = sqlglot.parse_one(sql, read="trino")
    return enforce(tree, lambda t: policies.get(t.name.lower())).sql(dialect="trino")


@pytest.fixture
def db():
    c = sqlite3.connect(":memory:")
    c.execute("create table pay (person_id int, dept text, salary int, email text)")
    rows = [(1, "eng", 100, "a@x"), (2, "eng", 110, "b@x"), (3, "eng", 120, "c@x"), (4, "ops", 90, "d@x")]
    c.executemany("insert into pay values (?,?,?,?)", rows)
    c.execute("create table visits (person_id int, day int)")
    c.executemany("insert into visits values (?,?)", [(4, d) for d in range(10)])
    return c


def run(db, sql):
    return sorted(db.execute(sqlglot.transpile(sql, read="trino", write="sqlite")[0]).fetchall())


def test_only_groups_of_the_minimum_size_come_back(db):
    sql = check("select dept, avg(salary) from pay group by dept", {"pay": PAY})
    assert "HAVING COUNT(*) >= 3" in sql
    assert run(db, sql) == [("eng", 110.0)]                         # ops (one person) is held back
    assert run(db, check("select count(*) from pay where person_id = 4", {"pay": PAY})) == []
    # Group by position, a constant and an existing HAVING are kept.
    sql = check("select dept, 'x', sum(salary) from pay group by 1 having sum(salary) > 0", {"pay": PAY})
    assert run(db, sql) == [("eng", "x", 330)]
    # A block that reads the table inside a subquery, a CTE and a union branch: each gets the floor.
    sql = check("with t as (select dept, sum(salary) s from pay group by dept) select max(s) from t", {"pay": PAY})
    assert run(db, sql) == [(330,)]
    sql = check("select max(s) from (select dept, sum(salary) s from pay group by dept) x", {"pay": PAY})
    assert run(db, sql) == [(330,)]
    sql = check("select count(*) from pay union all select count(*) from pay where dept = 'ops'", {"pay": PAY})
    assert run(db, sql) == [(4,)]


@pytest.mark.parametrize("sql,why", [
    ("select * from pay", "name the columns"),
    ("select salary from pay", "only be read in aggregate"),
    ("select dept, salary, count(*) from pay group by dept", "neither aggregated nor grouped"),
    ("select dept, sum(salary) over (partition by dept) from pay group by dept", "window"),
    ("select x from (select salary as x from pay) t", "aggregate"),
    ("select count(*) from pay p join visits v on p.person_id = v.person_id", "entity key"),
    # Aggregates that hand back a row's own value (review finding: array_agg returned every salary).
    ("select dept, array_agg(salary) from pay group by dept", "row's own value"),
    ("select dept, max(salary) from pay group by dept", "row's own value"),
    ("select dept, min(salary) from pay group by dept", "row's own value"),
    ("select dept, arbitrary(salary) from pay group by dept", "row's own value"),
    ("select dept, listagg(email, ',') from pay group by dept", "row's own value"),
    ("select dept, approx_percentile(salary, 0.5) from pay group by dept", "row's own value"),
    ("select dept, min_by(email, salary) from pay group by dept", "row's own value"),
    ("select dept, count(*) from pay group by dept having max(salary) > 100", "row's own value"),
    ("select dept, histogram(salary) from pay group by dept", "does not know"),
    ("select dept, sum(salary) + cardinality(map_agg(person_id, salary)) from pay group by dept", "does not know"),
])
def test_what_would_release_rows_is_refused(sql, why):
    with pytest.raises(PolicyViolation, match=why):
        check(sql, {"pay": PAY})


def test_reducing_aggregates_are_allowed(db):
    sql = check("select dept, count(*), count(distinct person_id), sum(salary), avg(salary), stddev(salary), "
                "variance(salary), approx_distinct(email), count_if(salary > 100) from pay group by dept", {"pay": PAY})
    assert "HAVING COUNT(*) >= 3" in sql


def test_with_an_entity_key_joins_count_people_not_rows(db):
    # Person 4 has ten visits: counting rows would pass a floor of 3; counting people does not.
    sql = check("select p.dept, count(*) from pay p join visits v on p.person_id = v.person_id group by p.dept",
                {"pay": KEYED})
    assert "COUNT(DISTINCT p.person_id) >= 3" in sql
    assert run(db, sql) == []


def test_projection_columns_are_used_but_never_returned(db):
    ok = ["select dept from pay where email = 'a@x'",
          "select dept, count(email) from pay group by dept",
          "select p.dept from pay p join visits v on p.email = cast(v.day as varchar)",
          "select dept from pay order by email"]
    for sql in ok:
        check(sql, {"pay": HIDE})
    for sql in ["select email from pay", "select upper(email) from pay", "select p.email from pay p",
                "select x from (select email as x from pay) t", "select * from pay",
                "select dept, max(email) from pay group by dept"]:
        with pytest.raises(PolicyViolation):
            check(sql, {"pay": HIDE})
    # Another table's column of the same name is not this table's.
    check("select v.email from pay p join visits v on p.person_id = v.person_id", {"pay": HIDE})


def test_policies_come_from_obligations_and_misconfigured_ones_refuse():
    pol = from_obligations([Obligation(type="aggregation", params={"min_group": 5}, policy_id="a"),
                            Obligation(type="aggregation", params={"min_group": 10}, policy_id="b"),
                            Obligation(type="projection", params={"columns": ["Email"]}, policy_id="c"),
                            Obligation(type="max_rows", params={"n": 1})])
    assert pol.min_group == 10 and pol.projection == {"email"} and pol.policies == ["a", "b", "c"]
    assert from_obligations([Obligation(type="max_rows", params={})]) is None
    for bad in [{"min_group": 1}, {"min_group": "5"}, {"min_group": True}, {"min_group": 5, "entity_key": ""}]:
        with pytest.raises(PolicyViolation):
            from_obligations([Obligation(type="aggregation", params=bad, policy_id="x")])
    with pytest.raises(PolicyViolation):
        from_obligations([Obligation(type="projection", params={"columns": []}, policy_id="x")])


def test_checked_identities_expire(engine):
    now = {"t": 1_000_000}
    cq = CheckedQueries(engine, clock=lambda: now["t"])
    ident = cq.register("urn:li:corpuser:ana", ["urn:li:corpGroup:hr"], ["urn:b", "urn:a"])
    user = ident.removeprefix("urn:li:corpuser:")
    assert user.startswith("checked-")
    assert cq.lookup(user) == {"actor": "urn:li:corpuser:ana", "groups": ["urn:li:corpGroup:hr"],
                               "tables": {"urn:a", "urn:b"}}
    assert cq.lookup("checked-nope") is None and cq.lookup("ana") is None
    now["t"] += 11 * 60_000
    assert cq.lookup(user) is None
