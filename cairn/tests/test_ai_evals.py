"""The shipped AI eval cases pass, and each one fails when the guardrail it guards is removed."""

import os

import pytest

from cairn.ai import evals
from cairn.cli import main

CASES = os.path.join(os.path.dirname(__file__), "..", "config", "ai-evals")


def _failed() -> set[str]:
    return set(evals.summarize(evals.run_cases(evals.load_cases(CASES)))["failed"])


def test_shipped_cases_pass():
    results = evals.run_cases(evals.load_cases(CASES))
    assert results and all(r["ok"] for r in results), [r for r in results if not r["ok"]]
    assert {r["feature"] for r in results} == {"nlq", "docgen", "explain", "semantic", "steward", "text2sql"}


def test_without_redaction_pii_cases_fail(monkeypatch):
    for mod in ("cairn.ai.nlq", "cairn.ai.docgen", "cairn.ai.lineage_explainer"):
        monkeypatch.setattr(f"{mod}.redact", lambda t: t)
    assert _failed() >= {"nlq-redacts-pii-before-the-model", "docgen-never-invents-columns",
                         "explain-hides-restricted-upstreams-and-redacts"}


def test_without_tag_escaping_injection_cases_fail(monkeypatch):
    for mod in ("cairn.ai.nlq", "cairn.ai.docgen"):
        monkeypatch.setattr(f"{mod}.untrusted", lambda t, label="catalog_metadata": f"<{label}>\n{t}\n</{label}>")
    assert _failed() >= {"nlq-injection-stays-inside-the-data-wrapper", "docgen-injection-in-description-is-data"}


def test_without_authorization_filtering_restricted_cases_fail(monkeypatch):
    for mod in ("cairn.ai.nlq", "cairn.ai.semantic"):
        monkeypatch.setattr(f"{mod}.filter_viewable", lambda items, can_view: list(items))
    assert _failed() >= {"nlq-restricted-assets-never-reach-prompt-or-answer", "nlq-falls-back-without-a-model",
                         "semantic-never-returns-hidden-assets"}


def test_without_plural_folding_semantic_ranking_regresses(monkeypatch):
    monkeypatch.setattr("cairn.ai.embeddings._stem", lambda w: w)
    assert "semantic-never-returns-hidden-assets" in _failed()


def test_without_citation_filter_hallucinations_are_caught(monkeypatch):
    from cairn.ai.nlq import NaturalLanguageQuery

    real = NaturalLanguageQuery._answer

    def unfiltered(self, question, plan, results):
        answer, cites = real(self, question, plan, results)
        if self.llm is not None and results:   # return what the model cited, unchecked
            out, _ = self.llm.generate_json("", "", evals.ANSWER_SCHEMA)
            return answer, out["citations"]
        return answer, cites

    monkeypatch.setattr(NaturalLanguageQuery, "_answer", unfiltered)
    assert "nlq-drops-hallucinated-citations" in _failed()


def test_case_validation(tmp_path):
    (tmp_path / "bad.yaml").write_text("cases:\n  - id: x\n    feature: nlq\n    expect: {nonsense: 1}\n")
    with pytest.raises(evals.EvalCaseError, match="unknown expectations"):
        evals.load_cases(str(tmp_path))
    (tmp_path / "bad.yaml").write_text("cases:\n  - {id: x, feature: poetry}\n")
    with pytest.raises(evals.EvalCaseError, match="feature"):
        evals.load_cases(str(tmp_path))
    (tmp_path / "bad.yaml").write_text("cases:\n  - {id: x, feature: nlq}\n  - {id: x, feature: nlq}\n")
    with pytest.raises(evals.EvalCaseError, match="duplicate"):
        evals.load_cases(str(tmp_path))


def test_a_crashing_feature_is_a_failed_case(tmp_path):
    (tmp_path / "c.yaml").write_text("cases:\n  - {id: missing-asset, feature: docgen, urn: 'urn:li:dataset:x',"
                                     " model_output: {draft: {}}}\n")
    [r] = evals.run_cases(evals.load_cases(str(tmp_path)))
    assert not r["ok"] and "LookupError" in r["problems"][0]


def test_cli(capsys, tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["ai", "eval", CASES])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "19/19 AI eval cases passed" in out and "nlq-drops-hallucinated-citations" in out
    report = tmp_path / "r.json"
    with pytest.raises(SystemExit):
        main(["ai", "eval", CASES, "--json", str(report)])
    import json

    body = json.loads(report.read_text())
    assert body["summary"]["passed"] == 19 and body["mode"] == "offline"
    with pytest.raises(SystemExit) as e:
        main(["ai", "eval", CASES, "--live"])            # no provider configured
    assert "needs an LLM provider" in str(e.value.code)
