"""Command line entry point: ``tether <command>`` (or ``python -m tether.cli``)."""

import argparse
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from tether.config import get_settings
from tether.db.session import session_scope
from tether.pipelines.seed import seed


def _project_root() -> Path:
    """Directory holding ``alembic.ini`` (source checkout or the Docker workdir)."""
    for candidate in (Path.cwd(), Path(__file__).resolve().parents[2]):
        if (candidate / "alembic.ini").exists():
            return candidate
    raise SystemExit("alembic.ini not found; run from the project root")


def alembic_config(database_url: str | None = None) -> Config:
    root = _project_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = database_url or get_settings().database_url
    return cfg


def cmd_migrate(_: argparse.Namespace) -> None:
    command.upgrade(alembic_config(), "head")


def cmd_seed(_: argparse.Namespace) -> None:
    with session_scope() as s:
        result = seed(s)
    print(f"seeded {result.customers} customers and {result.tickets} tickets")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tether", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("seed", help="reset and seed demo source data").set_defaults(func=cmd_seed)
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
