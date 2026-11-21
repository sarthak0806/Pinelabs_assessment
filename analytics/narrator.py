"""Answer assembly. Facts / hypotheses / recommendations / caveats are built DETERMINISTICALLY from
computed results, each with an evidence tag (datasets · filters · time window · n).
The LLM may only rewrite the executive summary, and its output is rejected if it contains any
number that does not appear in the computed facts (hallucination guard).
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from analytics.analysis import DIM_LABELS


def pct(x, d=1):
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def pt(x, d=2):
    return "n/a" if x is None else f"{x * 100:+.{d}f}pt"


def evidence_tag(intent: dict, n: Optional[int] = None, extra: str = "") -> str:
    ds = " ⋈ ".join(intent.get("datasets") or ["transactions"])
    f = intent.get("filters") or {}
    filt = ", ".join(f"{k}={v}" for k, v in f.items()) or "none"
    pa, pb = intent.get("period_a"), intent.get("period_b")
    window = f"{pa['label']} vs {pb['label']}" if pa and pb else (pa["label"] if pa else "May–Oct 2026")
    s = f"Source: {ds} · filters: {filt} · window: {window}"
    if n is not None:
        s += f" · n={n:,}"
    if extra:
        s += f" · {extra}"
    return s


def _item(text, evidence):
    return {"text": text, "evidence": evidence}


def build_rate_change(intent, oa, ob, decs, driver, mix, reasons_a, prep_notes, dq_summary, low_conf) -> dict:
    facts, hyp, recs, cav, nxt = [], [], [], [], []
    pa, pb = intent["period_a"]["label"], intent["period_b"]["label"]
    delta = (oa["success_rate"] or 0) - (ob["success_rate"] or 0)
    direction = "fell" if delta < 0 else "rose"
    headline = (f"Success rate {direction} from {pct(ob['success_rate'])} in {pb} to {pct(oa['success_rate'])} "
                f"in {pa} ({pt(delta)}).")
    facts.append(_item(f"{pb}: {ob['successes']:,} of {ob['total']:,} transactions succeeded = {pct(ob['success_rate'], 2)} "
                       f"(95% CI {pct(ob['ci_low'])}–{pct(ob['ci_high'])}).", evidence_tag(intent, ob["total"])))
    facts.append(_item(f"{pa}: {oa['successes']:,} of {oa['total']:,} transactions succeeded = {pct(oa['success_rate'], 2)} "
                       f"(95% CI {pct(oa['ci_low'])}–{pct(oa['ci_high'])}).", evidence_tag(intent, oa["total"])))

    for dim in ("payment_method", "merchant_category", "bank"):
        d = decs.get(dim)
        if not d or d.empty_period or not d.delta:
            continue
        facts.append(_item(
            f"By {DIM_LABELS[dim].lower()}: rate effect {pt(d.rate_effect)} ({d.rate_share:.0%} of change), "
            f"mix effect {pt(d.mix_effect)} ({d.mix_share:.0%}); sums exactly to {pt(d.delta)}.",
            evidence_tag(intent, d.n_A + d.n_B, "Kitagawa decomposition")))

    if driver:
        dim, row, _ = driver
        share = row.rate_contrib / delta if delta else 0
        facts.append(_item(
            f"Largest single driver: {row.segment} ({DIM_LABELS.get(dim, dim)}) - success rate {pct(row.r_B)} → {pct(row.r_A)} "
            f"(n={int(row.n_B):,} → {int(row.n_A):,}), contributing {pt(row.rate_contrib)} ({share:.0%} of the change) via rate effect.",
            evidence_tag(intent, int(row.n_A + row.n_B), f"segment {row.segment}")))
    if mix:
        mdec, mover = mix
        facts.append(_item(
            f"Mix shift ({DIM_LABELS.get(mdec.dimension, mdec.dimension).lower()}): {mover.segment} share moved "
            f"{pct(mover.w_B)} → {pct(mover.w_A)} of volume (its success rate ≈ {pct(mover.r_bar)}); "
            f"total mix effect {pt(mdec.mix_effect)}.",
            evidence_tag(intent, mdec.n_A + mdec.n_B, "mix effect")))

    # ---- hypotheses (clearly labelled as such) ----
    if driver:
        dim, row, _ = driver
        top_reason = None
        if reasons_a is not None and len(reasons_a):
            top_reason = reasons_a.iloc[0]
        if top_reason is not None:
            hyp.append(_item(
                f"Plausible: the drop at {row.segment} is technical (gateway/issuer) rather than customer-driven - "
                f"the most common failure reason in that segment in {pa} is '{top_reason['failure_reason']}' "
                f"({int(top_reason['failures'])} failures).", evidence_tag(intent, None, f"failure reasons, {row.segment}")))
        recs.append(_item(f"Investigate {row.segment}: pull gateway/issuer logs for {pa}, check timeouts and error codes, "
                          f"and open an incident with the bank/PSP if confirmed.", "Based on driver above"))
        nxt.append(f"Show daily success rate for {row.segment} in {pa}")
    if mix and abs(mix[0].mix_effect) > 0.25 * abs(delta):
        mdec, mover = mix
        hyp.append(_item(f"Part of the change is compositional: more volume from lower-success segments "
                         f"(e.g. {mover.segment}) lowers the blended rate even if no system degraded.",
                         evidence_tag(intent, None, "mix effect")))
        recs.append(_item(f"Review the {mover.segment} portfolio (onboarding, retries, routing) rather than treating the "
                          f"mix-driven part as an outage.", "Based on mix effect above"))
    if mix and driver is None or (mix and abs(mix[0].mix_effect) >= 0.8 * abs(delta)):
        recs.append(_item("Do not escalate as a system failure: per-segment success rates are broadly stable; "
                          "the decline is explained by transaction mix.", "Kitagawa: mix share ≥ 80%"))
    nxt.append("Which merchants had the most failures?")

    cav += _common_caveats(prep_notes, dq_summary, low_conf)
    return {"headline": headline, "facts": facts, "hypotheses": hyp, "recommendations": recs,
            "caveats": cav, "next_questions": nxt}


def build_breakdown(intent, table, dim, overall_stats, prep_notes, dq_summary, low_conf, metric) -> dict:
    facts, recs, cav = [], [], []
    window = intent["period_a"]["label"] if intent.get("period_a") else "May–Oct 2026"
    label = DIM_LABELS.get(dim, dim)
    headline = (f"Overall success rate {pct(overall_stats['success_rate'])} across {overall_stats['total']:,} transactions "
                f"({window}); broken down by {label.lower()}.")
    reliable = table[(table.total >= 30) & ~table.segment.astype(str).str.match(r"^(Unknown|Other|Invalid)")]
    if len(reliable) < len(table):
        cav.append(_item("Segments with <30 transactions or data-quality buckets (Unknown/Other/Invalid) are excluded "
                         "from best/worst rankings.", "Wilson 95% CI / data quality"))
    if len(reliable):
        table = reliable
        worst = table.sort_values("success_rate").iloc[0]
        best = table.sort_values("success_rate").iloc[-1]
        facts.append(_item(f"Lowest success rate: {worst.segment} at {pct(worst.success_rate)} "
                           f"({int(worst.successes):,}/{int(worst.total):,}).", evidence_tag(intent, int(worst.total))))
        facts.append(_item(f"Highest success rate: {best.segment} at {pct(best.success_rate)} "
                           f"({int(best.successes):,}/{int(best.total):,}).", evidence_tag(intent, int(best.total))))
        most_fail = table.sort_values("failures", ascending=False).iloc[0]
        facts.append(_item(f"Most failed transactions: {most_fail.segment} with {int(most_fail.failures):,} failures.",
                           evidence_tag(intent, int(most_fail.total))))
        if worst.segment.startswith("Unknown") or worst.segment in ("Other", "Invalid"):
            cav.append(_item(f"'{worst.segment}' is a data-quality bucket, not a real segment.", "Data quality checks"))
        recs.append(_item(f"Drill into {worst.segment}: compare its failure reasons and trend against peers.",
                          "Based on lowest-rate segment"))
    cav += _common_caveats(prep_notes, dq_summary, low_conf)
    return {"headline": headline, "facts": facts, "hypotheses": [], "recommendations": recs, "caveats": cav,
            "next_questions": [f"Why did success rate drop from Sep to Oct by {label.lower()}?"]}


def build_lookup(intent, stats, metric, value_total, prep_notes, dq_summary) -> dict:
    window = intent["period_a"]["label"] if intent.get("period_a") else "May–Oct 2026"
    f = intent.get("filters") or {}
    scope = ", ".join(f"{v}" for v in f.values()) or "all transactions"
    if metric == "txn_count":
        headline = f"{stats['total']:,} unique transactions ({scope}, {window})."
    elif metric == "txn_value":
        headline = f"Total value ₹{value_total:,.2f} across {stats['total']:,} transactions ({scope}, {window})."
    elif metric == "failure_rate":
        headline = f"Failure rate {pct(1 - stats['success_rate'] if stats['success_rate'] is not None else None)} ({scope}, {window})."
    else:
        headline = f"Success rate {pct(stats['success_rate'], 2)} ({scope}, {window})."
    facts = [_item(f"{stats['successes']:,} of {stats['total']:,} transactions succeeded = {pct(stats['success_rate'], 2)} "
                   f"(95% CI {pct(stats['ci_low'])}–{pct(stats['ci_high'])}).", evidence_tag(intent, stats["total"]))]
    return {"headline": headline, "facts": facts, "hypotheses": [], "recommendations": [],
            "caveats": _common_caveats(prep_notes, dq_summary, []), "next_questions": []}


def build_trend(intent, tr, prep_notes, dq_summary) -> dict:
    facts = []
    if len(tr):
        lo = tr.loc[tr.success_rate.idxmin()]
        hi = tr.loc[tr.success_rate.idxmax()]
        facts.append(_item(f"Lowest month: {lo.month} at {pct(lo.success_rate)} ({int(lo.successes):,}/{int(lo.total):,}).",
                           evidence_tag(intent, int(lo.total))))
        facts.append(_item(f"Highest month: {hi.month} at {pct(hi.success_rate)} ({int(hi.successes):,}/{int(hi.total):,}).",
                           evidence_tag(intent, int(hi.total))))
        first, last = tr.iloc[0], tr.iloc[-1]
        facts.append(_item(f"{first.month} → {last.month}: {pct(first.success_rate)} → {pct(last.success_rate)} "
                           f"({pt(last.success_rate - first.success_rate)}).", evidence_tag(intent, int(tr.total.sum()))))
    return {"headline": "Monthly success-rate trend.", "facts": facts, "hypotheses": [],
            "recommendations": [_item("Ask why the lowest month dropped to get a driver decomposition.", "Trend")],
            "caveats": _common_caveats(prep_notes, dq_summary, []),
            "next_questions": ["Why did success rate fall from Sep to Oct?"]}


def build_reconciliation(intent, r) -> dict:
    window = intent["period_a"]["label"]
    facts = [
        _item(f"Gateway (transactions) reports {r['gateway_success']:,} successful payments = "
              f"{pct(r['gateway_success_rate'], 2)} of {r['total']:,}.", evidence_tag(intent, r["total"], "source: transactions")),
        _item(f"Settlement system confirms {r['settled_records']:,} settled = {pct(r['settlement_success_rate'], 2)} "
              f"- the two sources disagree by {r['gap']:,} transactions ({r['gap_pt']:.2f}pt).",
              evidence_tag(intent, r["total"], "source: settlements")),
        _item(f"Breakdown: {r['success_no_settlement']} successes have no settlement record, {r['success_pending']} are "
              f"still PENDING, and {r['failed_but_settled']} FAILED transactions were nonetheless settled.",
              evidence_tag(intent, None, "row-level join on transaction_id")),
    ]
    hyp = [_item("Plausible: settlement lag (PENDING) and missing settlement-file rows explain most of the gap; "
                 "settled-but-failed rows suggest late authorisations not reflected in gateway status.",
                 "Reconciliation breakdown")]
    recs = [_item("Treat the gateway status as the source of truth for authorisation success rate, and settlements "
                  "for money movement; raise the unsettled and settled-but-failed IDs with the settlement team.",
                  "Source-of-truth policy"),
            _item("Re-run this reconciliation after T+2 to see if PENDING items clear.", "Settlement timing")]
    cav = [_item("Success rates differ by source; any number quoted must name its source.", "Two-source disagreement")]
    return {"headline": f"Sources disagree for {window}: gateway success {pct(r['gateway_success_rate'], 2)} vs "
                        f"settled {pct(r['settlement_success_rate'], 2)} ({r['gap']:,} transactions).",
            "facts": facts, "hypotheses": hyp, "recommendations": recs, "caveats": cav, "next_questions": []}


def _common_caveats(prep_notes, dq_summary, low_conf) -> List[dict]:
    cav = [_item(n, "Data-quality cleaning step") for n in prep_notes]
    if dq_summary and dq_summary.get("affected_pct", 0) > 0:
        cav.append(_item(f"Data quality: {dq_summary['affected_rows']:,} of {dq_summary['rows_analyzed']:,} rows "
                         f"({dq_summary['affected_pct']:.2%}) had at least one issue; impact on headline rates is small "
                         f"but segment-level figures for 'Unknown'/'Other' buckets should not be relied on.",
                         "Data quality panel"))
    if low_conf:
        cav.append(_item(f"Low sample size (<30 transactions) for: {', '.join(low_conf[:6])} - treat those rates as indicative only.",
                         "Wilson 95% CI"))
    cav.append(_item("Synthetic dataset - figures illustrate the method, not real Pine Labs performance.", "README"))
    return cav


# --------------------------------------------------------------------------- LLM summary (grounded)
NUM_RE = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str):
    out = set()
    for m in NUM_RE.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def grounded(summary: str, narrative: dict) -> bool:
    """True if every number in `summary` appears in the computed narrative (or is a tiny ordinal / year)."""
    allowed = set()
    blob = narrative["headline"] + " " + " ".join(i["text"] for k in ("facts", "hypotheses", "recommendations", "caveats")
                                                  for i in narrative[k])
    for n in _numbers(blob):
        allowed |= {n, abs(n), round(n, 1), round(abs(n), 1), round(n), round(abs(n))}
    for n in _numbers(summary):
        if n in allowed or round(n, 1) in allowed or (abs(n) <= 3 and float(n).is_integer()) or 2000 <= n <= 2100:
            continue
        return False
    return True


def llm_summary(narrative: dict, provider) -> (Optional[str], str):
    """Ask the LLM for a 3-sentence executive summary using ONLY the computed facts."""
    if provider.name == "mock":
        return None, "template"
    facts = {"headline": narrative["headline"], "facts": [f["text"] for f in narrative["facts"]],
             "hypotheses": [h["text"] for h in narrative["hypotheses"]],
             "recommendations": [r["text"] for r in narrative["recommendations"]]}
    msgs = [{"role": "system", "content": (
        "You write executive summaries for payments leaders. Use ONLY the numbers and claims in the JSON you are given. "
        "Do not add any new number, segment, cause or entity. Label hypotheses as 'likely'/'plausible'. "
        "Write at most 3 sentences of plain text, no markdown.")},
            {"role": "user", "content": json.dumps(facts)}]
    try:
        text = (provider.complete(msgs) or "").strip()
    except Exception as e:
        return None, f"LLM summary unavailable ({str(e)[:80]})"
    if not text:
        return None, "template"
    if not grounded(text, narrative):
        return None, "LLM summary rejected by grounding check (contained a number not in computed evidence)"
    return text, provider.name
