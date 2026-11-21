"""Known-answer benchmark (mock mode, no LLM quota) + LLM-safety integration tests with a fake provider."""
import pytest

from analytics.evaluation import DEFAULT, run_eval
from analytics.narrator import grounded
from analytics.pipeline import answer
from utils.llm import MockProvider


@pytest.fixture(scope="module")
def results():
    return run_eval(MockProvider())


def test_known_answer_accuracy_is_100pct(results):
    failed = results[~results.passed]
    assert failed.empty, failed[["question", "expected", "actual", "error"]].to_string()
    assert len(results) >= 10


def test_no_invalid_sql_in_benchmark(results):
    assert results.sql_valid_or_expected_block.all()


def test_latency_under_10s(results):
    assert results.latency_s.max() < 10


class FakeLLM:
    """Simulates a misbehaving LLM: returns unsafe SQL and a hallucinated summary."""
    name = "fake"

    def __init__(self, sql, summary="Success fell 99.9% because of Visa."):
        self.sql, self.summary = sql, summary

    def complete(self, messages, json_mode=False):
        if json_mode:
            import json
            return json.dumps({"analysis_type": "lookup", "metric": "success_rate",
                               "period_a": {"start": "2026-10-01", "end": "2026-11-01", "label": "Oct 2026"},
                               "period_b": None, "group_by": [], "filters": {}, "sql": self.sql, "params": []})
        return self.summary


def test_unsafe_llm_sql_is_never_executed():
    rep = answer("What was the success rate in Oct 2026?", "analyst", DEFAULT,
                 FakeLLM("SELECT 1; DROP TABLE transactions"))
    assert rep["status"] == "ok"
    assert rep["primary"]["source"] == "template"
    assert any("rejected by validator" in n for n in rep["llm"]["notes"])


def test_hallucinated_summary_is_rejected():
    rep = answer("What was the success rate in Oct 2026?", "analyst", DEFAULT,
                 FakeLLM("SELECT COUNT(*) AS n FROM transactions"))
    assert rep["narrative"]["summary"] is None
    assert "grounding" in rep["narrative"]["summary_source"]


def test_query_flow_records_every_query_and_llm_draft():
    rep = answer("Why did our overall success rate fall from Sept to Oct?", "analyst", DEFAULT, MockProvider())
    purposes = [q["purpose"] for q in rep["queries"]]
    assert purposes[0] == "Answer query" and any("evidence" in p for p in purposes) and any("trend" in p for p in purposes)
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in rep["queries"])
    rep = answer("What was the success rate in Oct 2026?", "analyst", DEFAULT, FakeLLM("SELECT 1; DROP TABLE transactions"))
    assert rep["llm_sql"]["ok"] is False and rep["llm_sql"]["errors"]          # rejected draft is kept for display
    assert rep["primary"]["source"] == "template"


def test_grounding_accepts_faithful_summary():
    nar = {"headline": "Success rate fell from 92.8% to 85.0% (-7.85pt).", "facts": [], "hypotheses": [],
           "recommendations": [], "caveats": []}
    assert grounded("Success rate fell from 92.8% to 85.0%, a 7.85 point drop.", nar)
    assert not grounded("Success rate fell from 92.8% to 81.0%.", nar)


def test_llm_access_denied_sql_reports_denial():
    rep = answer("Show me success rates by bank", "merchant_analyst", DEFAULT,
                 FakeLLM("SELECT bank_name FROM banks"))
    assert rep["status"] == "access_denied"
