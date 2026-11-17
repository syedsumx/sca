"""The chart's Gateway API routes (core.gateway) as helm renders them: what each listener serves. Needs helm (CI's
helm job runs this file as a script; under pytest it skips without helm)."""

import os
import shutil
import subprocess

import yaml

try:
    import pytest

    pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")
except ImportError:  # run as a script (CI's helm job has helm and PyYAML, not pytest)
    pass

CHART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helm", "cairn")
CREATE = ["core.gateway.enabled=true", "core.gateway.create.enabled=true", "core.gateway.create.className=eg"]


def render(*sets: str) -> list[dict]:
    args = ["helm", "template", "t", CHART, "--show-only", "templates/gateway-api.yaml"]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def routes(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == "HTTPRoute"}


def test_with_tls_plain_http_only_redirects_to_https():
    docs = render(*CREATE, "core.gateway.create.tlsSecret=cairn-tls", "core.gateway.hostnames[0]=cairn.test")
    rs = routes(docs)
    app = rs["t-cairn-edge"]
    # The application is served on the HTTPS listener only, never in the clear.
    assert [p.get("sectionName") for p in app["spec"]["parentRefs"]] == ["https"], app["spec"]["parentRefs"]
    redirect = rs["t-cairn-edge-https-redirect"]
    assert redirect["spec"]["parentRefs"] == [{"name": "t-cairn-gateway", "sectionName": "http"}]
    assert redirect["spec"]["hostnames"] == ["cairn.test"]
    [rule] = redirect["spec"]["rules"]
    assert "backendRefs" not in rule
    assert rule["filters"] == [{"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 301}}]


def test_each_https_listener_names_its_hostname_for_cert_manager():
    docs = render(*CREATE, "core.gateway.create.tlsSecret=cairn-tls", "core.gateway.hostnames[0]=cairn.test",
                  "core.gateway.hostnames[1]=data.test")
    [gw] = [d for d in docs if d["kind"] == "Gateway"]
    https = [x for x in gw["spec"]["listeners"] if x["protocol"] == "HTTPS"]
    # cert-manager's Gateway support skips a listener without a hostname: no certificate would ever be issued.
    assert [(x["name"], x.get("hostname")) for x in https] == [("https", "cairn.test"), ("https-1", "data.test")]
    assert all(x["tls"]["certificateRefs"] == [{"kind": "Secret", "name": "cairn-tls"}] for x in https)
    app = routes(docs)["t-cairn-edge"]
    assert [p["sectionName"] for p in app["spec"]["parentRefs"]] == ["https", "https-1"]


def test_without_tls_the_http_listener_serves_the_application():
    rs = routes(render(*CREATE))
    assert list(rs) == ["t-cairn-edge"]
    assert rs["t-cairn-edge"]["spec"]["parentRefs"] == [{"name": "t-cairn-gateway", "sectionName": "http"}]


def test_a_shared_gateway_is_attached_to_as_given():
    rs = routes(render("core.gateway.enabled=true", "core.gateway.parentRefs[0].name=platform",
                       "core.gateway.parentRefs[0].namespace=gw", "core.gateway.parentRefs[0].sectionName=https"))
    assert list(rs) == ["t-cairn-edge"]
    assert rs["t-cairn-edge"]["spec"]["parentRefs"] == [{"name": "platform", "namespace": "gw", "sectionName": "https"}]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
