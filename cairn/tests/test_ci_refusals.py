"""CI's negative checks of the Helm chart ("these values are refused") must check why they are refused.

``! helm template ... >/dev/null 2>&1`` passes whenever helm fails, for whatever reason: a guard removed while
another, unrelated error fires (a value newly required, a template typo) leaves the check green. Each refusal goes
through ``.github/scripts/helm-refuses.sh`` with the text of the chart's own error instead."""

import os
import re
import stat
import subprocess

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
WORKFLOW = os.path.join(REPO, ".github", "workflows", "cairn-ci.yml")
SCRIPT = os.path.join(REPO, ".github", "scripts", "helm-refuses.sh")


def test_no_refusal_check_hides_the_reason():
    text = open(WORKFLOW).read()
    blind = [m.group(0).splitlines()[0].strip() for m in re.finditer(r"^\s*!\s*helm\s+template\b.*$", text, re.M)]
    assert not blind, "use helm-refuses.sh \"<the chart's error>\" ... instead of:\n" + "\n".join(blind)
    calls = re.findall(r"helm-refuses\.sh\"?\s+(\"[^\"]*\"|'[^']*')", text)
    assert calls and all(len(c.strip("\"'")) >= 10 for c in calls), calls   # a real piece of the message


@pytest.fixture()
def fake_helm(tmp_path):
    """A stand-in for helm: exits with $FAKE_RC and prints $FAKE_ERR on stderr."""
    helm = tmp_path / "helm"
    helm.write_text('#!/usr/bin/env bash\necho "$FAKE_ERR" >&2\nexit "$FAKE_RC"\n')
    helm.chmod(helm.stat().st_mode | stat.S_IEXEC)
    return str(helm)


def _run(fake_helm, rc, err):
    return subprocess.run(["bash", SCRIPT, "cairn.trino needs cairn.trino.tls.secretName", "cairn", "helm/cairn"],
                          env={**os.environ, "HELM": fake_helm, "FAKE_RC": str(rc), "FAKE_ERR": err},
                          capture_output=True, text=True, timeout=30)


def test_the_script_passes_only_for_the_named_refusal(fake_helm):
    assert _run(fake_helm, 1, "Error: execution error at (cairn/templates/trino.yaml:17:54): cairn.trino needs "
                              "cairn.trino.tls.secretName (sign-in tokens travel over HTTPS)").returncode == 0
    other = _run(fake_helm, 1, "Error: template: cairn/templates/api.yaml:3: nil pointer evaluating")
    assert other.returncode != 0 and "nil pointer" in other.stderr
    rendered = _run(fake_helm, 0, "")
    assert rendered.returncode != 0 and "rendered" in rendered.stderr
