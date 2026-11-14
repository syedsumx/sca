"""Automatic failover (queue row 24): when the primary region is lost, the standby takes over by itself.

``cairn dr watch --config failover.yaml`` runs in the standby region (it must not share the primary's fate). Every
``interval_s`` it probes the primary's checks. Once **every** primary check has failed ``failures`` times in a row
(a region lost, not one component slow), and **every** standby check passes, it fails over:

1. **Promotes** each standby PostgreSQL (``pg_promote``): the catalog's and Cairn's own database, or one cluster
   holding both. It waits until each one takes writes.
2. **Points DNS at the standby.** It sends one RFC 2136 update per record (BIND, PowerDNS, Knot and Windows DNS take
   them), signed with a TSIG key, and reads each record back from the server.
3. **Writes the state file.** With the state file present the watcher never acts again, and it never fails back on
   its own: a primary that comes back is not trusted until an operator has rebuilt it as the new standby and
   removed the file.

The order matters. Clients are only sent to databases that already take writes. A standby that is not healthy is
never promoted: the watcher reports it and keeps watching, so the region stays down rather than splitting in two.

Config (YAML or JSON)::

    interval_s: 10
    failures: 3                    # consecutive ticks with every primary check failing
    timeout_s: 3
    state_file: /var/lib/cairn/failover.json
    primary_checks: ["https://api.eu-1.example.com/health", "tcp://pg.eu-1.internal:5432"]
    standby_checks: ["tcp://store.eu-2.internal:9000"]
    standby_databases:             # each a PostgreSQL URL, read from a file (it holds a password)
      - url_file: /etc/cairn/standby-db-url
    dns:
      server: ns1.example.com
      port: 53
      zone: example.com.
      key_name: cairn-failover.
      key_algorithm: hmac-sha256
      key_secret_file: /etc/cairn/tsig-secret
      records:
        - { name: api, type: A, value: 203.0.113.20, ttl: 60 }
        - { name: lake, type: CNAME, value: lake.eu-2.example.com., ttl: 60 }
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger("cairn.dr.failover")
RECORD_TYPES = {"A", "AAAA", "CNAME"}
ALGORITHMS = {"hmac-sha256", "hmac-sha384", "hmac-sha512"}


class FailoverError(Exception):
    pass


def _secret(path: str, what: str) -> str:
    try:
        value = open(path).read().strip()
    except OSError as e:
        raise FailoverError(f"{what}: cannot read {path} ({e.strerror})") from e
    if not value:
        raise FailoverError(f"{what}: {path} is empty")
    return value


@dataclass
class Record:
    name: str
    type: str
    value: str
    ttl: int = 60


@dataclass
class Dns:
    server: str
    zone: str
    key_name: str
    key_secret: str
    key_algorithm: str = "hmac-sha256"
    port: int = 53
    records: list[Record] = field(default_factory=list)


@dataclass
class Config:
    primary_checks: list[str]
    standby_checks: list[str]
    standby_databases: list[str]
    dns: Dns
    state_file: str
    interval_s: float = 10.0
    failures: int = 3
    timeout_s: float = 3.0

    @staticmethod
    def load(path: str) -> "Config":
        import yaml

        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        return Config.parse(raw)

    @staticmethod
    def parse(raw: dict) -> "Config":
        def need(d: dict, key: str, where: str):
            if d.get(key) in (None, "", []):
                raise FailoverError(f"{where}{key} is required")
            return d[key]

        for key in ("primary_checks", "standby_checks"):
            for url in need(raw, key, ""):
                if urlparse(str(url)).scheme not in ("http", "https", "tcp"):
                    raise FailoverError(f"{key}: {url!r} is not an http(s):// or tcp:// check")
        dbs = []
        for i, db in enumerate(raw.get("standby_databases") or []):
            dbs.append(_secret(need(db, "url_file", f"standby_databases[{i}]."), f"standby_databases[{i}]"))
        d = need(raw, "dns", "")
        algorithm = str(d.get("key_algorithm", "hmac-sha256")).lower()
        if algorithm not in ALGORITHMS:
            raise FailoverError(f"dns.key_algorithm: one of {sorted(ALGORITHMS)}")
        records = []
        for i, r in enumerate(need(d, "records", "dns.")):
            rtype = str(need(r, "type", f"dns.records[{i}].")).upper()
            if rtype not in RECORD_TYPES:
                raise FailoverError(f"dns.records[{i}].type: one of {sorted(RECORD_TYPES)}")
            ttl = int(r.get("ttl", 60))
            if not 1 <= ttl <= 3600:
                raise FailoverError(f"dns.records[{i}].ttl: 1 to 3600 seconds (keep it low: clients follow after it)")
            name, value = str(need(r, "name", f"dns.records[{i}].")), str(need(r, "value", f"dns.records[{i}]."))
            records.append(Record(name, rtype, value, ttl))
        dns = Dns(server=need(d, "server", "dns."), zone=str(need(d, "zone", "dns.")), key_name=str(need(d, "key_name", "dns.")),
                  key_secret=_secret(need(d, "key_secret_file", "dns."), "dns.key_secret_file"), key_algorithm=algorithm,
                  port=int(d.get("port", 53)), records=records)
        failures = int(raw.get("failures", 3))
        if failures < 2:
            raise FailoverError("failures: at least 2 (one failed probe is not a lost region)")
        return Config(primary_checks=list(raw["primary_checks"]), standby_checks=list(raw["standby_checks"]),
                      standby_databases=dbs, dns=dns, state_file=need(raw, "state_file", ""),
                      interval_s=float(raw.get("interval_s", 10)), failures=failures, timeout_s=float(raw.get("timeout_s", 3)))


def probe(url: str, timeout: float) -> bool:
    """True when the check answers: a TCP connection, or an HTTP status below 500.

    Prefer HTTP checks for the standby: a hung process still completes TCP handshakes (the kernel's listen queue
    accepts them), so only a check it must answer itself tells that it works."""
    u = urlparse(url)
    try:
        if u.scheme == "tcp":
            socket.create_connection((u.hostname, u.port), timeout).close()
            return True
        import httpx

        return httpx.get(url, timeout=timeout, follow_redirects=False).status_code < 500
    except Exception:  # noqa: BLE001 - any failure to answer is a failed check
        return False


def _in_recovery(url: str, timeout: float) -> bool:
    import psycopg

    with psycopg.connect(url, connect_timeout=max(1, int(timeout)), autocommit=True) as c:
        return bool(c.execute("SELECT pg_is_in_recovery()").fetchone()[0])


def promote(url: str, timeout: float = 60.0) -> bool:
    """Promotes a streaming standby; True when it was promoted now, False when it already took writes."""
    import psycopg

    with psycopg.connect(url, connect_timeout=10, autocommit=True) as c:
        if not c.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            return False
        if not c.execute("SELECT pg_promote(true, %s)", (int(timeout),)).fetchone()[0]:
            raise FailoverError("the standby database did not finish promoting in time")
        if c.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            raise FailoverError("the standby database is still in recovery after promotion")
        return True


def _keyring(d: Dns):
    import dns.tsigkeyring

    return dns.tsigkeyring.from_text({d.key_name: (d.key_algorithm, d.key_secret)})


def _fqdn(name: str, zone: str) -> str:
    return name if name.endswith(".") else f"{name}.{zone.rstrip('.')}."


def point_dns(d: Dns, timeout: float = 10.0) -> list[str]:
    """Replaces each record with the standby's value (TSIG-signed RFC 2136 updates), then reads it back."""
    import dns.exception
    import dns.message
    import dns.name
    import dns.query
    import dns.rcode
    import dns.rdatatype
    import dns.tsig
    import dns.update

    keyring, algorithm = _keyring(d), dns.name.from_text(d.key_algorithm)
    done = []
    for r in d.records:
        u = dns.update.Update(d.zone, keyring=keyring, keyalgorithm=algorithm)
        u.replace(dns.name.from_text(_fqdn(r.name, d.zone)), r.ttl, r.type, r.value)
        try:
            resp = dns.query.tcp(u, d.server, port=d.port, timeout=timeout)
        except dns.tsig.PeerError as e:          # the server rejected our key or signature
            raise FailoverError(f"DNS update of {r.name} refused: {type(e).__name__} (check the TSIG key)") from e
        except (dns.exception.DNSException, OSError) as e:
            raise FailoverError(f"DNS update of {r.name} failed: {type(e).__name__}: {e}") from e
        if resp.rcode() != dns.rcode.NOERROR:
            raise FailoverError(f"DNS update of {r.name} refused: {dns.rcode.to_text(resp.rcode())}")
        q = dns.message.make_query(_fqdn(r.name, d.zone), r.type)
        try:
            ans = dns.query.tcp(q, d.server, port=d.port, timeout=timeout)
        except (dns.exception.DNSException, OSError) as e:
            raise FailoverError(f"DNS record {r.name} could not be read back: {type(e).__name__}") from e
        values = {rd.to_text() for rrset in ans.answer for rd in rrset if rrset.rdtype == dns.rdatatype.from_text(r.type)}
        if r.value not in values and r.value.rstrip(".") + "." not in values:
            raise FailoverError(f"DNS record {r.name} reads {sorted(values)} after the update, not {r.value}")
        done.append(f"{r.name} {r.type} {r.value}")
    return done


class Watcher:
    def __init__(self, cfg: Config, probe_fn: Callable[[str, float], bool] = probe, clock: Callable[[], float] = time.time):
        self.cfg, self.probe, self.clock = cfg, probe_fn, clock
        self.lost_ticks = 0
        self.first_failed_at: Optional[float] = None

    def done(self) -> Optional[dict]:
        try:
            with open(self.cfg.state_file) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def _standby_ok(self) -> Optional[str]:
        bad = [u for u in self.cfg.standby_checks if not self.probe(u, self.cfg.timeout_s)]
        if bad:
            return f"standby checks failing: {', '.join(bad)}"
        for i, url in enumerate(self.cfg.standby_databases):
            try:
                _in_recovery(url, self.cfg.timeout_s)
            except Exception as e:  # noqa: BLE001 - unreachable is not healthy
                return f"standby database {i} unreachable ({type(e).__name__})"
        return None

    def tick(self) -> dict:
        """One round: a report of what was seen and done."""
        if (state := self.done()) is not None:
            if state.get("in_progress"):
                # A failover that started (databases may already take writes) is finished whatever the primary
                # does now: sending clients back to it would leave two regions taking writes.
                log.warning("finishing the failover started at %s", state.get("started_at"))
                self.first_failed_at = self.first_failed_at or state.get("primary_lost_since")
                return self.fail_over()
            return {"action": "none", "why": "already failed over (remove the state file once the old primary is rebuilt)",
                    "state": state}
        down = [u for u in self.cfg.primary_checks if not self.probe(u, self.cfg.timeout_s)]
        if len(down) < len(self.cfg.primary_checks):
            if self.lost_ticks:
                log.info("primary answering again after %d failed round(s)", self.lost_ticks)
            self.lost_ticks, self.first_failed_at = 0, None
            return {"action": "none", "why": "primary healthy" if not down else f"primary partly failing: {', '.join(down)}"}
        self.lost_ticks += 1
        self.first_failed_at = self.first_failed_at or self.clock()
        if self.lost_ticks < self.cfg.failures:
            return {"action": "none", "why": f"primary unreachable ({self.lost_ticks} of {self.cfg.failures})"}
        if why := self._standby_ok():
            log.error("primary lost but not failing over: %s", why)
            return {"action": "refused", "why": why}
        return self.fail_over()

    def _write_state(self, state: dict) -> None:
        tmp = f"{self.cfg.state_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.cfg.state_file)

    def fail_over(self) -> dict:
        started = self.clock()
        # Recorded before anything changes: a round that fails half-way is resumed, never abandoned (see tick).
        self._write_state({"in_progress": True, "started_at": started, "primary_lost_since": self.first_failed_at})
        promoted = [promote(url) for url in self.cfg.standby_databases]
        records = point_dns(self.cfg.dns)
        state = {"failed_over_at": self.clock(), "primary_lost_since": self.first_failed_at, "databases_promoted": promoted,
                 "dns": records, "seconds": round(self.clock() - started, 2)}
        self._write_state(state)
        log.warning("failed over to the standby: %s", state)
        return {"action": "failed-over", **state}

    def run(self, once: bool = False) -> int:
        while True:
            try:
                report = self.tick()
            except Exception as e:  # noqa: BLE001 - retried next round: promotion and record replacement are idempotent
                log.error("failover round failed, retrying in %s s: %s: %s", self.cfg.interval_s, type(e).__name__, e)
                report = {"action": "error", "why": f"{type(e).__name__}: {e}"}
                if once:
                    return 1
                time.sleep(self.cfg.interval_s)
                continue
            log.info("%s", report)
            if once or report["action"] == "failed-over":
                return 0 if report["action"] != "refused" else 1
            time.sleep(self.cfg.interval_s)


def main(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config.load(args.config)
    except FailoverError as e:
        log.error("%s", e)
        return 2
    return Watcher(cfg).run(once=args.once)
