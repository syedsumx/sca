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
