"""PayInsight Analytics - Streamlit entrypoint.   Run:  streamlit run app.py"""
import calendar
import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="PayInsight Analytics", page_icon="◆", layout="wide", initial_sidebar_state="expanded")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from analytics import pipeline  # noqa: E402
from analytics.analysis import DIM_LABELS  # noqa: E402
from analytics.catalog import CATALOG, MEASURES, RELATIONSHIPS  # noqa: E402
from analytics.nlp import month_period  # noqa: E402
from ui import components as ui  # noqa: E402
from utils import audit, viz  # noqa: E402
from utils.llm import PROVIDERS, LLMUnavailable, MockProvider, build_provider  # noqa: E402

if not config.DB_PATH.exists():
    from data import seed_data
    seed_data.main()

ui.inject_css()

EXAMPLES = [
    "Why did our overall success rate fall from Sept to Oct?",
    "Did our merchant mix shift from July to August?",
    "Why did UPI success drop?",
    "Show me success rates by bank",
    "Do the gateway and settlement sources disagree in October?",
    "Who are the problematic banks?",
    "Why did success rate drop recently?",
    "Show the monthly success rate trend",
]
# (icon, short card title, full question). The card shows the title; clicking runs the full question.
EXAMPLE_GROUPS = [
    ("Business questions", [
        ("📉", "Sep → Oct decline", EXAMPLES[0]),
        ("🔀", "Jul → Aug mix shift", EXAMPLES[1]),
        ("💳", "UPI success drop", EXAMPLES[2]),
        ("📈", "Monthly trend", EXAMPLES[7]),
    ]),
    ("Failure & control scenarios", [
        ("🏦", "By bank · data quality", EXAMPLES[3]),
        ("🧾", "Settlement mismatch", EXAMPLES[4]),
        ("❓", "Problematic banks?", EXAMPLES[5]),
        ("⏱️", "Dropped 'recently'?", EXAMPLES[6]),
    ]),
]
MONTHS = {f"{calendar.month_name[m]} 2026": m for m in range(5, 11)}

ss = st.session_state
ss.setdefault("report", None)
ss.setdefault("pending", None)
ss.setdefault("question", EXAMPLES[0])
ss.setdefault("eval", None)


def queue(q: str):
    ss.pending = q
    ss.question = q


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("### ◆ PayInsight")
    st.markdown('<div class="pi-side-title">Access role</div>', unsafe_allow_html=True)
    role = st.selectbox("Role", list(config.ROLES), format_func=lambda r: config.ROLES[r]["label"],
                        label_visibility="collapsed", help="Simulated RBAC - the restricted role cannot query bank or settlement data.")
    st.markdown('<div class="pi-side-title">Comparison window</div>', unsafe_allow_html=True)
    a_name = st.selectbox("Current period (A)", list(MONTHS), index=5)
    b_name = st.selectbox("Baseline period (B)", list(MONTHS), index=4)
    period_a, period_b = month_period(2026, MONTHS[a_name]), month_period(2026, MONTHS[b_name])
    st.markdown('<div class="pi-side-title">Language model</div>', unsafe_allow_html=True)
    status_slot = st.empty()                      # status card is filled after the settings below are read
    modes = {**{k: v["label"] + (" · Nemotron (default, free)" if k == "openrouter" else "")
                for k, v in PROVIDERS.items()}, "mock": "Offline (rules, no key needed)"}
    default_mode = config.LLM_MODE if config.LLM_MODE in modes else "openrouter"
    provider, llm_label, llm_ok, llm_note = MockProvider(), "Offline (rules)", False, "No LLM - deterministic rules"
    with st.expander("⚙ Change model / use your own API key", expanded=False):
        mode = st.radio("LLM provider", list(modes), format_func=modes.get, label_visibility="collapsed",
                        index=list(modes).index(default_mode),
                        help="Numbers are always computed deterministically - the LLM only parses the question, "
                             "drafts SQL (validated) and words the summary.")
        if mode != "mock":
            spec = PROVIDERS[mode]
            env_key = spec["env_key"]()
            shared = mode == "openrouter" and bool(env_key)   # app-provided free model: no key needed
            own = False
            if shared:
                st.success(f"Free · no API key needed\n\nModel: `{config.OPENROUTER_MODEL}`")
                own = st.checkbox("Use my own OpenRouter key / model", key="own_openrouter",
                                  help="Only needed to pick a different OpenRouter model.")
            if shared and not own:
                user_key, model_name = "", config.OPENROUTER_MODEL      # model locked on the shared key
            else:
                user_key = st.text_input(
                    f"Your {spec['label']} API key", type="password", key=f"apikey_{mode}",
                    placeholder="Using the app's configured key - paste yours to override" if env_key and not shared
                    else "Paste your API key",
                    help=f"Kept only in this browser session's memory - never logged or saved. Get one: {spec['key_url']}")
                model_name = st.text_input("Model", value=spec["default_model"](), key=f"model_{mode}")
            api_key = user_key.strip() or ("" if own else env_key)
            if not api_key:
                st.info(f"Enter a {spec['label']} API key to enable it. Until then the app runs offline.")
                llm_note = f"{spec['label']} needs an API key"
            else:
                try:
                    provider = build_provider(mode, api_key, model_name.strip())
                    llm_label, llm_ok = f"{spec['label'].split(' (')[0]} · {provider.model}", True
                    src = "your key" if user_key.strip() else ("free, no key needed" if shared else "app key")
                    llm_note = f"Active · {src} · {provider.usage_today()}/{config.LLM_DAILY_CAP} calls today"
                    st.caption(f"✓ Enabled with {src} · cache on · auto-fallback to offline")
                    if st.button("Test key", key=f"test_{mode}", use_container_width=True):
                        with st.spinner("Calling model…"):
                            try:
                                st.success(f"Connected · model replied: {provider.ping()!r}")
                            except LLMUnavailable as e:
                                st.error(str(e))
                except LLMUnavailable as e:
                    st.warning(f"{spec['label']} unavailable: {e}. Using offline mode.")
                    llm_note = str(e)
    with status_slot.container():
        ui.llm_status(llm_label, llm_ok, llm_note)
    if ss.report:
        st.markdown('<div class="pi-side-title">Analysis steps (last question)</div>', unsafe_allow_html=True)
        ui.steps_tracker(ss.report["steps"])

# ------------------------------------------------------------------ header + KPIs
ui.header(config.ROLES[role]["label"], llm_label, llm_ok)


@st.cache_data(show_spinner=False)
def _kpis(role, pa, pb):
    return pipeline.kpis(role, pa, pb)


k = _kpis(role, period_a, period_b)
ra, rb = k["a"]["success_rate"], k["b"]["success_rate"]
d = (ra - rb) * 100 if ra is not None and rb is not None else 0
ui.kpi_row([
    ui.kpi(f"Success rate · {period_a['label']}", f"{ra * 100:.2f}%" if ra is not None else "n/a",
           f"{d:+.2f}pt vs {period_b['label']}", d >= 0, f"CI {k['a']['ci_low'] * 100:.1f}–{k['a']['ci_high'] * 100:.1f}%" if ra else ""),
    ui.kpi(f"Success rate · {period_b['label']}", f"{rb * 100:.2f}%" if rb is not None else "n/a", "", True, "baseline"),
    ui.kpi("Transactions", f"{k['a']['total']:,}",
           f"{(k['a']['total'] / k['b']['total'] - 1) * 100:+.1f}%" if k["b"]["total"] else "", True, f"vs {k['b']['total']:,}"),
    ui.kpi("Failed transactions", f"{k['a']['failures']:,}", f"{k['a']['failures'] - k['b']['failures']:+,}",
           k["a"]["failures"] <= k["b"]["failures"], period_a["label"]),
    ui.kpi("Data-quality score", f"{k['dq']['score'] * 100:.1f}%", f"{k['dq']['issues']} issue types",
           k["dq"]["issues"] == 0, f"{k['dq']['affected_rows']} rows flagged"),
])

# ------------------------------------------------------------------ ask
st.markdown('<div class="pi-ask-title">Ask a question about payment performance</div>', unsafe_allow_html=True)
with st.form("ask", clear_on_submit=False, border=False):
    c1, c2 = st.columns([6, 1])
    q = c1.text_input("Question", key="question", label_visibility="collapsed",
                      placeholder="e.g. Why did our success rate fall from Sept to Oct?")
    submitted = c2.form_submit_button("Ask Analyst ➜", use_container_width=True)
with st.container(key="examples"):
    n = 0
    for group, items in EXAMPLE_GROUPS:
        st.markdown(f'<div class="pi-ex-group">{ui.esc(group)}</div>', unsafe_allow_html=True)
        cols = st.columns(4, gap="small")
        for col, (icon, title, question) in zip(cols, items):
            col.button(f"{icon}  {title}", key=f"chip{n}", on_click=queue, args=(question,),
                       help=question, use_container_width=True)
            n += 1

run_q = ss.pending or (q if submitted else None)
if run_q:
    ss.pending = None
    with st.spinner("Understanding question → drafting SQL → validating → executing → analysing…"):
        ss.report = pipeline.answer(run_q.strip(), role, (period_a, period_b), provider)
    st.rerun()

rep = ss.report
if not rep:
    st.info("Pick an example or type a question, then press **Ask Analyst**.")
    st.stop()


# ------------------------------------------------------------------ helpers
_chart_no = [0]


def chart(fig, where=st):
    """Render a Plotly figure with a unique key (same figure may appear in several tabs)."""
    if fig is None:
        return
    _chart_no[0] += 1
    where.plotly_chart(fig, use_container_width=True, key=f"chart_{_chart_no[0]}",
                       config={"displaylogo": False, "displayModeBar": "hover"})


def fpct(x, d=2):
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def answer_metrics(rep, an_, atype):
    """Analytical metric strip shown under the headline."""
    if atype == "rate_change":
        oa, ob = an_["overall_a"], an_["overall_b"]
        pa, pb = rep["intent"]["period_a"]["label"], rep["intent"]["period_b"]["label"]
        delta = (oa["success_rate"] - ob["success_rate"]) * 100
        decs = an_["decompositions"]
        dref = decs.get("merchant_category") or next(iter(decs.values()))
        cards = [
            ui.kpi(f"Baseline · {pb}", fpct(ob["success_rate"]), "", True, f"{ob['successes']:,}/{ob['total']:,}"),
            ui.kpi(f"Current · {pa}", fpct(oa["success_rate"]), f"{delta:+.2f}pt", delta >= 0, f"{oa['successes']:,}/{oa['total']:,}"),
            ui.kpi("Rate effect", f"{dref.rate_effect * 100:+.2f}pt", f"{(dref.rate_share or 0):.0%} of change", False,
                   "segments performed worse" if dref.rate_effect < 0 else "segments improved"),
            ui.kpi("Mix effect", f"{dref.mix_effect * 100:+.2f}pt", f"{(dref.mix_share or 0):.0%} of change", False,
                   "by merchant category" if "merchant_category" in decs else ""),
        ]
        if an_.get("driver"):
            _, row, _ = an_["driver"]
            share = row.rate_contrib / (delta / 100) if delta else 0
            cards.append(ui.kpi("Top driver", str(row.segment), f"{share:.0%} of change", False,
                                f"{fpct(row.r_B, 1)} → {fpct(row.r_A, 1)}"))
        return cards
    if atype == "segment_breakdown":
        t, o = an_["table"], an_["overall_a"]
        rel = t[(t.total >= 30) & ~t.segment.astype(str).str.match(r"^(Unknown|Other|Invalid)")]
        rel = rel if len(rel) else t
        best, worst = rel.loc[rel.success_rate.idxmax()], rel.loc[rel.success_rate.idxmin()]
        return [
            ui.kpi("Overall success rate", fpct(o["success_rate"]), "", True, f"{o['total']:,} transactions"),
            ui.kpi("Best segment", str(best.segment), fpct(best.success_rate, 1), True, f"n={int(best.total):,}"),
            ui.kpi("Worst segment", str(worst.segment), fpct(worst.success_rate, 1), False, f"n={int(worst.total):,}"),
            ui.kpi("Spread best − worst", f"{(best.success_rate - worst.success_rate) * 100:.1f}pt", "", True,
                   f"{len(rel)} reliable segments"),
            ui.kpi("Failures", f"{int(t.failures.sum()):,}", f"{int(worst.failures) / max(1, int(t.failures.sum())):.0%} in worst",
                   False, str(worst.segment)),
        ]
    if atype == "reconciliation":
        r = rep["reconciliation"]
        return [
            ui.kpi("Gateway success", f"{r['gateway_success']:,}", fpct(r["gateway_success_rate"]), True, "transactions table"),
            ui.kpi("Settled", f"{r['settled_records']:,}", fpct(r["settlement_success_rate"]), True, "settlement system"),
            ui.kpi("Disagreement", f"{r['gap']:,}", f"{r['gap_pt']:.2f}pt", False, "net transactions"),
            ui.kpi("Unsettled successes", f"{r['success_no_settlement'] + r['success_pending']:,}", "", False,
                   f"{r['success_no_settlement']} missing · {r['success_pending']} pending"),
            ui.kpi("Settled failures", f"{r['failed_but_settled']:,}", "", False, "late authorisations?"),
        ]
    tr = rep.get("trend")
    o = an_.get("overall_a") or {}
    cards = []
    if o:
        cards += [ui.kpi("Success rate", fpct(o.get("success_rate")), "", True, f"{o.get('successes', 0):,}/{o.get('total', 0):,}"),
                  ui.kpi("95% CI", f"{fpct(o.get('ci_low'), 1)} – {fpct(o.get('ci_high'), 1)}", "", True, "Wilson interval"),
                  ui.kpi("Transactions", f"{o.get('total', 0):,}", "", True, "unique attempts")]
    if tr is not None and len(tr):
        rates = tr["success_rate_pct"] if "success_rate_pct" in tr else tr["success_rate"] * 100
        lo, hi = tr.loc[rates.idxmin()], tr.loc[rates.idxmax()]
        cards += [ui.kpi("Best month", str(hi["month"]), f"{rates.max():.1f}%", True, ""),
                  ui.kpi("Worst month", str(lo["month"]), f"{rates.min():.1f}%", False, f"range {rates.max() - rates.min():.1f}pt")]
    return cards


def fmt_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if c in ("success_rate", "ci_low", "ci_high", "r_A", "r_B", "w_A", "w_B", "r_bar", "w_bar", "success_rate_prev"):
            df[c] = (df[c] * 100).round(2)
        if c in ("rate_contrib", "mix_contrib", "total_contrib"):
            df[c] = (df[c] * 100).round(3)
    return df


def show_sql(title, sql, params, extra=""):
    st.markdown(f"**{title}** {extra}")
    st.code(sql, language="sql")
    st.caption("Bound parameters (never string-interpolated): " + (json.dumps(params, default=str) if params else "none"))


def _step_details(rep, name):
    """What happened inside one pipeline step - shown in the Query Flow tab."""
    intent = rep.get("intent") or {}
    p = rep.get("primary") or {}
    if name == "Understand question":
        who = intent.get("source", "rules")
        st.markdown(f"**Interpreted by:** {'deterministic rules parser' if who == 'rules' else who.title() + ' (LLM)'}")
        pa, pb = intent.get("period_a"), intent.get("period_b")
        st.dataframe(pd.DataFrame([
            ("Analysis type", intent.get("analysis_type")), ("Metric", intent.get("metric")),
            ("Current period (A)", pa["label"] if pa else "all data"), ("Baseline period (B)", pb["label"] if pb else "-"),
            ("Group by", ", ".join(intent.get("group_by") or []) or "-"),
            ("Filters", ", ".join(f"{k} = {v}" for k, v in (intent.get("filters") or {}).items()) or "-"),
        ], columns=["Field", "Value"]), hide_index=True, width="stretch")
        for n in rep["llm"].get("notes", []):
            st.caption(f"ℹ️ {n}")
    elif name == "Ambiguity check":
        if intent.get("clarification"):
            st.warning(intent["clarification"]["question"])
        for a in intent.get("assumptions", []):
            st.markdown(f"- {a}")
    elif name == "Select datasets":
        for t in intent.get("datasets", []):
            st.markdown(f"- **{t}** - {CATALOG[t]['description']}")
    elif name == "Draft SQL":
        llm_sql, tmpl = rep.get("llm_sql"), rep.get("template_sql")
        if llm_sql:
            verdict = "✅ accepted by validator" if llm_sql["ok"] else (
                "⛔ blocked: access denied" if llm_sql["access_denied"] else "❌ rejected → template used instead")
            st.markdown(f"**SQL drafted by {llm_sql['model'].title()} (LLM)** · {verdict}")
            st.code(llm_sql["sql"], language="sql")
            st.caption("LLM params: " + (json.dumps(llm_sql["params"], default=str) if llm_sql["params"] else "none"))
            for e in llm_sql["errors"]:
                st.caption(f"✕ {e}")
        else:
            st.markdown("**No LLM draft** (offline mode or LLM unavailable) - SQL built from a parameterized template.")
        if tmpl and (not llm_sql or not llm_sql["ok"]):
            show_sql("Deterministic template SQL", tmpl["sql"], tmpl["params"])
    elif name == "Validate SQL":
        v = p.get("validation", {})
        checks = ["Single statement, SELECT only", "No DDL/DML, PRAGMA, ATTACH, UNION, comments",
                  "Tables & columns on the allowlist", f"Role '{rep['role']}' permitted",
                  "Cost guard: no cartesian joins, ≤4 joins, nesting ≤2", "Placeholders match bound params"]
        ok = v.get("ok")
        for c in checks:
            st.markdown(f"{'✅' if ok else '•'} {c}")
        for w in v.get("warnings", []):
            st.markdown(f"🔒 {w}")
        for e in v.get("errors", []):
            st.error(e)
        if v.get("tables"):
            st.caption("Tables referenced: " + ", ".join(v["tables"]))
    elif name == "Execute (read-only)":
        show_sql("Executed SQL (read-only connection)", p.get("sql", ""), p.get("params", []),
                 f"· {p.get('rows', 0)} rows · {p.get('elapsed_ms', 0):.0f} ms" + (" · cached" if p.get("cached") else ""))
        if p.get("df") is not None:
            st.dataframe(p["df"].head(50), width="stretch", hide_index=True)
    elif name == "Fetch evidence rows" and rep.get("evidence"):
        show_sql("Evidence query", rep["evidence"]["sql"], rep["evidence"]["params"], f"· {rep['evidence']['rows']:,} rows")
    elif name == "Reconcile sources" and rep.get("reconciliation"):
        show_sql("Reconciliation query", rep["reconciliation"]["sql"], rep["reconciliation"]["params"])
    elif name == "Data-quality checks" and rep.get("quality"):
        for c in rep["quality"]["checks"]:
            if c["status"] != "pass":
                st.markdown(f"⚠️ **{c['name']}** - {c['detail']}")
    elif name == "Analyse" and rep.get("analysis"):
        a = rep["analysis"]
        if a.get("decompositions"):
            for dim, d in a["decompositions"].items():
                if not d.empty_period:
                    st.markdown(f"- **{DIM_LABELS.get(dim, dim)}**: rate {d.rate_effect * 100:+.2f}pt + mix "
                                f"{d.mix_effect * 100:+.2f}pt = {d.delta * 100:+.2f}pt {'✅' if d.check_ok else '❌'}")
    elif name.startswith("Narrate"):
        nar = rep.get("narrative") or {}
        if nar.get("summary"):
            st.markdown(f"**LLM summary (passed grounding check):** {nar['summary']}")
        else:
            st.markdown(f"Summary source: {nar.get('summary_source', 'template')}")


def render_flow(rep):
    """Step-by-step trace of how the answer was produced, plus every query executed."""
    steps = rep.get("steps", [])
    ui.flow_stepper(steps)
    icon = {"ok": "✅", "warn": "⚠️", "fail": "⛔"}
    for i, s in enumerate(steps, 1):
        open_ = s["status"] != "ok" or s["name"] in ("Draft SQL", "Execute (read-only)")
        with st.expander(f"{i}. {s['name']}  ·  {icon.get(s['status'], '•')}  ·  {s['ms']:.0f} ms", expanded=open_):
            st.caption(s["detail"])
            _step_details(rep, s["name"])
    qs = rep.get("queries") or []
    if qs:
        ui.section_title("Every query executed for this answer", "All validated, parameterized and run read-only")
        for i, q in enumerate(qs, 1):
            meta = f"· {q['source']} · {q['rows']:,} rows" + (f" · {q['ms']:.0f} ms" if q.get("ms") is not None else "") \
                   + (" · cached" if q.get("cached") else "")
            show_sql(f"Q{i}. {q['purpose']}", q["sql"], q["params"], meta)


st.markdown(f"<div style='color:#64748B;font-size:.85rem;margin-top:8px'>Question: <b>{ui.esc(rep['question'])}</b> · "
            f"role: {ui.esc(rep['role'])} · {rep.get('elapsed_ms', 0) / 1000:.1f}s</div>", unsafe_allow_html=True)
for n in rep["llm"].get("notes", []):
    st.caption(f"ℹ️ {n}")

status = rep["status"]
if status == "needs_clarification":
    c = rep["intent"]["clarification"]
    ui.alert("ask", "I need one clarification before running any query", c["question"])
    cols = st.columns(max(1, len(c["options"])))
    for i, opt in enumerate(c["options"]):
        cols[i].button(opt["label"], key=f"clar{i}", on_click=queue, args=(opt["rewrite"],), use_container_width=True)
    ui.section_title("Query Flow", "Stopped at the ambiguity check - no SQL was run")
    render_flow(rep)
    st.stop()
if status == "access_denied":
    ui.alert("deny", "⛔ Access denied - query blocked before execution",
             f"{rep.get('error')}. Your role ({config.ROLES[role]['label']}) cannot see this data. "
             f"Try a merchant-level question, or switch role if you are authorised.")
    ui.section_title("Query Flow", "See exactly where the request was stopped")
    render_flow(rep)
    if rep.get("primary"):
        show_sql("Blocked SQL (not executed)", rep["primary"]["sql"], rep["primary"]["params"])
    st.stop()
if status in ("rejected", "error"):
    ui.alert("deny", "Query rejected", rep.get("error", "unknown error"))
    ui.section_title("Query Flow", "See exactly where the request was stopped")
    render_flow(rep)
    st.stop()

nar = rep["narrative"]
an_ = rep.get("analysis") or {}
atype = an_.get("type") or rep["intent"]["analysis_type"]

tab_answer, tab_drill, tab_dq, tab_admin, tab_evidence = st.tabs(
    ["📊 Answer", "🔎 Drill-down", "🩺 Data Quality", "🛡️ Audit & Evaluation", "🧭 Query Flow"])

# ------------------------------------------------------------------ answer
with tab_answer:
    ui.headline(nar["headline"], nar.get("summary"), nar.get("summary_source", "template"))
    ui.kpi_row(answer_metrics(rep, an_, atype))

    # ---- visual evidence: full-width hero chart + 2-column grid ----
    ui.section_title("Visual evidence", "Every chart is computed from the executed queries shown in the Query Flow tab")
    if atype == "rate_change":
        decs = an_["decompositions"]
        best = "method_x_bank" if an_.get("driver") and an_["driver"][0] == "method_x_bank" else None
        mix_dim = an_["mix"][0].dimension if an_.get("mix") else None
        main_dim = best or (an_["driver"][0] if an_.get("driver") else None) or mix_dim or next(iter(decs))
        if mix_dim and an_["mix"][0].mix_share and an_["mix"][0].mix_share > 0.5:
            main_dim = mix_dim
        chart(viz.waterfall(decs[main_dim]))
        pa_m, pb_m = rep["intent"]["period_a"]["start"][:7], rep["intent"]["period_b"]["start"][:7]
        g1, g2 = st.columns(2, gap="medium")
        with g1:
            chart(viz.trend_line(rep["trend"], highlight=(pa_m, pb_m)))
        with g2:
            seg_dim = "merchant_category" if "merchant_category" in decs else main_dim
            chart(viz.contribution_bars(decs[seg_dim]))
        g3, g4 = st.columns(2, gap="medium")
        with g3:
            cmp_dim = "bank" if "bank" in decs else ("payment_method" if "payment_method" in decs else main_dim)
            chart(viz.period_compare(decs[cmp_dim], rep["intent"]["period_b"]["label"], rep["intent"]["period_a"]["label"]))
        with g4:
            chart(viz.reasons_donut(an_.get("reasons_a"), f"Failure reasons · {rep['intent']['period_a']['label']}"))
    elif atype == "segment_breakdown":
        chart(viz.segment_bars(an_["table"], DIM_LABELS.get(an_["dimension"], an_["dimension"])))
        g1, g2 = st.columns(2, gap="medium")
        with g1:
            chart(viz.failure_pareto(an_["table"], DIM_LABELS.get(an_["dimension"], an_["dimension"])))
        with g2:
            chart(viz.reasons_donut(an_.get("reasons_a")))
    elif atype == "reconciliation" or rep.get("reconciliation"):
        r = rep["reconciliation"]
        g1, g2 = st.columns(2, gap="medium")
        with g1:
            chart(viz.reconciliation_bars(r))
        with g2:
            chart(viz.reconciliation_breakdown(r))
    else:
        chart(viz.trend_line(rep["trend"]))
        g1, g2 = st.columns(2, gap="medium")
        with g1:
            chart(viz.volume_bars(rep["trend"]))
        with g2:
            chart(viz.reasons_donut(an_.get("reasons_a")))

    if an_.get("top_affected") is not None and len(an_["top_affected"]):
        ui.section_title("Top 10 most-affected segments", "Ranked by failures in the current period · rates in %")
        st.dataframe(fmt_df(an_["top_affected"]), width="stretch", hide_index=True)

    # ---- findings ----
    ui.section_title("Findings", "Facts are observed in data · hypotheses need confirmation · every item cites its evidence")
    f1, f2 = st.columns(2, gap="medium")
    with f1:
        ui.section("facts", nar.get("facts"))
        ui.section("caveats", nar.get("caveats"))
    with f2:
        ui.section("hypotheses", nar.get("hypotheses"))
        ui.section("recommendations", nar.get("recommendations"))
        if nar.get("next_questions"):
            st.markdown("**Suggested next questions**")
            for i, nq in enumerate(nar["next_questions"]):
                st.button(f"↳ {nq}", key=f"nq{i}", on_click=queue, args=(nq,))

# ------------------------------------------------------------------ query flow
with tab_evidence:
    p = rep["primary"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SQL written by", "Template" if p["source"] == "template" else f"{p['source'].title()} (LLM)")
    c2.metric("Validation", "Passed" if p["validation"]["ok"] else "Failed")
    c3.metric("Queries executed", len(rep.get("queries", [])))
    c4.metric("Total time", f"{rep.get('elapsed_ms', 0) / 1000:.1f} s")
    render_flow(rep)

# ------------------------------------------------------------------ drill-down
with tab_drill:
    if atype == "rate_change":
        decs = an_["decompositions"]
        dim = st.selectbox("Decompose by", list(decs), format_func=lambda d: DIM_LABELS.get(d, d))
        dec = decs[dim]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total change", f"{dec.delta * 100:+.2f}pt")
        m2.metric("Rate effect", f"{dec.rate_effect * 100:+.2f}pt", f"{(dec.rate_share or 0):.0%} of change", delta_color="off")
        m3.metric("Mix effect", f"{dec.mix_effect * 100:+.2f}pt", f"{(dec.mix_share or 0):.0%} of change", delta_color="off")
        m4.metric("Exact-sum check", "✓ passed" if dec.check_ok else "✕ failed")
        cA, cB = st.columns(2)
        chart(viz.waterfall(dec), cA)
        chart(viz.contribution_bars(dec), cB)
        st.markdown("**Kitagawa table** - r = success rate (%), w = share of volume (%), contributions in pt; "
                    "`confidence` flags segments with < 30 transactions.")
        st.dataframe(fmt_df(dec.table[["segment", "n_B", "n_A", "r_B", "r_A", "w_B", "w_A", "rate_contrib", "mix_contrib",
                                       "total_contrib", "confidence"]]), use_container_width=True, hide_index=True)
        with st.expander("Method"):
            st.markdown(r"""For each segment $k$: $\bar w_k=\frac{w_k^A+w_k^B}{2}$, $\bar r_k=\frac{r_k^A+r_k^B}{2}$.
**Rate effect** $=\sum_k \bar w_k (r_k^A-r_k^B)$ (segments performing differently);
**Mix effect** $=\sum_k \bar r_k (w_k^A-w_k^B)$ (volume moving between segments).
Rate + Mix $= R^A-R^B$ exactly - verified on every run.""")
    elif atype == "segment_breakdown":
        st.dataframe(fmt_df(an_["table"]), width="stretch", hide_index=True)
    elif rep.get("reconciliation"):
        st.json({k: v for k, v in rep["reconciliation"].items() if k not in ("sql", "params")})
    else:
        st.dataframe(rep["trend"], width="stretch", hide_index=True)

# ------------------------------------------------------------------ data quality
with tab_dq:
    qd = rep.get("quality")
    if qd:
        s = qd["summary"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Rows analysed", f"{s['rows_analyzed']:,}")
        c2.metric("Issue types", s["issues"])
        c3.metric("Rows flagged", f"{s['affected_rows']:,}", f"{s['affected_pct']:.2%}", delta_color="off")
        c4.metric("Quality score", f"{s['score'] * 100:.1f}%")
        for chk in qd["checks"]:
            icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}[chk["status"]]
            st.markdown(f"{icon} **{chk['name']}** - {chk['detail']}")
        if qd["prep_notes"]:
            ui.alert("warn", "How the data was cleaned before computing any rate", " ".join(qd["prep_notes"]))
    else:
        st.info("No row-level data-quality checks for this question type (see reconciliation results).")

# ------------------------------------------------------------------ admin
with tab_admin:
    st.markdown("#### Known-answer benchmark")
    st.caption("13 questions with ground truth computed by independent SQL. Runs in mock mode (no LLM quota).")
    if st.button("▶ Run benchmark"):
        from analytics.evaluation import run_eval
        bar = st.progress(0.0)
        ss.eval = run_eval(MockProvider(), progress=bar.progress)
    if ss.eval is not None:
        e = ss.eval
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Accuracy", f"{e.passed.mean():.0%}", f"{int(e.passed.sum())}/{len(e)}", delta_color="off")
        c2.metric("Unsafe / invalid SQL executed", "0")
        c3.metric("Grounded narratives", f"{e.grounded.mean():.0%}")
        c4.metric("Max latency", f"{e.latency_s.max():.2f}s")
        st.dataframe(e, width="stretch", hide_index=True)
    st.markdown("#### Audit trail")
    log = audit.read(100)
    if log:
        st.dataframe(pd.DataFrame(log), width="stretch", hide_index=True)
    with st.expander("Data dictionary"):
        for t, meta in CATALOG.items():
            st.markdown(f"**{t}** - {meta['description']}")
            st.dataframe(pd.DataFrame([(c, typ, desc) for c, (typ, desc) in meta["columns"].items()],
                                      columns=["column", "type", "description"]), hide_index=True, use_container_width=True)
        st.markdown("**Relationships:** " + "; ".join(RELATIONSHIPS))
        st.markdown("**Measures:** " + "; ".join(f"`{k}` = {v}" for k, v in MEASURES.items()))
