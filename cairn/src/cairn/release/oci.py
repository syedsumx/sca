"""Images by digest between OCI registries and an OCI image layout on disk (L162): what an air-gapped bundle carries.

Only what a release needs: a manifest (or an index of manifests, one per platform) and its blobs, addressed by digest,
so what lands in a customer's registry is byte for byte what the vendor signed (the digest *is* the content; nothing
here trusts a tag). Every manifest and blob is hashed as it is read and refused when it is not its digest.

Sign-in: anonymous, or a user and password (a token as the password) from a Docker ``config.json`` (``auths``:
``{"registry.example.com": {"auth": "<base64 user:password>"}}``) read from a file, never from the command line;
Basic or the registry token flow (``WWW-Authenticate: Bearer realm=…``), the realm https only. TLS by default, a
private CA from a file; plain http only to a registry on this machine.

Layout (https://github.com/opencontainers/image-spec/blob/main/image-layout.md): ``oci-layout``, ``index.json`` (one
entry per image, its reference in ``org.opencontainers.image.ref.name``), ``blobs/sha256/<hex>``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from cairn.release.manifest import ReleaseError

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$")
HOST = re.compile(r"^[A-Za-z0-9.-]+(?::\d{1,5})?$")
MAX_MANIFEST = 4 << 20            # a manifest larger than this is refused unread
CHUNK = 1 << 20
LOCAL = ("127.0.0.1", "localhost", "::1")
REF_NAME = "org.opencontainers.image.ref.name"


def parse_ref(ref: str) -> tuple[str, str, str]:
    """``registry.example.com[:port]/repo/name@sha256:…`` → (registry, repository, digest). A release names its
    registry explicitly and pins by digest; anything else is refused."""
    name, sep, digest = ref.partition("@")
    if not sep or not DIGEST.match(digest):
        raise ReleaseError(f"{ref}: an image is pinned by digest (…@sha256:…)")
    host, _, repo = name.partition("/")
    if not repo or not HOST.match(host) or not ("." in host or ":" in host or host == "localhost"):
        raise ReleaseError(f"{ref}: name the registry (registry.example.com/repository@sha256:…)")
    if not REPO.match(repo):
        raise ReleaseError(f"{ref}: {repo!r} is not a repository name")
    return host, repo, digest


def split_target(target: str) -> tuple[str, str]:
    """``registry.internal:5000/mirror/cairn`` → (``registry.internal:5000``, ``mirror/cairn``): where images go."""
    host, _, prefix = target.strip().rstrip("/").partition("/")
    if not HOST.match(host) or (prefix and not REPO.match(prefix)):
        raise ReleaseError(f"{target}: a registry, optionally with a path (registry.internal:5000/mirror)")
    return host, prefix


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def read_auths(config_file: Optional[str]) -> dict[str, tuple[str, str]]:
    """{registry: (user, password)} from a Docker config.json (``auths``; credential helpers are not run)."""
    if not config_file:
        return {}
    try:
        doc = json.loads(Path(config_file).read_text())
    except (OSError, ValueError) as e:
        raise ReleaseError(f"{config_file}: not a readable Docker config.json ({type(e).__name__})")
    out = {}
    for host, entry in (doc.get("auths") or {}).items():
        host = re.sub(r"^https?://", "", host).split("/", 1)[0]
        if isinstance(entry, dict) and entry.get("auth"):
            user, _, pw = base64.b64decode(entry["auth"]).decode().partition(":")
            out[host] = (user, pw)
        elif isinstance(entry, dict) and entry.get("username"):
            out[host] = (entry["username"], entry.get("password") or "")
    return out


class Registry:
    """One registry's v2 API, as one client."""

    def __init__(self, host: str, auth: Optional[tuple[str, str]] = None, ca_file: Optional[str] = None,
                 plain_http: bool = False, http=None):
        if not HOST.match(host):
            raise ReleaseError(f"{host!r} is not a registry")
        if plain_http and host.rsplit(":", 1)[0].strip("[]") not in LOCAL:
            raise ReleaseError("plain http is for a registry on this machine only")
        self.host, self.auth = host, auth
        self.base = f"{'http' if plain_http else 'https'}://{host}"
        if http is None:
            import ssl

            import httpx

            verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
            http = httpx.Client(timeout=httpx.Timeout(60, read=300), verify=verify, follow_redirects=False,
                                headers={"User-Agent": "cairn-bundle"})
        self.http = http
        self._tokens: dict[str, str] = {}

    # ---- sign-in -------------------------------------------------------------------------------------------
    def _token(self, challenge: str, scope: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.get("realm", "")
        u = urlsplit(realm)
        if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in LOCAL)):
            raise ReleaseError(f"{self.host}: its token service {realm!r} is not https: refused")
        query = {k: v for k, v in (("service", params.get("service")), ("scope", scope)) if v}
        url = urlunsplit((u.scheme, u.netloc, u.path, "&".join(filter(None, [u.query, urlencode(query)])), ""))
        r = self.http.get(url, auth=self.auth) if self.auth else self.http.get(url)
        if r.status_code != 200:
            raise ReleaseError(f"{self.host}: its token service answered {r.status_code}")
        body = r.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            raise ReleaseError(f"{self.host}: its token service gave no token")
        return token

    def _request(self, method: str, url: str, headers: dict, stream: bool, **kw):
        req = self.http.build_request(method, url, headers=headers, **kw)
        return self.http.send(req, stream=stream)

    def _send(self, method: str, url: str, repo: str, push: bool = False, stream: bool = False, **kw):
        """A request with this registry's sign-in. With ``stream``, the caller reads and closes the response."""
        scope = f"repository:{repo}:{'pull,push' if push else 'pull'}"
        headers = dict(kw.pop("headers", {}) or {})
        if scope in self._tokens:
            headers["Authorization"] = self._tokens[scope]
        body = kw.get("content")
        r = self._request(method, url, headers, stream, **kw)
        if r.status_code != 401:
            return r
        r.close()
        challenge = r.headers.get("www-authenticate", "")
        if challenge.lower().startswith("bearer"):
            self._tokens[scope] = "Bearer " + self._token(challenge, scope)
        elif challenge.lower().startswith("basic") and self.auth:
            self._tokens[scope] = "Basic " + base64.b64encode(":".join(self.auth).encode()).decode()
        else:
            raise ReleaseError(f"{self.host}: sign-in needed ({repo}); give a Docker config.json with its auth")
        if hasattr(body, "seek"):
            body.seek(0)
        headers["Authorization"] = self._tokens[scope]
        r = self._request(method, url, headers, stream, **kw)
        if r.status_code == 401:
            r.close()
            raise ReleaseError(f"{self.host}: the credentials were refused for {repo}")
        return r

    def _url(self, repo: str, kind: str, ref: str = "") -> str:
        return f"{self.base}/v2/{repo}/{kind}/{ref}"

    # ---- reading -------------------------------------------------------------------------------------------
    def manifest(self, repo: str, digest: str) -> tuple[bytes, str]:
        r = self._send("GET", self._url(repo, "manifests", digest), repo, headers={"Accept": ACCEPT})
        if r.status_code != 200:
            raise ReleaseError(f"{self.host}/{repo}@{digest}: the registry answered {r.status_code}")
        raw = r.content
        if len(raw) > MAX_MANIFEST:
            raise ReleaseError(f"{repo}@{digest}: a manifest larger than {MAX_MANIFEST} bytes")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ReleaseError(f"{self.host}/{repo}@{digest}: the manifest served is not that digest: refused")
        media = (r.headers.get("content-type") or "").split(";")[0].strip() or json.loads(raw).get("mediaType", "")
        return raw, media

    def has_blob(self, repo: str, digest: str) -> bool:
        r = self._send("HEAD", self._url(repo, "blobs", digest), repo, push=True)
        return r.status_code == 200

    def fetch_blob(self, repo: str, digest: str, dest: Path) -> None:
        """The blob, hashed as it streams, into ``dest``; a blob that is not its digest is never kept."""
        url = self._url(repo, "blobs", digest)
        for _ in range(4):                       # a registry may redirect a blob to its storage
            r = self._send("GET", url, repo, stream=True)
            try:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    nxt = urljoin(url, r.headers["location"])
                    if urlsplit(nxt).netloc != urlsplit(self.base).netloc:
                        self._fetch_plain(nxt, digest, dest)        # another host: no credential goes with it
                        return
                    url = nxt
                    continue
                if r.status_code != 200:
                    raise ReleaseError(f"{self.host}/{repo}: blob {digest}: the registry answered {r.status_code}")
                self._keep(r.iter_bytes(CHUNK), digest, dest)
                return
            finally:
                r.close()
        raise ReleaseError(f"{self.host}/{repo}: blob {digest}: too many redirects")

    def _fetch_plain(self, url: str, digest: str, dest: Path) -> None:
        if urlsplit(url).scheme != "https":
            raise ReleaseError(f"a blob was redirected to {urlsplit(url).scheme}: refused")
        with self.http.stream("GET", url) as r:
            if r.status_code != 200:
                raise ReleaseError(f"blob {digest}: its storage answered {r.status_code}")
            self._keep(r.iter_bytes(CHUNK), digest, dest)

    @staticmethod
    def _keep(chunks: Iterator[bytes], digest: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    h.update(chunk)
                    f.write(chunk)
            if "sha256:" + h.hexdigest() != digest:
                raise ReleaseError(f"blob {digest}: the bytes served are not that digest: refused")
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---- writing -------------------------------------------------------------------------------------------
    def push_blob(self, repo: str, digest: str, path: Path) -> bool:
        """The blob from ``path`` (already checked), unless the registry has it. True when it was sent."""
        if self.has_blob(repo, digest):
            return False
        r = self._send("POST", f"{self.base}/v2/{repo}/blobs/uploads/", repo, push=True)
        if r.status_code not in (201, 202) or not r.headers.get("location"):
            raise ReleaseError(f"{self.host}/{repo}: an upload could not start ({r.status_code})")
        u = urlsplit(urljoin(self.base + "/", r.headers["location"]))
        if u.netloc != urlsplit(self.base).netloc:
            raise ReleaseError(f"{self.host}: the upload went to another host: refused")
        query = urlencode(parse_qsl(u.query) + [("digest", digest)])
        url = urlunsplit((u.scheme, u.netloc, u.path, query, ""))
        with path.open("rb") as f:
            r = self._send("PUT", url, repo, push=True, content=f,
                           headers={"Content-Type": "application/octet-stream",
                                    "Content-Length": str(path.stat().st_size)})
        if r.status_code != 201:
            raise ReleaseError(f"{self.host}/{repo}: blob {digest} was not accepted ({r.status_code})")
        return True

    def put_manifest(self, repo: str, digest: str, raw: bytes, media: str) -> None:
        r = self._send("PUT", self._url(repo, "manifests", digest), repo, push=True, content=raw,
                       headers={"Content-Type": media})
        if r.status_code not in (200, 201):
            raise ReleaseError(f"{self.host}/{repo}@{digest}: the manifest was not accepted ({r.status_code}: "
                               f"{r.text[:200]})")
        got = r.headers.get("docker-content-digest")
        if got and got != digest:
            raise ReleaseError(f"{self.host}/{repo}: stored the manifest as {got}, not {digest}")


# ---- the layout -------------------------------------------------------------------------------------------------
class Layout:
    def __init__(self, root: Path):
        self.root = root

    def blob(self, digest: str) -> Path:
        if not DIGEST.match(digest):
            raise ReleaseError(f"{digest!r} is not a sha256 digest")
        return self.root / "blobs" / "sha256" / digest.split(":", 1)[1]

    def init(self) -> None:
        (self.root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
        (self.root / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}))
        if not (self.root / "index.json").exists():
            self._write_index([])

    def entries(self) -> list[dict]:
        p = self.root / "index.json"
        return json.loads(p.read_text()).get("manifests", []) if p.exists() else []

    def _write_index(self, entries: list[dict]) -> None:
        (self.root / "index.json").write_text(json.dumps(
            {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": entries},
            indent=1))

    def add(self, ref: str, digest: str, media: str) -> None:
        entries = [e for e in self.entries() if e.get("annotations", {}).get(REF_NAME) != ref]
        entries.append({"mediaType": media, "digest": digest, "size": self.blob(digest).stat().st_size,
                        "annotations": {REF_NAME: ref}})
        self._write_index(sorted(entries, key=lambda e: e["annotations"][REF_NAME]))

    def walk(self, digest: str, media: str) -> Iterator[tuple[str, str, bool]]:
        """(digest, media type, is a manifest) of everything ``digest`` is made of, children before parents. Each
        manifest is checked against its digest before it is read, so an altered one cannot point anywhere."""
        p = self.blob(digest)
        if not p.is_file() or p.stat().st_size > MAX_MANIFEST:
            raise ReleaseError(f"the bundle lacks manifest {digest}, or it is too large")
        raw = p.read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ReleaseError(f"manifest {digest} in the bundle is not that digest: refused")
        try:
            doc = json.loads(raw)
        except ValueError:
            raise ReleaseError(f"manifest {digest} is not JSON")
        if media in INDEX_TYPES:
            for m in doc.get("manifests") or []:
                yield from self.walk(m["digest"], m.get("mediaType", ""))
        elif media in MANIFEST_TYPES:
            for b in [doc.get("config") or {}] + list(doc.get("layers") or []):
                if b.get("digest") and not b.get("urls"):          # a foreign layer stays where it is
                    yield b["digest"], b.get("mediaType", ""), False
        else:
            raise ReleaseError(f"{digest}: {media or 'an unknown type'} is not an image manifest or index")
        yield digest, media, True

    def verify(self, digest: str, media: str) -> int:
        """Every blob the image needs is present and is its digest. Returns the bytes checked."""
        total = 0
        for d, _, _ in self.walk(digest, media):
            p = self.blob(d)
            if not p.is_file():
                raise ReleaseError(f"the bundle lacks blob {d}")
            if _sha(p) != d:
                raise ReleaseError(f"blob {d} in the bundle is not that digest: refused")
            total += p.stat().st_size
        return total


def pull(reg: Registry, repo: str, digest: str, layout: Layout, ref: str) -> str:
    """The image ``repo@digest`` and everything it is made of into ``layout``. Returns its media type."""
    def get(d: str) -> str:
        raw, media = reg.manifest(repo, d)
        layout.blob(d).parent.mkdir(parents=True, exist_ok=True)
        layout.blob(d).write_bytes(raw)
        doc = json.loads(raw)
        if media in INDEX_TYPES:
            for m in doc.get("manifests") or []:
                get(m["digest"])
        elif media in MANIFEST_TYPES:
            for b in [doc.get("config") or {}] + list(doc.get("layers") or []):
                if b.get("digest") and not b.get("urls") and not layout.blob(b["digest"]).is_file():
                    reg.fetch_blob(repo, b["digest"], layout.blob(b["digest"]))
        else:
            raise ReleaseError(f"{repo}@{d}: {media or 'an unknown type'} is not an image manifest or index")
        return media

    media = get(digest)
    layout.add(ref, digest, media)
    return media


def push(layout: Layout, digest: str, media: str, reg: Registry, repo: str) -> int:
    """The image from ``layout`` into ``reg``/``repo`` by digest, blobs first. Returns the blobs sent."""
    sent = 0
    for d, m, is_manifest in layout.walk(digest, media):
        if is_manifest:
            reg.put_manifest(repo, d, layout.blob(d).read_bytes(), m)
        else:
            sent += reg.push_blob(repo, d, layout.blob(d))
    return sent


__all__ = ["Layout", "Registry", "parse_ref", "pull", "push", "read_auths", "split_target"]
