"""``dhe`` command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("DHE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _services(**kwargs):
    from datahub_enterprise.common.config import get_settings
    from datahub_enterprise.services import build_services

    return build_services(get_settings(), **kwargs)


def _serve(app, host: str, port: int, root_path: str = "") -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, root_path=root_path, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("DHE_FORWARDED_ALLOW_IPS", "127.0.0.1"))


def cmd_api(a) -> None:
    from datahub_enterprise.app import create_api_app

    s = _services()
    s.start_background()
    _serve(create_api_app(s, a.modules.split(",")), a.host, a.port, a.root_path)


def cmd_pdp(a) -> None:
    from datahub_enterprise.app import create_pdp_app

    s = _services(with_gateway=False, with_ai=False)
    s.start_background()
    _serve(create_pdp_app(s), a.host, a.port, a.root_path)


def cmd_gateway(a) -> None:
    from datahub_enterprise.agent.dispatcher import ExecutionRequestDispatcher
    from datahub_enterprise.app import create_gateway_app
    import threading

    s = _services(with_ai=False)
    _ = s.gateway.keys.identity  # type: ignore[union-attr]  # fail fast if the key dir is unusable
    if s.graph is not None and not a.no_dispatcher:
        dispatcher = ExecutionRequestDispatcher(s.gateway, s.graph)  # type: ignore[arg-type]

        def loop() -> None:
            while True:
                try:
                    dispatcher.poll_once()
                except Exception as e:  # GMS may still be starting
                    logging.getLogger("dhe.dispatcher").warning("dispatch poll failed: %s", e)
                time.sleep(a.dispatch_interval)

        threading.Thread(target=loop, daemon=True, name="dhe-dispatcher").start()
    _serve(create_gateway_app(s), a.host, a.port, a.root_path)


def cmd_scheduler(a) -> None:
    from datahub_enterprise.quality.scheduler import Scheduler

    s = _services(with_gateway=False, with_ai=False)
    s.start_background()
    Scheduler(s.quality, a.tick, a.workers, a.partition, a.partitions).run_forever()


def _every(seconds: float, fn, name: str) -> None:
    import threading

    log = logging.getLogger(f"dhe.{name}")

    def loop() -> None:
        while True:
            time.sleep(seconds)
            try:
                fn()
            except Exception:
                log.exception("%s failed", name)

    threading.Thread(target=loop, daemon=True, name=name).start()


def cmd_gms_proxy(a) -> None:
    """Masking reverse proxy in the datahub-frontend → GMS path."""
    from datahub_enterprise.gmsproxy.proxy import MaskingService, ProxyIdentity, create_proxy_app

    s = _services(with_gateway=False, with_ai=False)
    s.start_background()
    # Personas/purposes are edited through `dhe api`; pick up changes from the shared database.
    _every(a.reload_interval, s.personas.reload, "persona-reload")
    _every(a.reload_interval, lambda: s.pdp.set_bundle(s.policy_repo.latest() or s.pdp.bundle), "bundle-reload")
    identity = ProxyIdentity(s.settings.active.session_signing_key, a.upstream)
    app = create_proxy_app(a.upstream, MaskingService(s.pdp, s.attributes), identity, a.timeout)
    _serve(app, a.host, a.port)


def cmd_context_miner(a) -> None:
    from datahub_enterprise.context.miner import ContextMiner

    s = _services(with_gateway=False, with_ai=False)
    if s.graph is None:
        sys.exit("DHE_GMS_URL is required for context mining")
    miner = ContextMiner(s.graph, s.context, a.interval)
    if a.once:
        print(json.dumps(miner.run_once(), indent=2))
        return
    miner.run_forever()


def notifier_tick(s) -> dict:
    """One maintenance pass: deliver notifications, expire access, revoke expired grants, purge chats."""
    now_ms = int(time.time() * 1000)
    out: dict = {"delivery": s.notify.dispatch_pending()}
    if s.marketplace is not None:
        out["access"] = s.marketplace.sweep(now_ms)
    expired = s.personas.expired_grants(now_ms)
    for g in expired:
        s.personas.revoke_grant(g["id"], "urn:li:corpuser:dhe-notifier")
    out["grants_expired"] = len(expired)
    return out


def cmd_notifier(a) -> None:
    s = _services(with_gateway=False, with_ai=False)
    log = logging.getLogger("dhe.notifier")
    last_purge = 0.0
    while True:
        try:
            stats = notifier_tick(s)
            if time.time() - last_purge > 3600:
                stats["conversations_purged"] = s.assistant.purge_older_than(
                    s.settings.active.conversation_retention_days)
                last_purge = time.time()
            if any(v for v in stats["delivery"].values()) or stats.get("grants_expired"):
                log.info("notifier tick: %s", stats)
        except Exception:
            log.exception("notifier tick failed")
        if a.once:
            return
        time.sleep(a.interval)


def cmd_mcp(a) -> None:
    from datahub_enterprise.ai.mcp_server import run

    run(_services(with_gateway=False), a.transport, a.host, a.port)


def cmd_actions(a) -> None:
    # `datahub actions` from acryl-datahub-actions discovers DHE actions via entry points.
    sys.exit(subprocess.call(["datahub", "actions", "-c", a.config]))


def cmd_agent_run(a) -> None:
    from datahub_enterprise.agent.worker import main

    main(a.config)


def cmd_bootstrap(a) -> None:
    from datahub_enterprise.governance.certification import bootstrap_mcps

    s = _services(with_gateway=False, with_ai=False)
    mcps = bootstrap_mcps()
    if a.dry_run or s.graph is None:
        print(json.dumps(mcps, indent=2))
        return
    s.graph.emit(mcps)
    print(f"emitted {len(mcps)} bootstrap aspects")


def cmd_audit_verify(_a) -> None:
    s = _services(with_gateway=False, with_ai=False)
    result = s.audit.verify_chain()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["valid"] else 1)


def cmd_policies_validate(a) -> None:
    from datahub_enterprise.governance.pdp import load_bundle_from_dir

    bundle, rbac = load_bundle_from_dir(a.dir)
    print(json.dumps({"valid": True, "policies": len(bundle.policies), "roles": len(rbac.roles)}))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dhe", description="DataHub Enterprise extension layer")
    sub = p.add_subparsers(dest="command", required=True)

    def http(name: str, port: int, fn, help_: str):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=port)
        sp.add_argument("--root-path", default=os.environ.get("DHE_ROOT_PATH", ""))
        sp.set_defaults(fn=fn)
        return sp

    api = http("api", 8800, cmd_api, "control-plane API (quality, governance, ai, agent admin)")
    api.add_argument("--modules", default="quality,governance,ai,agents,collab,marketplace,context,notify,embed")
    http("pdp", 8810, cmd_pdp, "policy decision point")
    gw = http("gateway", 8820, cmd_gateway, "remote agent gateway")
    gw.add_argument("--no-dispatcher", action="store_true")
    gw.add_argument("--dispatch-interval", type=float, default=10.0)

    sch = sub.add_parser("scheduler", help="data-quality monitor scheduler")
    sch.add_argument("--tick", type=float, default=15.0)
    sch.add_argument("--workers", type=int, default=8)
    sch.add_argument("--partition", type=int, default=int(os.environ.get("DHE_SCHEDULER_PARTITION", 0)))
    sch.add_argument("--partitions", type=int, default=int(os.environ.get("DHE_SCHEDULER_PARTITIONS", 1)))
    sch.set_defaults(fn=cmd_scheduler)

    gp = http("gms-proxy", 8890, cmd_gms_proxy, "PII-masking reverse proxy between datahub-frontend and GMS")
    gp.add_argument("--upstream", default=os.environ.get("DHE_GMS_URL", "http://localhost:8080"))
    gp.add_argument("--timeout", type=float, default=120.0)
    gp.add_argument("--reload-interval", type=float, default=30.0)

    cm = sub.add_parser("context-miner", help="context-mining worker (queries, dbt, BI, runs → findings)")
    cm.add_argument("--interval", type=int, default=300)
    cm.add_argument("--once", action="store_true")
    cm.set_defaults(fn=cmd_context_miner)

    nt = sub.add_parser("notifier", help="notification delivery + access/grant expiry sweeps")
    nt.add_argument("--interval", type=float, default=10.0)
    nt.add_argument("--once", action="store_true")
    nt.set_defaults(fn=cmd_notifier)

    mcp = sub.add_parser("mcp", help="Model Context Protocol server")
    mcp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8830)
    mcp.set_defaults(fn=cmd_mcp)

    act = sub.add_parser("actions", help="run a datahub-actions pipeline with DHE actions")
    act.add_argument("-c", "--config", required=True)
    act.set_defaults(fn=cmd_actions)

    agent = sub.add_parser("agent", help="in-VPC remote ingestion agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    run = agent_sub.add_parser("run")
    run.add_argument("--config", default=os.environ.get("DHE_AGENT_CONFIG", "/etc/dhe-agent/agent.yml"))
    run.set_defaults(fn=cmd_agent_run)

    boot = sub.add_parser("bootstrap", help="create DHE structured properties, tags and PII glossary terms")
    boot.add_argument("--dry-run", action="store_true")
    boot.set_defaults(fn=cmd_bootstrap)

    audit = sub.add_parser("audit", help="audit log tools")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_sub.add_parser("verify").set_defaults(fn=cmd_audit_verify)

    pol = sub.add_parser("policies", help="policy bundle tools")
    pol_sub = pol.add_subparsers(dest="policies_command", required=True)
    v = pol_sub.add_parser("validate")
    v.add_argument("dir")
    v.set_defaults(fn=cmd_policies_validate)
    return p


def main(argv=None) -> None:
    _setup_logging()
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
