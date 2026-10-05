# Contributing to Tether

Thanks for helping! Tether is an early-stage MVP, and issues and pull requests are
welcome.

## Getting set up

```bash
cd tether
docker compose up -d db             # Postgres 16 + pgvector on :5432 (also creates tether_test)
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
tether migrate
make test lint typecheck
```

Run the UI with `make ui-dev`. It proxies `/api` to `localhost:8000`.

## Ground rules

- **Type hints everywhere.** `ruff` and `mypy --strict` must stay clean (`make lint typecheck`).
- **Thin route handlers.** Business logic lives in `deletion/`, `policy/`, `lineage/`,
  `services/` and `pipelines/`.
- **Every deletion state change** goes through `deletion/service.py::_transition`, which
  writes the log line and the audit entry.
- **Idempotency is a feature.** Any new deletion step or purger must be safe to run
  twice and must tolerate data that is already gone.
- **Fail closed.** An unknown license counts as `restricted`, and a subject with no
  policy counts as `pending`.
- **Tests.** Add tests next to the behavior you change. The suite uses a real Postgres
  database (`TETHER_TEST_DATABASE_URL`) because filtering and locking semantics matter.
- **Migrations.** Schema changes need an Alembic migration (`alembic revision
  --autogenerate`), and `alembic check` must be clean.

## Adding a connector

1. **Vector store.** Implement `stores/vector.py::VectorStore`. Apply the block list
   *inside* the similarity query, before top-k.
2. **Purger.** Register one for its node type in `deletion/purgers.py::PURGERS`.
3. **Source table.** Add it to `stores/source.py::SOURCE_TABLES`, with its purge order
   and subject column.
4. **Tests.** Extend `tests/test_deletion_e2e.py` so a deletion is shown to reach the
   new store.

## Commit and PR conventions

- Keep PRs focused. Describe the behavior change and how you tested it.
- By contributing, you agree that your contributions are licensed under Apache-2.0.
