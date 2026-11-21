"""End-to-end orchestration: question -> intent -> SQL -> validate -> execute -> DQ -> analysis -> narrative.

Returns a plain dict "report" consumed by the Streamlit UI, the tests and the audit log.
Every step is recorded (name, status, detail, ms) so the UI can show the analysis steps.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

import config
from analytics import analysis as an
from analytics import narrator, quality, sqlgen
from analytics.nlp import llm_intent
from analytics.security import SQLValidationError, validate_sql
from utils import audit
from utils.db import QueryTimeout, get_db
from utils.llm import get_provider

RATE_CHANGE_DIMS = ["payment_method", "bank", "merchant_category", "region", "method_x_bank"]


class Steps:
    def __init__(self):
        self.items = []
        self._t = time.perf_counter()

    def add(self, name, status, detail=""):
        now = time.perf_counter()
        self.items.append({"name": name, "status": status, "detail": detail, "ms": round((now - self._t) * 1000, 1)})
        self._t = now


def _log_query(report, purpose, sql, params, res, source):
    report.setdefault("queries", []).append({"purpose": purpose, "sql": sql, "params": list(params),
                                             "rows": res.row_count, "ms": round(res.elapsed_ms, 1),
                                             "cached": res.cached, "source": source})


def _finish(report, steps, t0):
    report["steps"] = steps.items
    report["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    p = report.get("primary") or {}
    audit.log({"question": report["question"], "role": report["role"], "status": report["status"],
               "llm": report.get("llm", {}).get("mode"), "intent_source": (report.get("intent") or {}).get("source"),
               "sql": p.get("sql"), "params": p.get("params"), "sql_source": p.get("source"),
               "rows": p.get("rows"), "elapsed_ms": report["elapsed_ms"], "error": report.get("error")})
    return report


def answer(question: str, role: str = config.DEFAULT_ROLE,
           default_periods: Optional[Tuple[dict, dict]] = None, provider=None) -> dict:
    t0 = time.perf_counter()
    steps = Steps()
    db = get_db()
    if provider is None:
        provider, llm_note = get_provider()
    else:
        llm_note = provider.name
    report = {"question": question, "role": role, "status": "ok", "llm": {"mode": provider.name, "note": llm_note,
              "notes": []}}

    # 1. understand ---------------------------------------------------------------------
    intent, note = llm_intent(question, provider, default_periods)
    if note:
        report["llm"]["notes"].append(note)
    report["intent"] = intent
    steps.add("Understand question", "ok", f"{intent['analysis_type']} · {intent['metric']} · via {intent['source']}")
    if intent.get("clarification"):
        report["status"] = "needs_clarification"
        steps.add("Ambiguity check", "warn", intent["clarification"]["question"])
        return _finish(report, steps, t0)
    steps.add("Ambiguity check", "ok", "; ".join(intent["assumptions"][:2]) or "unambiguous")
    steps.add("Select datasets", "ok", ", ".join(intent["datasets"]))

    # 2. draft + validate SQL -----------------------------------------------------------
    tmpl_sql, tmpl_params = sqlgen.primary_sql(intent, role)
    sql, params, source = tmpl_sql, tmpl_params, "template"
    report["queries"] = []                     # every query executed for this answer (shown in Query Flow)
    report["template_sql"] = {"sql": tmpl_sql, "params": list(tmpl_params)}
    if intent.get("sql"):
        v_llm = validate_sql(intent["sql"], intent.get("params") or [], role=role)
        report["llm_sql"] = {"sql": intent["sql"], "params": list(intent.get("params") or []), "model": provider.name,
                             "ok": v_llm.ok, "errors": v_llm.errors, "access_denied": v_llm.access_denied}
        if v_llm.ok or v_llm.access_denied:
            sql, params, source = intent["sql"], intent.get("params") or [], provider.name
        else:
            report["llm"]["notes"].append("LLM SQL rejected by validator: " + "; ".join(v_llm.errors[:2])
                                          + " - used deterministic template instead.")
    steps.add("Draft SQL", "ok", f"source: {source}")
    v = validate_sql(sql, params, role=role)
    report["primary"] = {"sql": v.sql or sql, "params": list(params), "source": source,
                         "validation": {"ok": v.ok, "errors": v.errors, "warnings": v.warnings,
                                        "tables": v.tables}}
    if not v.ok:
        report["status"] = "access_denied" if v.access_denied else "rejected"
        report["error"] = "; ".join(v.errors)
        steps.add("Validate SQL", "fail", report["error"])
        return _finish(report, steps, t0)
    steps.add("Validate SQL", "ok", "SELECT-only · allowlist · role · cost guard · " + "; ".join(v.warnings))

    # 3. execute -------------------------------------------------------------------------
    try:
        res = db.execute(v.sql, params, role=role)
    except SQLValidationError as e:
        report.update(status="access_denied", error=str(e))
        steps.add("Execute (read-only)", "fail", str(e))
        return _finish(report, steps, t0)
    except QueryTimeout as e:
        report.update(status="rejected", error=str(e))
        steps.add("Execute (read-only)", "fail", str(e))
        return _finish(report, steps, t0)
    except Exception as e:
        if source != "template":   # LLM SQL failed at runtime -> fall back to template once
            report["llm"]["notes"].append(f"LLM SQL failed at execution ({str(e)[:100]}); used template.")
            v = validate_sql(tmpl_sql, tmpl_params, role=role)
            res = db.execute(v.sql, tmpl_params, role=role)
            report["primary"].update(sql=v.sql, params=list(tmpl_params), source="template")
        else:
            report.update(status="error", error=str(e))
            steps.add("Execute (read-only)", "fail", str(e))
            return _finish(report, steps, t0)
    report["primary"].update(df=res.df, rows=res.row_count, elapsed_ms=round(res.elapsed_ms, 1), cached=res.cached)
    _log_query(report, "Answer query", report["primary"]["sql"], report["primary"]["params"], res,
               report["primary"]["source"])
    steps.add("Execute (read-only)", "ok", f"{res.row_count} rows · {res.elapsed_ms:.0f} ms" + (" · cached" if res.cached else ""))
    if res.row_count == 0:
        report["status"] = "no_results"

    atype = intent["analysis_type"]

    # 4a. reconciliation is a two-source comparison ------------------------------------------
    if atype == "reconciliation":
        pa = intent["period_a"]
        try:
            r = quality.reconcile_settlements(db, role, pa["start"], pa["end"])
        except SQLValidationError as e:
            report.update(status="access_denied", error=str(e))
            steps.add("Reconcile sources", "fail", str(e))
            return _finish(report, steps, t0)
        report["reconciliation"] = r
        report["queries"].append({"purpose": "Reconciliation (transactions ⋈ settlements)", "sql": r["sql"],
                                  "params": r["params"], "rows": 1, "ms": None, "source": "template"})
        steps.add("Reconcile sources", "warn" if r["gap"] else "ok", f"gap {r['gap']} transactions")
        report["narrative"] = narrator.build_reconciliation(intent, r)
        _summarize(report, provider, steps)
        return _finish(report, steps, t0)

    # 4b. row-level evidence for the analytics engine ----------------------------------------
    ev_sql, ev_params = sqlgen.evidence_sql(intent, role)
    ev = db.safe_query(ev_sql, ev_params, role=role, max_rows=config.MAX_EVIDENCE_ROWS)
    raw = ev.df
    report["evidence"] = {"sql": ev.sql, "params": ev_params, "rows": len(raw)}
    _log_query(report, "Row-level evidence for the analytics engine", ev.sql, ev_params, ev, "template")
    steps.add("Fetch evidence rows", "ok", f"{len(raw):,} rows")

    checks = quality.result_checks(raw) + quality.dataset_checks(db, role)
    pa, pb = intent.get("period_a"), intent.get("period_b")
    df, prep = an.prepare(raw, (pa["start"], pa["end"]) if pa else None, (pb["start"], pb["end"]) if pb else None)
    dq = quality.summarize(checks, len(raw))
    report["quality"] = {"checks": [c.as_dict() for c in checks], "summary": dq, "prep_notes": prep.notes}
    steps.add("Data-quality checks", "warn" if dq["issues"] else "ok", f"{dq['issues']} issue type(s)")

    # 5. analysis --------------------------------------------------------------------------
    tr_sql, tr_params = sqlgen.trend_sql(intent, role)
    tr = db.safe_query(tr_sql, tr_params, role=role)
    report["trend"] = tr.df
    _log_query(report, "Monthly trend (for the trend chart)", tr.sql, tr_params, tr, "template")
    if len(raw) == 0:
        report["status"] = "no_results"
        report["narrative"] = {"headline": "No transactions match this question's scope.", "facts": [], "hypotheses": [],
                               "recommendations": [], "caveats": [], "next_questions": []}
        steps.add("Analyse", "warn", "no rows")
        return _finish(report, steps, t0)

    if atype == "rate_change" and pa and pb:
        a, b = df[df.period == "A"], df[df.period == "B"]
        oa, ob = an.overall(a), an.overall(b)
        dims = intent["group_by"] + [d for d in RATE_CHANGE_DIMS if d not in intent["group_by"]]
        if intent["filters"].get("payment_method"):
            dims = [d for d in dims if d not in ("payment_method", "method_x_bank")]
        decs = an.decompose_all(df, dims)
        driver = an.top_rate_driver(decs)
        delta_ab = (oa["success_rate"] or 0) - (ob["success_rate"] or 0)
        if driver and (not delta_ab or driver[1].rate_contrib / delta_ab < 0.25):
            driver = None          # no single segment explains >= 25% of the change -> don't headline one
        mix = an.mix_story(decs)
        reasons_a = None
        if driver:
            dcol = an._dim_col(df, driver[0])
            seg = df[(df.period == "A") & (df[dcol].astype(str) == str(driver[1].segment))]
            reasons_a = an.failure_reasons(seg)
        low_conf = sorted({str(s) for d in decs.values() if not d.empty_period
                           for s in d.table.loc[d.table.min_n < config.LOW_SAMPLE_N, "segment"]})
        report["analysis"] = {"type": "rate_change", "overall_a": oa, "overall_b": ob, "decompositions": decs,
                              "driver": driver, "mix": mix, "reasons_a": an.failure_reasons(a),
                              "top_affected": an.top_affected(df, intent["group_by"][0] if intent["group_by"] else
                                                              ("bank" if "bank_name" in df else "merchant_category")),
                              "low_confidence": low_conf}
        steps.add("Analyse", "ok", "Kitagawa decomposition on " + ", ".join(decs) +
                  (" · exact-sum check passed" if all(d.check_ok for d in decs.values() if not d.empty_period) else ""))
        report["narrative"] = narrator.build_rate_change(intent, oa, ob, decs, driver, mix, reasons_a,
                                                         prep.notes, dq, low_conf)
    elif atype == "trend":
        tr = an.trend(df)
        report["analysis"] = {"type": "trend", "trend": tr, "reasons_a": an.failure_reasons(df)}
        steps.add("Analyse", "ok", "monthly trend")
        report["narrative"] = narrator.build_trend(intent, tr, prep.notes, dq)
    elif intent["group_by"]:
        dim = intent["group_by"][0]
        scope = df[df.period == "A"] if pa and pb else df
        table = an.rate_table(scope, dim)
        low_conf = table.loc[table.total < config.LOW_SAMPLE_N, "segment"].astype(str).tolist()
        report["analysis"] = {"type": "segment_breakdown", "dimension": dim, "table": table,
                              "overall_a": an.overall(scope), "reasons_a": an.failure_reasons(scope),
                              "top_affected": an.top_affected(df, dim) if pa and pb else table.sort_values(
                                  "failures", ascending=False).head(10)}
        steps.add("Analyse", "ok", f"rates by {dim} with 95% CI")
        report["narrative"] = narrator.build_breakdown(intent, table, dim, an.overall(scope), prep.notes, dq, low_conf,
                                                       intent["metric"])
    else:
        stats = an.overall(df)
        report["analysis"] = {"type": "lookup", "overall_a": stats, "reasons_a": an.failure_reasons(df),
                              "value_total": float(df["amount"].sum())}
        steps.add("Analyse", "ok", "overall metric")
        report["narrative"] = narrator.build_lookup(intent, stats, intent["metric"], float(df["amount"].sum()),
                                                    prep.notes, dq)
    _summarize(report, provider, steps)
    return _finish(report, steps, t0)


def _summarize(report, provider, steps):
    summary, how = narrator.llm_summary(report["narrative"], provider)
    report["narrative"]["summary"] = summary
    report["narrative"]["summary_source"] = how
    if summary is None and how not in ("template",):
        report["llm"]["notes"].append(how)
    steps.add("Narrate (grounded)", "ok" if summary or how == "template" else "warn",
              "LLM summary passed grounding check" if summary else how)


def kpis(role: str, period_a: dict, period_b: dict) -> dict:
    """Header KPI cards for the sidebar-selected periods (deterministic)."""
    db = get_db()
    intent = {"period_a": period_a, "period_b": period_b, "filters": {}}
    sql, params = sqlgen.evidence_sql(intent, role)
    raw = db.safe_query(sql, params, role=role, max_rows=config.MAX_EVIDENCE_ROWS).df
    df, _ = an.prepare(raw, (period_a["start"], period_a["end"]), (period_b["start"], period_b["end"]))
    oa, ob = an.overall(df[df.period == "A"]), an.overall(df[df.period == "B"])
    checks = quality.result_checks(raw)
    dq = quality.summarize(checks, len(raw))
    return {"a": oa, "b": ob, "dq": dq, "value_a": float(df.loc[df.period == "A", "amount"].sum())}
