# Example skills

Skills for AI coding tools (the `SKILL.md` format Claude Code and GitHub Copilot's agent skills read) that work with
Cairn through its MCP server. They are the single source: `examples/plugins/build.py` copies them into the Claude
Code plugin and the Copilot pack, and `tests/test_examples.py` checks every Cairn tool they name exists.

| Skill | Use it for |
|---|---|
| [`cairn-build-pipeline`](cairn-build-pipeline/SKILL.md) | Build a governed data pipeline in Cairn end to end, as code: ingestion, transformation, quality checks, schedule, proposed for approval |
| [`cairn-data-quality`](cairn-data-quality/SKILL.md) | Investigate and fix data quality in Cairn: why a dataset fails its checks, what it breaks, and new checks as code |
| [`cairn-governance-review`](cairn-governance-review/SKILL.md) | Review governance in Cairn: unlabelled personal data, missing owners and descriptions, who can access what, and access requests |
| [`cairn-iac-change`](cairn-iac-change/SKILL.md) | Change Cairn's configuration safely as code: export what exists, edit, plan, propose for approval |
