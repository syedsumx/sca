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


def cmd_pipelines(_: argparse.Namespace) -> None:
    from tether.embeddings import get_embedder
    from tether.pipelines.run import run_pipelines

    with session_scope() as s:
        result = run_pipelines(s, get_embedder())
    print(
        f"customers={result.customers} rag={result.rag} "
        f"models={result.training.models} memory={result.memory}"
    )


def cmd_reset(_: argparse.Namespace) -> None:
    from tether.demo import reset_database

    reset_database()
    print("database reset (all tables dropped and re-created)")


def cmd_worker(_: argparse.Namespace) -> None:
    from tether.deletion.worker import run_forever

    run_forever()


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("tether.api.app:app", host=args.host, port=args.port)


def cmd_demo(args: argparse.Namespace) -> None:
    from tether.demo import Backend, DemoFailure, HttpBackend, InProcessBackend, run_demo

    backend: Backend = InProcessBackend() if args.in_process else HttpBackend(args.api_url)
    try:
        run_demo(backend, args.subject, out_dir=Path(args.out_dir))
    except DemoFailure as exc:
        raise SystemExit(f"DEMO FAILED: {exc}") from exc


def cmd_verify_certificate(args: argparse.Namespace) -> None:
    import json

    from tether.deletion.certificate import verify_certificate

    doc = json.loads(Path(args.path).read_text())
    ok = verify_certificate(doc["certificate"], doc["sha256"])
    print(f"{'VALID' if ok else 'INVALID'}: sha256 {doc['sha256']}")
    if not ok:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tether", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("seed", help="reset and seed demo source data").set_defaults(func=cmd_seed)
    sub.add_parser("pipelines", help="run the RAG, training and memory pipelines").set_defaults(
        func=cmd_pipelines
    )
    sub.add_parser("reset", help="drop and re-create all tables").set_defaults(func=cmd_reset)
    sub.add_parser("worker", help="run the deletion job worker").set_defaults(func=cmd_worker)
    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    demo = sub.add_parser("demo", help="run the end-to-end deletion scenario")
    mode = demo.add_mutually_exclusive_group()
    mode.add_argument("--api-url", default="http://localhost:8000", help="drive a running API")
    mode.add_argument("--in-process", action="store_true", help="no API/worker needed")
    demo.add_argument("--subject", default="cust-007", help="customer to delete")
    demo.add_argument("--out-dir", default="certificates", help="where to save the certificate")
    demo.set_defaults(func=cmd_demo)
    verify = sub.add_parser("verify-certificate", help="check a downloaded certificate file")
    verify.add_argument("path")
    verify.set_defaults(func=cmd_verify_certificate)
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
