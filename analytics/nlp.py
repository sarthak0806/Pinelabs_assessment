"""Question understanding: NL question -> structured intent (+ optional LLM-drafted SQL).

Two paths, same output schema:
  * rules - deterministic parser (offline mode, fallback, and an always-on ambiguity guard)
  * LLM   - one call to the active provider (OpenRouter / Claude / Kimi) returning intent + SQL as JSON
            (schema-aware prompt); merged with and sanity-checked against the rules result.
            LLM SQL is always validated before use.
"""
from __future__ import annotations

import calendar
import json
import re
from typing import Optional, Tuple

import config
from analytics.catalog import DIMENSIONS, MEASURES, relevant_tables, schema_prompt

MONTH_RE = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                      r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b")
MONTH_NUM = {m[:3].lower(): i for i, m in enumerate(calendar.month_abbr) if m}
METHOD_WORDS = {"upi": "UPI", "card": "Card", "cards": "Card", "wallet": "Wallet", "wallets": "Wallet",
                "netbanking": "NetBanking", "net banking": "NetBanking"}
CATEGORY_WORDS = {"retail": "Retail", "food": "Food", "travel": "Travel", "utilities": "Utilities",
                  "utility": "Utilities", "electronics": "Electronics"}
REGION_WORDS = {"north": "North", "south": "South", "east": "East", "west": "West"}
BANK_WORDS = {"alpha": "Alpha Bank", "beacon": "Beacon Bank", "cobalt": "Cobalt Bank", "delta": "Delta Bank",
              "ember": "Ember Bank", "falcon": "Falcon Bank", "granite": "Granite Bank", "harbor": "Harbor Bank",
              "iris": "Iris Bank", "juniper": "Juniper Bank"}
CHANGE_WORDS = ("why", "drop", "fell", "fall", "declin", "decreas", "dip", "change", "caus", "driver", "explain",
                "contribut", "shift", "increas", "improv", "compare", " vs", "versus", "worse", "better")
VAGUE_TIME = ("recently", "lately", "these days", "of late", "nowadays", "recent ")
VAGUE_METRIC = ("problematic", "worst", "underperform", "bad bank", "bad merchant", "issues", "troubled", "struggling")


def month_period(year: int, month: int) -> dict:
    start = f"{year}-{month:02d}-01"
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return {"start": start, "end": f"{ny}-{nm:02d}-01", "label": f"{calendar.month_abbr[month]} {year}"}


def _latest():
    y, m = map(int, config.LATEST_MONTH.split("-"))
    return y, m


def _prev(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _has(q, *words):
    return any(w in q for w in words)


def parse_periods(q: str, comparison: bool):
    """Returns (period_a, period_b, assumptions, needs_time_clarification)."""
    assumptions = []
    y = config.DATA_YEAR
    months = [MONTH_NUM[m[:3]] for m in MONTH_RE.findall(q)]
    months = [m for i, m in enumerate(months) if m not in months[:i]]
    ly, lm = _latest()
    q3 = re.search(r"\bq([1-4])\b", q)
    if "last two months" in q or "last 2 months" in q:
        py, pm = _prev(ly, lm)
        return month_period(ly, lm), month_period(py, pm), ["'Last two months' = latest month vs the one before."], False
    if "this month" in q and "last month" in q:
        py, pm = _prev(ly, lm)
        return month_period(ly, lm), month_period(py, pm), ["'This month' = latest month in data (Oct 2026)."], False
    if "last month" in q and not months:
        py, pm = _prev(ly, lm)
        if comparison:
            ppy, ppm = _prev(py, pm)
            return month_period(py, pm), month_period(ppy, ppm), [], False
        return month_period(py, pm), None, ["'Last month' = Sep 2026 (latest complete month before Oct)."], False
    if "this month" in q and not months:
        if comparison:
            py, pm = _prev(ly, lm)
            return month_period(ly, lm), month_period(py, pm), ["'This month' = Oct 2026 (latest in data)."], False
        return month_period(ly, lm), None, ["'This month' = Oct 2026 (latest in data)."], False
    if q3 and not months:
        qn = int(q3.group(1))
        p = {"start": f"{y}-{3 * qn - 2:02d}-01", "end": f"{y + (qn == 4)}-{(3 * qn) % 12 + 1:02d}-01",
             "label": f"Q{qn} {y}"}
        return p, None, [], False
    if len(months) >= 2:
        a, b = max(months[:2]), min(months[:2])
        return month_period(y, a), month_period(y, b), [], False
    if len(months) == 1:
        if comparison:
            py, pm = _prev(y, months[0])
            return month_period(y, months[0]), month_period(py, pm), \
                [f"Compared {calendar.month_abbr[months[0]]} with the previous month."], False
        return month_period(y, months[0]), None, [], False
    if comparison and _has(q, *VAGUE_TIME):
        return None, None, [], True
    return None, None, assumptions, False


def rules_intent(question: str, default_periods: Optional[Tuple[dict, dict]] = None) -> dict:
    q = " " + question.lower().strip() + " "
    comparison = _has(q, *CHANGE_WORDS)
    pa, pb, assumptions, vague_time = parse_periods(q, comparison)

    # ---- dimensions & filters ---------------------------------------------------------
    group_by, filters = [], {}
    if _has(q, "payment method", " method", "by upi", "channel"):
        group_by.append("payment_method")
    if re.search(r"\bbanks?\b|issuer", q) and not any(b in q for b in BANK_WORDS):
        group_by.append("bank")
    if _has(q, "categor", "merchant mix", " mix", "segment", "vertical"):
        group_by.append("merchant_category")
    if _has(q, "region"):
        group_by.append("region")
    if re.search(r"\bmerchants?\b", q) and not _has(q, "categor", "mix"):
        group_by.append("merchant")
    if _has(q, "failure reason", "reasons", "error"):
        group_by.append("failure_reason")
    # A named value ("category Retail", "payment method UPI") is a filter unless the user asked "by <dim>".
    for words, key, by_words in ((METHOD_WORDS, "payment_method", ("by payment method", "by method", "each method")),
                                 (CATEGORY_WORDS, "merchant_category", ("by categor", "by merchant categor", "each categor",
                                                                        "mix", "segment"))):
        named = [v for w, v in words.items() if re.search(rf"\b{w}\b", q)]
        if named and not _has(q, *by_words):
            filters[key] = named[0]
            if key in group_by:
                group_by.remove(key)
    for w, v in REGION_WORDS.items():
        if re.search(rf"\b{w}\b", q) and "region" not in group_by:
            filters["region"] = v
    for w, v in BANK_WORDS.items():
        if re.search(rf"\b{w}\b", q):
            filters["bank"] = v

    # ---- analysis type & metric ----------------------------------------------------
    if _has(q, "settle", "reconcil", "disagree", "mismatch", "two sources", "sources"):
        atype = "reconciliation"
    elif comparison and (pa and pb or vague_time or default_periods):
        atype = "rate_change"
    elif _has(q, "trend", "over time", "monthly", "month by month", "each month", "per month"):
        atype = "trend"
    elif group_by:
        atype = "segment_breakdown"
    else:
        atype = "lookup"

    if _has(q, "failure rate", "failures", "failed", "decline rate"):
        metric = "failure_rate"
    elif _has(q, " value", "amount", "gmv", "revenue", " inr"):
        metric = "txn_value"
    elif _has(q, "how many", "count", "number of", "volume", "total transactions"):
        metric = "txn_count"
    else:
        metric = "success_rate"

    clarification = None
    if vague_time:
        clarification = {
            "field": "time_period",
            "question": "Which time window do you mean by 'recently'? The data covers May–Oct 2026.",
            "options": [
                {"label": "Oct 2026 vs Sep 2026 (latest two months)", "rewrite": f"{question} (Oct vs Sep)"},
                {"label": "Aug 2026 vs Jul 2026", "rewrite": f"{question} (Aug vs Jul)"},
                {"label": "Monthly trend May–Oct 2026", "rewrite": "Show the monthly success rate trend"},
            ],
        }
    elif _has(q, *VAGUE_METRIC):
        plural, single = (("banks", "bank") if "bank" in q else
                          ("merchants", "merchant") if "merchant" in q else ("payment methods", "payment method"))
        clarification = {
            "field": "metric",
            "question": f"'Problematic' is ambiguous - which measure should rank the {plural}?",
            "options": [
                {"label": "Highest failure rate in Oct 2026", "rewrite": f"Failure rate by {single} in October"},
                {"label": "Largest success-rate drop Sep → Oct 2026",
                 "rewrite": f"Why did success rate drop from Sep to Oct by {single}?"},
                {"label": "Most failed transactions in Oct 2026",
                 "rewrite": f"Number of failed transactions by {single} in October"},
            ],
        }

    if atype == "rate_change" and not (pa and pb) and not clarification:
        if default_periods:
            pa, pb = default_periods
            assumptions.append(f"No period stated - compared {pa['label']} with {pb['label']} (sidebar selection).")
    if atype in ("segment_breakdown", "lookup") and not pa:
        assumptions.append("No period stated - used all available data (May–Oct 2026).")
    if atype == "reconciliation" and not pa:
        ly, lm = _latest()
        pa = month_period(ly, lm)
        assumptions.append(f"Reconciliation period defaulted to {pa['label']}.")
    if atype == "rate_change" and "payment_method" in filters and not group_by:
        group_by = ["bank"]
    if metric in ("success_rate", "failure_rate"):
        assumptions.append("Success rate = SUCCESS / unique transaction attempts (duplicate IDs counted once).")

    tables = set(relevant_tables(question))
    for g in group_by:
        tables |= DIMENSIONS[g][2]
    if "bank" in filters:
        tables.add("banks")
    if any(k in filters for k in ("merchant_category", "region")):
        tables.add("merchants")
    if atype == "reconciliation":
        tables.add("settlements")

    return {
        "question": question, "analysis_type": atype, "metric": metric,
        "period_a": pa, "period_b": pb, "group_by": group_by, "filters": filters,
        "datasets": sorted(tables), "assumptions": assumptions,
        "clarification": clarification, "source": "rules", "sql": None, "params": [],
    }


# --------------------------------------------------------------------------- LLM path
SYSTEM_PROMPT = """You are the query planner of a payments analytics tool. Convert the business question into
a JSON object. Data is SYNTHETIC and covers {first} to {last} ({latest} is the latest month).

Schema (SQLite):
{schema}

Measures: {measures}
Allowed group_by keys: {dims}
Allowed filter keys: payment_method (UPI|Card|Wallet|NetBanking), bank (bank_name), merchant_category, region.

Return ONLY this JSON object:
{{
 "analysis_type": "rate_change" | "segment_breakdown" | "trend" | "lookup" | "reconciliation",
 "metric": "success_rate" | "failure_rate" | "txn_count" | "txn_value",
 "period_a": {{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","label":"Mon YYYY"}} or null,   // later / current period, end exclusive
 "period_b": {{...}} or null,                                                         // earlier / baseline period
 "group_by": [..], "filters": {{..}},
 "ambiguous": false | true, "clarifying_question": null | "...",
 "sql": "one SQLite SELECT answering the question",
 "params": [..]
}}
SQL rules: SELECT only; use table aliases t (transactions), m (merchants), b (banks), s (settlements);
use ? placeholders for EVERY literal value from the question (dates, method, bank, category) and list them in params in order;
compute success rate as COUNT(DISTINCT CASE WHEN t.status='SUCCESS' THEN t.transaction_id END)*100.0/COUNT(DISTINCT t.transaction_id);
filter time with t.txn_timestamp >= ? AND t.txn_timestamp < ?; no comments, no semicolons, no UNION.
Set ambiguous=true only if the time period or the metric/denominator genuinely cannot be inferred."""


def llm_intent(question: str, provider, default_periods=None) -> Tuple[dict, Optional[str]]:
    """Returns (intent, note). Always computes the rules intent; LLM refines it when available."""
    base = rules_intent(question, default_periods)
    if provider.name == "mock":
        return base, None
    prompt = SYSTEM_PROMPT.format(first="May 2026", last="Oct 2026", latest="Oct 2026",
                                  schema=schema_prompt(base["datasets"]), measures=json.dumps(MEASURES),
                                  dims=list(DIMENSIONS))
    try:
        from utils.llm import extract_json
        text = provider.complete([{"role": "system", "content": prompt},
                                  {"role": "user", "content": question}], json_mode=True)
        data = extract_json(text)
    except Exception as e:
        return base, f"LLM intent failed ({str(e)[:120]}); used rules parser."

    merged = dict(base)
    merged["source"] = provider.name
    if data.get("analysis_type") in ("rate_change", "segment_breakdown", "trend", "lookup", "reconciliation"):
        merged["analysis_type"] = data["analysis_type"]
    if data.get("metric") in ("success_rate", "failure_rate", "txn_count", "txn_value"):
        merged["metric"] = data["metric"]
    for k in ("period_a", "period_b"):
        p = data.get(k)
        if isinstance(p, dict) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(p.get("start", ""))) \
                and re.match(r"^\d{4}-\d{2}-\d{2}$", str(p.get("end", ""))):
            merged[k] = {"start": p["start"], "end": p["end"], "label": str(p.get("label") or p["start"][:7])}
    gb = [g for g in (data.get("group_by") or []) if g in DIMENSIONS]
    if gb:
        merged["group_by"] = gb
    if isinstance(data.get("filters"), dict):
        merged["filters"] = {k: v for k, v in data["filters"].items()
                             if k in ("payment_method", "bank", "merchant_category", "region") and isinstance(v, str)}
    if data.get("ambiguous") and data.get("clarifying_question") and not merged["clarification"]:
        merged["clarification"] = {"field": "llm", "question": str(data["clarifying_question"]), "options": []}
    if isinstance(data.get("sql"), str) and data["sql"].strip():
        merged["sql"] = data["sql"].strip()
        merged["params"] = data.get("params") if isinstance(data.get("params"), list) else []
    if merged["analysis_type"] == "rate_change" and not (merged["period_a"] and merged["period_b"]):
        if default_periods:
            merged["period_a"], merged["period_b"] = default_periods
    for g in merged["group_by"]:
        merged["datasets"] = sorted(set(merged["datasets"]) | DIMENSIONS[g][2])
    return merged, None
