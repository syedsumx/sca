"""An exported bundle keeps ``${`` in Cairn's text literally: applying it never pulls in the environment."""

import yaml

from cairn.iac import registry
from cairn.iac.bundle import parse
from cairn.iac.export import to_bundle


def test_dollar_brace_in_exported_text_round_trips(monkeypatch):
    monkeypatch.setenv("CAIRN_TOKEN", "secret-of-the-ci-runner")
    sql = "SELECT '${CAIRN_TOKEN}' AS t, '$${x}' AS u"
    rec = {"id": "q1", "owner": "urn:li:corpuser:ana", "name": "q", "sql": sql, "connection": "lakehouse",
           "description": "costs in ${currency}", "shared": False, "chart": {}}
    text = to_bundle([(registry.kind("queries"), rec)])
    got = parse(yaml.safe_load(text))["queries"]["q"]
    assert got["sql"] == sql and got["description"] == "costs in ${currency}"


def test_bundle_does_not_advertise_a_token_on_the_command_line():
    from cairn.cli import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    assert "--token" not in sub.choices["bundle"].format_help()
    assert "--token" not in sub.choices["export"].format_help()
