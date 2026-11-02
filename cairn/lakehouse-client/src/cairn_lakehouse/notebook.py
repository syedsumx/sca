"""Notebooks on the lakehouse, as the person signed in (Jupyter, JupyterHub, VS Code notebooks).

``%load_ext cairn_lakehouse.notebook`` adds:

* ``%%sql``: the cell runs on Trino as the notebook's Cairn user, with their masks, row filters and
  refusals; the result is shown as a table (a pandas DataFrame when pandas is installed). ``%%sql --to df``
  keeps it in ``df``; ``--limit N`` caps the rows fetched (default 10000, said when it is hit).
* ``%cairn whoami``: who the notebook runs as; ``%cairn spark``: a SparkSession on the lakehouse as the
  same user (``cairn_spark.lakehouse_session``), in ``spark``.

Or without magics: ``from cairn_lakehouse.notebook import sql; sql("SELECT ...")``.

Who the notebook is: ``CAIRN_TOKEN`` / ``CAIRN_TOKEN_FILE`` (JupyterHub passes the signed-in person's
token, see the docs) or ``CAIRN_WORKLOAD_TOKEN_FILE``. For each connection it gets a short Trino token for
that user from Cairn (``POST /v1/trino/token``) and renews it before it expires; nothing long-lived reaches
Trino. Settings: ``CAIRN_URL``, ``CAIRN_TRINO_URL`` (https), ``CAIRN_TRINO_CATALOG`` (default lakehouse),
``CAIRN_CA_FILE``. Queries are Trino's, so their lineage and usage reach Cairn as for any client.
"""

from __future__ import annotations

import os
import shlex
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from cairn_lakehouse import CairnLakehouseError, _cairn_token

DEFAULT_LIMIT = 10_000


class Result:
    """Rows of a query: shown as a table in notebooks."""

    def __init__(self, columns: list[str], rows: list[list[Any]], truncated: bool):
        self.columns, self.rows, self.truncated = columns, rows, truncated

    def to_pandas(self):
        import pandas as pd

        return pd.DataFrame(self.rows, columns=self.columns)

    def to_dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)

    def _repr_html_(self) -> str:
        from html import escape

        head = "".join(f"<th>{escape(str(c))}</th>" for c in self.columns)
        body = "".join("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in r) + "</tr>" for r in self.rows[:200])
        more = f"<p>{len(self.rows)} rows" + (" (the limit: there are more)" if self.truncated else "") + "</p>"
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}"

    def __repr__(self) -> str:
        return f"Result({len(self.rows)} rows{', truncated' if self.truncated else ''}, columns={self.columns})"


class Session:
    """Trino as the notebook's Cairn user, with short tokens renewed before they expire."""

    def __init__(self, cairn_url: Optional[str] = None, trino_url: Optional[str] = None, catalog: Optional[str] = None):
        self.cairn_url = (cairn_url or os.environ.get("CAIRN_URL", "")).rstrip("/")
        self.trino_url = trino_url or os.environ.get("CAIRN_TRINO_URL", "")
        self.catalog = catalog or os.environ.get("CAIRN_TRINO_CATALOG", "lakehouse")
        self.verify: Any = os.environ.get("CAIRN_CA_FILE") or True
        if not self.cairn_url or not self.trino_url:
            raise CairnLakehouseError("set CAIRN_URL (Cairn's API) and CAIRN_TRINO_URL (Trino)")
        if urlsplit(self.trino_url).scheme != "https":
            raise CairnLakehouseError("CAIRN_TRINO_URL must be https: Trino takes sign-in tokens only over TLS")
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires = 0.0
        self.user = ""

    def token(self) -> str:
        with self._lock:
            if self._token is None or time.time() > self._expires - 120:
                cairn = _cairn_token(self.cairn_url, self.verify)
                r = httpx.post(f"{self.cairn_url}/v1/trino/token", json={}, headers={"Authorization": f"Bearer {cairn}"},
                               verify=self.verify, timeout=30)
                if r.status_code != 200:
                    raise CairnLakehouseError(f"Cairn refused a Trino token ({r.status_code}): {r.text[:300]}")
                out = r.json()
                self._token, self.user = out["token"], out.get("user", "") or self.user
                exp = out.get("expires_at")
                self._expires = (exp / 1000 if exp and exp > 1e11 else exp) if exp else time.time() + 600
            return self._token

    def connect(self):
        import trino

        u = urlsplit(self.trino_url)
        session = None
        if isinstance(self.verify, str):
            # The given CA: requests would otherwise let REQUESTS_CA_BUNDLE in the environment win over it.
            import requests

            session = requests.Session()
            session.trust_env = False
            session.verify = self.verify
        return trino.dbapi.connect(host=u.hostname, port=u.port or 443, http_scheme="https", catalog=self.catalog,
                                   auth=trino.auth.JWTAuthentication(self.token()), verify=self.verify,
                                   http_session=session, source="cairn-notebook")

    def sql(self, query: str, limit: int = DEFAULT_LIMIT) -> Result:
        cur = self.connect().cursor()
        cur.execute(query.strip().rstrip(";"))
        rows = cur.fetchmany(limit + 1) if cur.description else []
        truncated = len(rows) > limit
        if truncated:
            cur.cancel()
        cols = [d[0] for d in (cur.description or [])]
        return Result(cols, [list(r) for r in rows[:limit]], truncated)


_session: Optional[Session] = None


def session() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


def sql(query: str, limit: int = DEFAULT_LIMIT) -> Result:
    """Runs ``query`` on Trino as the notebook's Cairn user."""
    return session().sql(query, limit)


def load_ipython_extension(ip) -> None:
    from IPython.core.magic import Magics, cell_magic, line_magic, magics_class

    @magics_class
    class CairnMagics(Magics):
        @cell_magic
        def sql(self, line: str, cell: str):
            args = shlex.split(line or "")
            to, limit = None, DEFAULT_LIMIT
            while args:
                a = args.pop(0)
                if a == "--to" and args:
                    to = args.pop(0)
                elif a == "--limit" and args:
                    limit = int(args.pop(0))
                else:
                    raise CairnLakehouseError(f"%%sql: unknown option {a!r} (--to NAME, --limit N)")
            res = session().sql(cell, limit)
            if res.truncated:
                print(f"Showing the first {limit} rows (--limit N for more).")
            out: Any = res
            try:
                out = res.to_pandas()
            except ImportError:
                pass
            if to:
                if not to.isidentifier():
                    raise CairnLakehouseError("%%sql --to takes a variable name")
                self.shell.user_ns[to] = out
                return None
            return out

        @line_magic
        def cairn(self, line: str):
            cmd = (line or "").strip()
            if cmd == "whoami":
                s = session()
                s.token()
                return s.user
            if cmd == "spark":
                from cairn_spark import lakehouse_session

                self.shell.user_ns["spark"] = lakehouse_session("notebook")
                return "spark: a SparkSession on the lakehouse, as you"
            raise CairnLakehouseError("%cairn whoami | spark")

    ip.register_magics(CairnMagics)
