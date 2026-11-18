"""The chart's Gateway API rendering (core.gateway), without a cluster: `helm template` only. Needs helm
(CAIRN_HELM, or helm on PATH). The routing itself is tested through Envoy Gateway in test_gateway_api_kind.py."""

import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="needs helm")


def render(*sets):
    args = [a for kv in ("core.ingress.enabled=false", "core.gateway.enabled=true", "core.gateway.create.enabled=true",
                         "core.gateway.create.className=eg", *sets) for a in ("--set", kv)]
    out = subprocess.run([HELM, "template", "t", "helm/cairn", *args], cwd=ROOT, capture_output=True, text=True,
                         timeout=300)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d and d["kind"] == "HTTPRoute"]


def test_with_a_certificate_cairn_is_not_served_over_plain_http():
    routes = render("core.gateway.create.tlsSecret=cairn-tls")
    serving = [r for r in routes if any(rule.get("backendRefs") for rule in r["spec"]["rules"])]
    assert serving and all(p.get("sectionName") == "https" for r in serving for p in r["spec"]["parentRefs"])
    redirect = [r for r in routes if r not in serving]
    assert len(redirect) == 1 and redirect[0]["spec"]["parentRefs"][0]["sectionName"] == "http"
    assert redirect[0]["spec"]["rules"][0]["filters"][0]["requestRedirect"]["scheme"] == "https"


def test_without_a_certificate_the_http_listener_serves():
    routes = render()
    assert len(routes) == 1 and routes[0]["spec"]["parentRefs"][0].get("sectionName") == "http"
