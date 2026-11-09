"""The CLI's parser builds: two features registering the same command name breaks every `cairn` call (the API's
container entrypoint included), so this fails fast in the python job instead of on a cluster (CI run 328)."""

from cairn.cli import build_parser, cmd_export, cmd_export_metadata


def test_the_parser_builds_and_every_command_has_a_handler():
    p = build_parser()
    sub = next(a for a in p._actions if a.__class__.__name__ == "_SubParsersAction")
    assert len(sub.choices) == len(set(sub.choices))
    p.parse_args(["db", "current"])


def test_export_writes_code_and_export_metadata_writes_metadata():
    p = build_parser()
    a = p.parse_args(["export", "--format", "yaml", "--kind", "pipes"])
    assert a.fn is cmd_export and a.format == "yaml" and a.kind == ["pipes"]
    assert p.parse_args(["export"]).format == "terraform"
    m = p.parse_args(["export", "metadata", "--to", "/tmp/out", "--format", "iceberg"])
    assert m.fn is cmd_export_metadata and m.format == "iceberg" and m.to == "/tmp/out"
    assert p.parse_args(["export", "metadata", "--to", "/tmp/out"]).format == "parquet"


PEOPLE = {"login", "logout", "whoami", "profiles", "query", "search", "show", "ask", "iac", "completion"}


def test_the_people_commands_are_in_both_parsers_once_with_the_global_options_anywhere():
    from cairn_cli.main import build_parser as light_parser

    full = next(a for a in build_parser()._actions if a.__class__.__name__ == "_SubParsersAction").choices
    light = next(a for a in light_parser()._actions if a.__class__.__name__ == "_SubParsersAction").choices
    assert PEOPLE <= set(full) and PEOPLE | {"flows", "ontology"} <= set(light)
    assert {"api", "scheduler", "db"} <= set(full) and not {"api", "scheduler", "db"} & set(light)   # no server here
    for p in (build_parser(), light_parser()):
        before = p.parse_args(["--profile", "prod", "--output", "json", "query", "SELECT 1"])
        after = p.parse_args(["query", "SELECT 1", "--profile", "prod", "--output", "json", "-q", "--no-color"])
        assert (before.profile, before.output) == (after.profile, after.output) == ("prod", "json")
        assert after.quiet and after.no_color and after.client_command
        plain = p.parse_args(["whoami"])
        assert (plain.profile, plain.output, plain.quiet) == (None, "text", False)
    q = light_parser().parse_args(["query", "-f", "q.sql", "--format", "parquet", "--out", "o.parquet", "--limit", "5",
                                   "--warehouse", "team"])
    assert (q.file, q.format, q.out, q.limit, q.warehouse) == ("q.sql", "parquet", "o.parquet", 5, "team")
    assert light_parser().parse_args(["iac", "plan", "b.yaml", "--prune"]).iac_cmd == "plan"
    assert build_parser().parse_args(["ontology", "status"]).fn.__name__ == "cmd_ontology"     # the operator's here
