"""``cairn`` command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("CAIRN_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _services(**kwargs):
    from cairn.common.config import get_settings
    from cairn.services import build_services

    return build_services(get_settings(), **kwargs)


def _serve(app, host: str, port: int, root_path: str = "") -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, root_path=root_path, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("CAIRN_FORWARDED_ALLOW_IPS", "127.0.0.1"))


def cmd_api(a) -> None:
    from cairn.app import DEFAULT_MODULES, create_api_app

    s = _services()
    s.start_background(bootstrap_catalog=True)
    _serve(create_api_app(s, a.modules.split(",") if a.modules else DEFAULT_MODULES), a.host, a.port, a.root_path)


def cmd_pdp(a) -> None:
    from cairn.app import create_pdp_app

    s = _services(with_gateway=False, with_ai=False)
    s.start_background()
    _serve(create_pdp_app(s), a.host, a.port, a.root_path)


def cmd_gateway(a) -> None:
    from cairn.agent.dispatcher import ExecutionRequestDispatcher
    from cairn.app import create_gateway_app
    import threading

    s = _services(with_ai=False)
    _ = s.gateway.keys.identity  # type: ignore[union-attr]  # fail fast if the key dir is unusable
    if s.graph is not None and not a.no_dispatcher:
        dispatcher = ExecutionRequestDispatcher(s.gateway, s.graph)  # type: ignore[arg-type]

        def loop() -> None:
            while True:
                try:
                    dispatcher.poll_once()
                except Exception as e:  # Cairn Core may still be starting
                    logging.getLogger("cairn.dispatcher").warning("dispatch poll failed: %s", e)
                time.sleep(a.dispatch_interval)

        threading.Thread(target=loop, daemon=True, name="cairn-dispatcher").start()
    _serve(create_gateway_app(s), a.host, a.port, a.root_path)


def cmd_scheduler(a) -> None:
    from cairn.quality.scheduler import Scheduler

    from cairn.common.metrics import serve_worker_metrics

    s = _services(with_gateway=False, with_ai=False)
    s.start_background()
    serve_worker_metrics()
    Scheduler(s.quality, a.tick, a.workers, a.partition, a.partitions).run_forever()


def cmd_core_proxy(a) -> None:
    """Masking reverse proxy in the cairn-web → Cairn Core path."""
    from cairn.coreproxy.proxy import MaskingService, ProxyIdentity, create_proxy_app

    s = _services(with_gateway=False, with_ai=False)
    s.start_background(layer_reload_interval=a.reload_interval)
    identity = ProxyIdentity(s.settings.active.session_signing_key, a.upstream)
    app = create_proxy_app(a.upstream, MaskingService(s.pdp, s.attributes), identity, a.timeout)
    _serve(app, a.host, a.port)


def cmd_context_miner(a) -> None:
    from cairn.context.miner import ContextMiner

    s = _services(with_gateway=False, with_ai=False)
    if s.graph is None:
        sys.exit("CAIRN_CORE_URL is required for context mining")
    miner = ContextMiner(s.graph, s.context, a.interval)
    if a.once:
        print(json.dumps(miner.run_once(), indent=2))
        return
    from cairn.common.metrics import serve_worker_metrics

    serve_worker_metrics()
    miner.run_forever()


def notifier_tick(s) -> dict:
    """One maintenance pass: deliver notifications, expire access, revoke expired grants, purge chats."""
    now_ms = int(time.time() * 1000)
    out: dict = {"delivery": s.notify.dispatch_pending()}
    if s.marketplace is not None:
        out["access"] = s.marketplace.sweep(now_ms)
        if s.marketplace.provisioner is not None:
            out["provisioning"] = s.marketplace.retry_provisioning()
    expired = s.personas.expired_grants(now_ms)
    for g in expired:
        s.personas.revoke_grant(g["id"], "urn:li:corpuser:cairn-notifier")
    out["grants_expired"] = len(expired)
    if s.forms is not None:
        out["forms"] = forms_tick(s, now_ms)
    if s.reports is not None and now_ms - _SWEEP_STATE["reports_ms"] >= 3_600_000:
        _SWEEP_STATE["reports_ms"] = now_ms
        if s.reports.due(now_ms):           # once a day: the first hourly check after midnight UTC
            if s.costs is not None:         # yesterday's warehouse costs first, so the day is complete
                from cairn.reports.costs import _day
                out["costs"] = s.costs.collect_day(_day(now_ms - 86_400_000))
            out["reports"] = s.reports.compute(now_ms)["day"]
    if s.automations is not None:
        out["automation_events"] = s.automations.process_events()
    if s.automations is not None and now_ms - _SWEEP_STATE["automations_ms"] >= 60_000:
        _SWEEP_STATE["automations_ms"] = now_ms
        out["automations"] = s.automations.sweep(now_ms)
    if s.contracts is not None and now_ms - _SWEEP_STATE["contracts_ms"] >= 300_000:
        _SWEEP_STATE["contracts_ms"] = now_ms
        out["contracts"] = s.contracts.refresh_all()
        if s.publisher is not None:
            out["outdated_publications"] = s.publisher.sweep()
    if s.sources is not None and now_ms - _SWEEP_STATE["sources_ms"] >= 300_000:
        _SWEEP_STATE["sources_ms"] = now_ms
        try:
            out["sources"] = s.sources.check_health(now_ms)
        except Exception as e:  # noqa: BLE001 - Cairn Core briefly away: try again next time
            out["sources"] = {"error": str(e)[:200]}
    if s.slos is not None and now_ms - _SWEEP_STATE["slos_ms"] >= 3_600_000:
        _SWEEP_STATE["slos_ms"] = now_ms
        out["slos"] = s.slos.check(now_ms)
    if s.privacy is not None and now_ms - _SWEEP_STATE["privacy_ms"] >= 86_400_000:
        _SWEEP_STATE["privacy_ms"] = now_ms
        try:
            out["privacy"] = {"retention": s.privacy.check_retention(), "reminded": s.privacy.remind(now_ms)}
        except Exception as e:  # noqa: BLE001 - warehouses may be briefly away
            out["privacy"] = {"error": str(e)[:200]}
    if s.knowledge is not None and s.knowledge.sources and now_ms - _SWEEP_STATE["knowledge_ms"] >= 86_400_000:
        _SWEEP_STATE["knowledge_ms"] = now_ms
        out["knowledge"] = s.knowledge.sync()
    if s.traces is not None and now_ms - _SWEEP_STATE["ai_lineage_ms"] >= 3_600_000:
        _SWEEP_STATE["ai_lineage_ms"] = now_ms
        from cairn.ai.lineage_publish import publish

        try:
            out["ai_lineage"] = publish(s)
        except Exception as e:  # noqa: BLE001 - the catalog may be briefly away
            out["ai_lineage"] = {"error": str(e)[:200]}
    if s.tag_sync is not None and now_ms - _SWEEP_STATE["synced_ms"] >= TAG_SYNC_EVERY_MS:
        _SWEEP_STATE["synced_ms"] = now_ms
        out["tag_sync"] = s.tag_sync.sweep()
    return out


TAG_SYNC_EVERY_MS = int(os.environ.get("CAIRN_TAG_SYNC_EVERY_SECONDS", "900")) * 1000
_SWEEP_STATE = {"synced_ms": 0, "contracts_ms": 0, "automations_ms": 0, "reports_ms": 0, "sources_ms": 0,
                "ai_lineage_ms": 0, "knowledge_ms": 0,
                "privacy_ms": 0, "slos_ms": 0}


_FORMS_STATE = {"assigned_ms": 0, "reminded_ms": 0}


def forms_tick(s, now_ms: int, assign_every_ms: int = 3_600_000, remind_every_ms: int = 86_400_000) -> dict:
    """Hourly: give new matching assets their form tasks. Daily: remind assignees of overdue ones."""
    from cairn.notify.hub import Event

    out: dict = {}
    if now_ms - _FORMS_STATE["assigned_ms"] >= assign_every_ms:
        _FORMS_STATE["assigned_ms"] = now_ms
        for f in s.forms.list():
            try:
                r = s.forms.assign(f["id"], "urn:li:corpuser:cairn-notifier")
                if r["assigned"] or r["withdrawn"]:
                    out[f["id"]] = {"assigned": len(r["assigned"]), "withdrawn": len(r["withdrawn"])}
            except Exception:
                logging.getLogger("cairn.notifier").exception("form assignment failed for %s", f["id"])
    if now_ms - _FORMS_STATE["reminded_ms"] >= remind_every_ms:
        _FORMS_STATE["reminded_ms"] = now_ms
        by_person: dict = {}
        for t in s.forms.overdue(now_ms):
            for person in t["assignees"]:
                by_person.setdefault(person, []).append(t)
        for person, items in by_person.items():
            forms = sorted({t["form_name"] for t in items})
            s.notify.publish(Event(type="form.due", title=f"{len(items)} form task(s) are overdue",
                                   body="Still waiting for your answers on: " + ", ".join(forms),
                                   recipients=[person]))
        out["reminded"] = len(by_person)
    return out


NOTIFIER_LOCK_KEY = 0x0D4E_0101     # leader lock: one notifier delivers at a time


def cmd_notifier(a) -> None:
    from cairn.common.db import advisory_lock
    from cairn.common.metrics import serve_worker_metrics, worker_cycle

    s = _services(with_gateway=False, with_ai=False)
    log = logging.getLogger("cairn.notifier")
    serve_worker_metrics()
    last_purge = 0.0
    while True:
        try:
            # One notifier works at a time (no double deliveries); more replicas are hot standbys.
            with advisory_lock(s.engine, NOTIFIER_LOCK_KEY) as leader:
                if not leader:
                    worker_cycle("notifier", True)
                    if a.once:
                        return
                    time.sleep(a.interval)
                    continue
                stats = notifier_tick(s)
            worker_cycle("notifier", True)
            if time.time() - last_purge > 3600:
                stats["conversations_purged"] = s.assistant.purge_older_than(
                    s.settings.active.conversation_retention_days)
                last_purge = time.time()
            if any(v for v in stats["delivery"].values()) or stats.get("grants_expired"):
                log.info("notifier tick: %s", stats)
        except Exception:
            worker_cycle("notifier", False)
            log.exception("notifier tick failed")
        if a.once:
            return
        time.sleep(a.interval)


def cmd_mcp(a) -> None:
    from cairn.ai.mcp_server import run

    run(_services(with_gateway=False), a.transport, a.host, a.port)


def cmd_actions(a) -> None:
    # `datahub actions` from acryl-datahub-actions discovers Cairn actions via entry points.
    sys.exit(subprocess.call(["datahub", "actions", "-c", a.config]))


def cmd_agent_run(a) -> None:
    from cairn.agent.worker import main

    main(a.config)


def cmd_bootstrap(a) -> None:
    from cairn.governance.certification import bootstrap_mcps

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


def cmd_audit_export(a) -> None:
    """Write the audit log in chain order; ``--follow`` keeps writing new entries (for log shippers)."""
    from cairn.common.audit import AuditLog, export_lines
    from cairn.common.config import get_settings
    from cairn.common.db import get_engine

    audit = AuditLog(get_engine(get_settings().database_url))
    out = open(a.out, "a" if a.follow else "w") if a.out else sys.stdout
    cursor, header = a.after_seq, True
    try:
        while True:
            batch = list(audit.export(cursor, a.since, a.until, a.category))
            if batch or header:
                out.writelines(export_lines(batch, a.format, header=header))
                out.flush()
                header = False
            cursor = batch[-1]["seq"] if batch else cursor
            if not a.follow:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if a.out:
            out.close()


def cmd_audit_verify_file(a) -> None:
    from cairn.common.audit import verify_entries

    with open(a.path) as f:
        result = verify_entries(json.loads(line) for line in f if line.strip())
    if a.after_head and result.get("valid") and result.get("entries_checked") and result["starts_after"] != a.after_head:
        result = {**result, "valid": False, "reason": "export does not continue from the given head"}
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["valid"] else 1)


def _load_doc(path: str) -> dict:
    import yaml

    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def cmd_contract_check(a) -> None:
    """Offline, for CI: does the proposed dbt model (or a columns file) keep the contract? Exit 1 if not."""
    from cairn.quality.contracts import ContractSpec, check_proposed, dbt_columns

    spec = ContractSpec.model_validate(_load_doc(a.contract))
    if a.manifest:
        if not a.model:
            raise SystemExit("cairn contract check: --model is required with --manifest")
        with open(a.manifest) as fh:
            columns = dbt_columns(json.load(fh), a.model)
    else:
        columns = _load_doc(a.columns)
    violations = check_proposed(spec, columns)
    if violations:
        print(f"contract {spec.version} for {spec.dataset} would break:")
        for v in violations:
            print(f"  - {v}")
        raise SystemExit(1)
    print(f"contract {spec.version} for {spec.dataset}: the proposed schema keeps it ({len(columns)} columns)")


def cmd_contract_publish(a) -> None:
    """Publish contract files through the API (CAIRN_API_URL, CAIRN_TOKEN)."""
    import httpx

    failed = 0
    for path in a.files:
        r = httpx.put(f"{a.api.rstrip('/')}/quality/contracts", json=_load_doc(path), timeout=60,
                      headers={"Authorization": f"Bearer {a.token}"} if a.token else {})
        if r.status_code >= 400:
            failed += 1
            print(f"{path}: {r.status_code} {r.text[:500]}")
        else:
            out = r.json()
            print(f"{path}: {out['dataset']} v{out['version']} {out['status']}")
    if failed:
        raise SystemExit(1)


def cmd_db(a) -> None:
    from cairn import migrations
    from cairn.common.config import get_settings
    from cairn.common.db import get_engine

    engine = get_engine(get_settings().database_url)
    if a.db_command == "upgrade":
        print(f"schema at {migrations.upgrade(engine)}")
    elif a.db_command == "current":
        print(json.dumps({"current": migrations.current(engine), "head": migrations.head()}))
    elif a.db_command == "check":
        diffs = migrations.check(engine)
        cur, head = migrations.current(engine), migrations.head()
        print(json.dumps({"current": cur, "head": head, "differences": [str(d) for d in diffs]}, indent=2))
        sys.exit(0 if not diffs and cur == head else 1)


def cmd_export_metadata(a) -> None:
    from cairn.export.metadata import MetadataExporter

    s = _services(with_gateway=False, with_ai=False)
    exporter = MetadataExporter(s.graph, s.quality.store, s.engine)
    if a.format == "iceberg":
        result = exporter.write_iceberg(a.catalog, a.namespace)
    else:
        if not a.to:
            sys.exit("--to is required for parquet (a path, s3://, gs:// or abfs:// URI)")
        result = exporter.write_parquet(a.to, a.keep_days)
    print(json.dumps(result.as_dict(), indent=2))


def cmd_source(a) -> None:
    """SDK connectors: ``check`` runs the test kit on a connector folder (no database: for its own CI);
    ``run`` ingests once from a config file (``--dry-run`` prints the metadata instead of sending it)."""
    import yaml

    from cairn.sdk import manifest as mf
    from cairn.sdk.source import load
    from cairn.sdk.testkit import check

    m = mf.read(a.path)
    folder = a.path if os.path.isdir(a.path) else os.path.dirname(a.path)
    for extra in (folder, os.path.join(folder, "src")):          # a connector checked out, not installed
        if os.path.isdir(extra) and extra not in sys.path:
            sys.path.insert(0, extra)
    source = load(m.entrypoint)()
    if a.source_command == "check":
        report = {"name": m.name, "version": m.version, **check(source, m.example_config)}
        print(json.dumps(report, indent=2))
        if not report["ok"]:
            sys.exit(1)
        return
    with open(a.config) as fh:
        config = yaml.safe_load(fh) or {}
    from cairn.sdk.bridge import proposals

    mcps = proposals({"source": m.entrypoint, "config": config, "env": a.env})
    if a.dry_run:
        print(json.dumps(mcps, indent=2))
        return
    s = _services(with_gateway=False, with_ai=False)
    if s.graph is None:
        sys.exit("CAIRN_CORE_URL is required to send metadata")
    s.graph.emit(mcps)
    print(json.dumps({"sent": len(mcps), "datasets": sum(1 for x in mcps if x["aspectName"] == "status")}))


def cmd_extension(a) -> None:
    from cairn.extensions.api import parse_manifest
    from cairn.extensions.service import ExtensionError

    def manifest_text(path: str) -> str:
        p = os.path.join(path, "cairn-extension.yaml") if os.path.isdir(path) else path
        with open(p) as f:
            return f.read()

    try:
        if a.extension_command == "validate":            # no database: usable in an extension's own CI
            m = parse_manifest(manifest_text(a.path))
            print(json.dumps({"valid": True, "name": m.name, "version": m.version,
                              "permissions": m.permissions}, indent=2))
            return
        s = _services(with_gateway=False, with_ai=False)
        actor = "urn:li:corpuser:cairn-cli"
        if a.extension_command == "install":
            print(json.dumps(s.extensions.install(parse_manifest(manifest_text(a.path)), actor), indent=2))
        elif a.extension_command == "list":
            print(json.dumps(s.extensions.list(), indent=2))
        elif a.extension_command == "remove":
            s.extensions.remove(a.name, actor)
        elif a.extension_command == "rotate":
            print(json.dumps(s.extensions.rotate(a.name, actor), indent=2))
    except ExtensionError as e:
        sys.exit(str(e))


def cmd_licence(a) -> None:
    from cairn.common.audit import AuditLog
    from cairn.common.config import get_settings
    from cairn.common.db import get_engine
    from cairn.common.licence import LicenceError, from_env, verify

    if a.licence_command == "verify":                      # no database needed
        try:
            print(json.dumps(verify(open(a.file).read(), tenant=os.environ.get("CAIRN_TENANT_ID")).public(), indent=2))
        except LicenceError as e:
            sys.exit(f"invalid: {e}")
        return
    engine = get_engine(get_settings().database_url)
    svc = from_env(engine, AuditLog(engine))
    if a.licence_command == "install":
        try:
            svc.install(open(a.file).read(), "urn:li:corpuser:cairn-cli")
        except LicenceError as e:
            sys.exit(f"not installed: {e}")
    print(json.dumps(svc.state(), indent=2))


def cmd_sso_check(a) -> None:
    from cairn.common import sso

    env = os.environ.get
    report = sso.check(a.discovery_url or env("CAIRN_SSO_DISCOVERY_URL", ""), a.client_id or env("CAIRN_SSO_CLIENT_ID", ""),
                       env("CAIRN_SSO_CLIENT_SECRET", ""), a.base_url or env("CAIRN_PUBLIC_URL", ""),
                       env("CAIRN_SSO_CLIENT_AUTH_METHOD", "client_secret_basic"))
    if a.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(f"Redirect URI to register with the identity provider: {report.callback_url}\n")
        for c in report.checks:
            mark = "ok  " if c.ok else ("warn" if c.warning else "FAIL")
            print(f"  [{mark}] {c.name}: {c.detail}")
        print("\nSingle sign-on settings look right." if report.ok else "\nFix the failed checks, then run again.")
    sys.exit(0 if report.ok else 1)


def cmd_policies_validate(a) -> None:
    from cairn.governance.pdp import load_bundle_from_dir

    bundle, rbac = load_bundle_from_dir(a.dir)
    print(json.dumps({"valid": True, "policies": len(bundle.policies), "roles": len(rbac.roles)}))


def run_policy_tests(bundle_dir: str, tests_path: str) -> list[dict]:
    """Evaluate decision test cases (YAML) against a policy bundle; returns one result per case."""
    import pathlib

    import yaml

    from cairn.governance.attributes import AttributeResolver
    from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, load_bundle_from_dir

    bundle, rbac = load_bundle_from_dir(bundle_dir)
    pdp = PolicyDecisionPoint(AttributeResolver(None), mode="additive", root_users=[], cache_ttl_seconds=0)
    pdp.set_bundle(bundle)
    pdp.set_rbac(rbac)
    path = pathlib.Path(tests_path)
    files = sorted(path.glob("*.y*ml")) if path.is_dir() else [path]
    results = []
    for f in files:
        doc = yaml.safe_load(f.read_text()) or {}
        defaults = doc.get("defaults") or {}
        for case in doc.get("cases") or []:
            req = AuthorizeRequest(**{**defaults, **case["request"]})
            got = pdp.decide(req, explain=True)
            problems = []
            if got.decision != case["expect"]:
                problems.append(f"decision {got.decision}, expected {case['expect']} ({got.reason})")
            for pid in case.get("expect_policies") or []:
                if pid not in got.policy_ids:
                    problems.append(f"policy {pid} did not apply (applied: {got.policy_ids})")
            kinds = [o.type for o in got.obligations]
            for kind in case.get("expect_obligations") or []:
                if kind not in kinds:
                    problems.append(f"obligation {kind} missing (got {kinds})")
            if case.get("expect_no_obligations") and kinds:
                problems.append(f"unexpected obligations {kinds}")
            results.append({"file": f.name, "name": case["name"], "ok": not problems, "problems": problems})
    return results


def cmd_policies_test(a) -> None:
    results = run_policy_tests(a.dir, a.tests)
    failed = [r for r in results if not r["ok"]]
    for r in results:
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['file']}: {r['name']}")
        for p in r["problems"]:
            print(f"       {p}")
    print(f"{len(results) - len(failed)}/{len(results)} decision tests passed")
    sys.exit(1 if failed or not results else 0)


def cmd_ai_eval(a) -> None:
    from cairn.ai import evals

    provider = embedder = None
    if a.live:
        from cairn.ai.embeddings import build_embedder
        from cairn.ai.llm import build_provider
        from cairn.common.config import get_settings

        provider = build_provider(get_settings())
        embedder = build_embedder(get_settings())
        if provider is None:
            sys.exit("cairn: --live needs an LLM provider (set CAIRN_LLM_PROVIDER=anthropic or ollama)")
    try:
        cases = evals.load_cases(a.cases)
    except (evals.EvalCaseError, OSError) as e:
        sys.exit(f"cairn: {e}")
    if a.only:
        cases = [c for c in cases if any(o in c["id"] for o in a.only)]
    results = evals.run_cases(cases, provider, embedder=embedder)
    summary = evals.summarize(results)
    for r in results:
        cost = f"  {r['tokens']} tok ${r['cost_usd']:.4f} {r['latency_ms']:.0f}ms" if a.live else ""
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['feature']:<8} {r['id']}{cost}")
        for p in r["problems"]:
            print(f"       {p}")
    print(f"{summary['passed']}/{summary['cases']} AI eval cases passed"
          + (f"; {summary['tokens']} tokens, est. ${summary['cost_usd']:.4f}" if a.live else ""))
    if "text2sql" in summary:
        t = summary["text2sql"]
        print(f"text to SQL: {t['accuracy_with_context']:.0%} correct with Cairn context, "
              f"{t['accuracy_without_context']:.0%} without ({t['cases']} questions)")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"mode": "live" if a.live else "offline", "provider": getattr(provider, "name", "scripted"),
                       "summary": summary, "results": results}, f, indent=2)
    sys.exit(0 if results and not summary["failed"] else 1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cairn", description="Cairn extension layer")
    sub = p.add_subparsers(dest="command", required=True)

    def http(name: str, port: int, fn, help_: str):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=port)
        sp.add_argument("--root-path", default=os.environ.get("CAIRN_ROOT_PATH", ""))
        sp.set_defaults(fn=fn)
        return sp

    api = http("api", 8800, cmd_api, "control-plane API (quality, governance, ai, agent admin)")
    api.add_argument("--modules", default=None, help="comma-separated API modules (default: all)")
    http("pdp", 8810, cmd_pdp, "policy decision point")
    gw = http("gateway", 8820, cmd_gateway, "remote agent gateway")
    gw.add_argument("--no-dispatcher", action="store_true")
    gw.add_argument("--dispatch-interval", type=float, default=10.0)

    sch = sub.add_parser("scheduler", help="data-quality monitor scheduler")
    sch.add_argument("--tick", type=float, default=15.0)
    sch.add_argument("--workers", type=int, default=8)
    sch.add_argument("--partition", type=int, default=int(os.environ.get("CAIRN_SCHEDULER_PARTITION", 0)))
    sch.add_argument("--partitions", type=int, default=int(os.environ.get("CAIRN_SCHEDULER_PARTITIONS", 1)))
    sch.set_defaults(fn=cmd_scheduler)

    gp = http("core-proxy", 8890, cmd_core_proxy, "PII-masking reverse proxy between cairn-web and Cairn Core")
    gp.add_argument("--upstream", default=os.environ.get("CAIRN_CORE_URL", "http://localhost:8080"))
    gp.add_argument("--timeout", type=float, default=120.0)
    gp.add_argument("--reload-interval", type=float, default=10.0)

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

    act = sub.add_parser("actions", help="run a cairn-executor pipeline with Cairn actions")
    act.add_argument("-c", "--config", required=True)
    act.set_defaults(fn=cmd_actions)

    agent = sub.add_parser("agent", help="in-VPC remote ingestion agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    run = agent_sub.add_parser("run")
    run.add_argument("--config", default=os.environ.get("CAIRN_AGENT_CONFIG", "/etc/cairn-agent/agent.yml"))
    run.set_defaults(fn=cmd_agent_run)

    boot = sub.add_parser("bootstrap", help="create Cairn structured properties, tags and PII glossary terms")
    boot.add_argument("--dry-run", action="store_true")
    boot.set_defaults(fn=cmd_bootstrap)

    audit = sub.add_parser("audit", help="audit log tools")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_sub.add_parser("verify", help="verify the hash chain in the database").set_defaults(fn=cmd_audit_verify)
    ex = audit_sub.add_parser("export", help="export entries in chain order (JSON Lines or CSV)")
    ex.add_argument("--after-seq", type=int, default=0, help="resume after this sequence number")
    ex.add_argument("--since", help="ISO-8601 timestamp (inclusive)")
    ex.add_argument("--until", help="ISO-8601 timestamp (exclusive)")
    ex.add_argument("--category")
    ex.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    ex.add_argument("--out", help="file to write (default stdout)")
    ex.add_argument("--follow", action="store_true", help="keep exporting new entries")
    ex.add_argument("--interval", type=float, default=5.0, help="seconds between polls with --follow")
    ex.set_defaults(fn=cmd_audit_export)
    vf = audit_sub.add_parser("verify-file", help="verify a JSON Lines export without the database")
    vf.add_argument("path")
    vf.add_argument("--after-head", help="head hash of the previous export: proves nothing is missing between")
    vf.set_defaults(fn=cmd_audit_verify_file)

    db = sub.add_parser("db", help="control-plane database schema (migrations)")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    for name, help_ in (("upgrade", "migrate to the latest revision"), ("current", "show the schema revision"),
                        ("check", "fail if the schema differs from the models or is not at head")):
        db_sub.add_parser(name, help=help_).set_defaults(fn=cmd_db)

    ex = sub.add_parser("export", help="export catalog metadata to the customer's storage")
    ex_sub = ex.add_subparsers(dest="export_command", required=True)
    em = ex_sub.add_parser("metadata", help="assets, columns, owners, labels, lineage, monitors, grants")
    em.add_argument("--to", help="parquet: a path, or an s3://, gs:// or abfs:// URI")
    em.add_argument("--format", choices=("parquet", "iceberg"), default="parquet")
    em.add_argument("--keep-days", type=int, help="parquet: delete daily partitions older than this")
    em.add_argument("--catalog", default="default", help="iceberg: catalog name in the pyiceberg configuration")
    em.add_argument("--namespace", default="cairn", help="iceberg: namespace of the tables")
    em.set_defaults(fn=cmd_export_metadata)

    xt = sub.add_parser("extension", help="extensions: validate a manifest, install, list, rotate, remove")
    xt_sub = xt.add_subparsers(dest="extension_command", required=True)
    for name, help_ in (("validate", "check a manifest (file or folder) without installing it"),
                        ("install", "install or upgrade from a manifest (prints the token once)")):
        x = xt_sub.add_parser(name, help=help_)
        x.add_argument("path")
        x.set_defaults(fn=cmd_extension)
    xt_sub.add_parser("list", help="installed extensions").set_defaults(fn=cmd_extension)
    for name, help_ in (("remove", "uninstall"), ("rotate", "new token and signing secret")):
        x = xt_sub.add_parser(name, help=help_)
        x.add_argument("name")
        x.set_defaults(fn=cmd_extension)

    sc = sub.add_parser("source", help="SDK connectors: check one with the test kit, or run it once")
    sc_sub = sc.add_subparsers(dest="source_command", required=True)
    x = sc_sub.add_parser("check", help="run the conformance checks on a connector folder (cairn-source.yaml)")
    x.add_argument("path")
    x.set_defaults(fn=cmd_source)
    x = sc_sub.add_parser("run", help="ingest once with a config file")
    x.add_argument("path")
    x.add_argument("--config", required=True)
    x.add_argument("--env", default="PROD")
    x.add_argument("--dry-run", action="store_true", help="print the metadata instead of sending it")
    x.set_defaults(fn=cmd_source)

    lc = sub.add_parser("licence", help="licence key: show the state, verify or install a key")
    lc_sub = lc.add_subparsers(dest="licence_command", required=True)
    lc_sub.add_parser("show", help="the installed licence, seats and status").set_defaults(fn=cmd_licence)
    for name, help_ in (("verify", "check a key file without installing it"), ("install", "install a key file")):
        x = lc_sub.add_parser(name, help=help_)
        x.add_argument("file")
        x.set_defaults(fn=cmd_licence)

    ct = sub.add_parser("contract", help="data contracts: check in CI, publish")
    ct_sub = ct.add_subparsers(dest="contract_command", required=True)
    cc = ct_sub.add_parser("check", help="fail if a proposed dbt model or columns file breaks a contract.yaml")
    cc.add_argument("contract")
    src = cc.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", help="dbt target/manifest.json")
    src.add_argument("--columns", help="YAML/JSON file: {column: {type, nullable}}")
    cc.add_argument("--model", help="dbt model name or unique id (with --manifest)")
    cc.set_defaults(fn=cmd_contract_check)
    cpub = ct_sub.add_parser("publish", help="publish contract files (a new version for changed terms)")
    cpub.add_argument("files", nargs="+")
    cpub.add_argument("--api", default=os.environ.get("CAIRN_API_URL", "http://localhost:8000/cairn/api"))
    cpub.add_argument("--token", default=os.environ.get("CAIRN_TOKEN"))
    cpub.set_defaults(fn=cmd_contract_publish)

    sso = sub.add_parser("sso", help="single sign-on (OpenID Connect) setup")
    sso_sub = sso.add_subparsers(dest="sso_command", required=True)
    sc = sso_sub.add_parser("check", help="validate CAIRN_SSO_* settings against the identity provider")
    sc.add_argument("--discovery-url", default="")
    sc.add_argument("--client-id", default="")
    sc.add_argument("--base-url", default="", help="public URL of Cairn (default CAIRN_PUBLIC_URL)")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(fn=cmd_sso_check)

    pol = sub.add_parser("policies", help="policy bundle tools")
    pol_sub = pol.add_subparsers(dest="policies_command", required=True)
    v = pol_sub.add_parser("validate")
    v.add_argument("dir")
    v.set_defaults(fn=cmd_policies_validate)
    t = pol_sub.add_parser("test", help="run decision test cases against a policy bundle")
    t.add_argument("dir", help="policy bundle directory")
    t.add_argument("tests", help="YAML file or directory of decision test cases")
    t.set_defaults(fn=cmd_policies_test)

    ai = sub.add_parser("ai", help="AI feature tools")
    ai_sub = ai.add_subparsers(dest="ai_command", required=True)
    ev = ai_sub.add_parser("eval", help="run golden-case evaluations of the AI features")
    ev.add_argument("cases", nargs="?", default=os.environ.get("CAIRN_AI_EVALS", "/etc/cairn/config/ai-evals"),
                    help="YAML file or directory of eval cases")
    ev.add_argument("--live", action="store_true", help="call the configured model instead of scripted replies")
    ev.add_argument("--only", action="append", help="run cases whose id contains this text (repeatable)")
    ev.add_argument("--json", help="also write the report as JSON to this path")
    ev.set_defaults(fn=cmd_ai_eval)
    return p


def main(argv=None) -> None:
    from cairn.common.config import load_secret_files

    _setup_logging()
    args = build_parser().parse_args(argv)
    try:
        load_secret_files()
    except RuntimeError as e:
        sys.exit(f"cairn: {e}")
    args.fn(args)


if __name__ == "__main__":
    main()
