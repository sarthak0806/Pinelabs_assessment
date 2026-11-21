"""Known-answer evaluation suite. Ground truth is computed with independent raw SQL (not the pipeline),
then compared with what the pipeline reports. Used by tests/test_e2e.py and the UI Evaluation tab.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, List

import pandas as pd

import config
from analytics.nlp import month_period
from analytics.pipeline import answer

DEFAULT = (month_period(2026, 10), month_period(2026, 9))


def _truth(sql: str, params=()):
    con = sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def _rate(where: str, params=(), join: str = ""):
    tot, succ = _truth(f"SELECT COUNT(DISTINCT t.transaction_id), COUNT(DISTINCT CASE WHEN t.status='SUCCESS' "
                       f"THEN t.transaction_id END) FROM transactions t {join} WHERE {where}", params)
    return succ / tot


MJ = "LEFT JOIN merchants m ON t.merchant_id = m.merchant_id"
BJ = "LEFT JOIN banks b ON t.bank_id = b.bank_id"


def _close(a, b, tol=1e-4):
    return a is not None and b is not None and abs(a - b) <= tol


CASES: List[dict] = [
    {"q": "Total transactions in Aug 2026?",
     "truth": lambda: _truth("SELECT COUNT(DISTINCT transaction_id) FROM transactions WHERE txn_timestamp >= '2026-08-01' "
                             "AND txn_timestamp < '2026-09-01'")[0],
     "actual": lambda r: r["analysis"]["overall_a"]["total"], "cmp": lambda a, t: a == t},
    {"q": "What was the success rate in Oct 2026?",
     "truth": lambda: _rate("t.txn_timestamp >= '2026-10-01' AND t.txn_timestamp < '2026-11-01'"),
     "actual": lambda r: r["analysis"]["overall_a"]["success_rate"], "cmp": _close},
    {"q": "Success rate for merchant category Retail in Sep 2026?",
     "truth": lambda: _rate("t.txn_timestamp >= '2026-09-01' AND t.txn_timestamp < '2026-10-01' AND UPPER(m.category)='RETAIL'",
                            join=MJ),
     "actual": lambda r: r["analysis"]["overall_a"]["success_rate"], "cmp": _close},
    {"q": "What was the UPI success rate in October?",
     "truth": lambda: _rate("t.txn_timestamp >= '2026-10-01' AND t.txn_timestamp < '2026-11-01' AND t.payment_method='UPI'"),
     "actual": lambda r: r["analysis"]["overall_a"]["success_rate"], "cmp": _close},
    {"q": "What is the failure rate for Card payments in Sep 2026?",
     "truth": lambda: 1 - _rate("t.txn_timestamp >= '2026-09-01' AND t.txn_timestamp < '2026-10-01' AND t.payment_method='Card'"),
     "actual": lambda r: 1 - r["analysis"]["overall_a"]["success_rate"], "cmp": _close},
    {"q": "Show me success rates by bank",
     "truth": lambda: _rate("t.txn_timestamp IS NOT NULL AND b.bank_name='Cobalt Bank'", join=BJ),
     "actual": lambda r: float(r["analysis"]["table"].set_index("segment").loc["Cobalt Bank", "success_rate"]), "cmp": _close},
    {"q": "Show the monthly success rate trend",
     "truth": lambda: _rate("t.txn_timestamp >= '2026-10-01' AND t.txn_timestamp < '2026-11-01'"),
     "actual": lambda r: float(r["analysis"]["trend"].set_index("month").loc["2026-10", "success_rate"]), "cmp": _close},
    {"q": "Why did our overall success rate fall from Sept to Oct?",
     "truth": lambda: (_rate("t.txn_timestamp >= '2026-10-01' AND t.txn_timestamp < '2026-11-01'")
                       - _rate("t.txn_timestamp >= '2026-09-01' AND t.txn_timestamp < '2026-10-01'"), "UPI @ Cobalt Bank"),
     "actual": lambda r: (r["analysis"]["decompositions"]["payment_method"].delta,
                          r["analysis"]["driver"][1].segment if r["analysis"]["driver"] else None),
     "cmp": lambda a, t: _close(a[0], t[0]) and a[1] == t[1]},
    {"q": "Did our merchant mix shift from July to August?",
     "truth": lambda: "mix share > 80%",
     "actual": lambda r: r["analysis"]["decompositions"]["merchant_category"].mix_share,
     "cmp": lambda a, t: a > 0.8},
    {"q": "Do the gateway and settlement sources disagree in October?",
     "truth": lambda: 30 + 20 - 12,
     "actual": lambda r: r["reconciliation"]["gap"], "cmp": lambda a, t: a == t},
    {"q": "Show me success rates by bank", "role": "merchant_analyst",
     "truth": lambda: "access_denied", "actual": lambda r: r["status"], "cmp": lambda a, t: a == t},
    {"q": "Who are the problematic banks?",
     "truth": lambda: "needs_clarification", "actual": lambda r: r["status"], "cmp": lambda a, t: a == t},
    {"q": "Why did success rate drop recently?",
     "truth": lambda: "needs_clarification", "actual": lambda r: r["status"], "cmp": lambda a, t: a == t},
]


def run_eval(provider=None, progress: Callable = None) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(CASES):
        role = c.get("role", "analyst")
        t0 = time.perf_counter()
        try:
            rep = answer(c["q"], role, DEFAULT, provider)
            truth, actual = c["truth"](), c["actual"](rep)
            passed = bool(c["cmp"](actual, truth))
            sql_valid = rep.get("primary", {}).get("validation", {}).get("ok", rep["status"] == "needs_clarification")
            grounded = rep.get("narrative", {}).get("summary_source", "template") != \
                "LLM summary rejected by grounding check (contained a number not in computed evidence)"
            err = rep.get("error")
        except Exception as e:  # a crash is a failed case, not a crashed suite
            truth, actual, passed, sql_valid, grounded, err = None, None, False, False, True, f"{type(e).__name__}: {e}"
        rows.append({"question": c["q"], "role": role, "passed": passed, "expected": _fmt(truth), "actual": _fmt(actual),
                     "sql_valid_or_expected_block": bool(sql_valid) or role != "analyst",
                     "grounded": grounded, "latency_s": round(time.perf_counter() - t0, 2), "error": err})
        if progress:
            progress((i + 1) / len(CASES))
    return pd.DataFrame(rows)


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4f}"
    if isinstance(v, tuple):
        return ", ".join(_fmt(x) for x in v)
    return str(v)
