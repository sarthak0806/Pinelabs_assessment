"""Custom HTML/CSS components for a product-grade Streamlit UI."""
from __future__ import annotations

import html
from pathlib import Path

import streamlit as st

CSS_PATH = Path(__file__).with_name("styles.css")


def inject_css():
    st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def esc(s) -> str:
    return html.escape(str(s))


def header(role_label: str, llm_label: str, llm_ok: bool):
    st.markdown(f"""
    <div class="pi-header">
      <div class="pi-brand">
        <div class="pi-logo">◆</div>
        <div><div class="pi-title">PayInsight Analytics</div>
             <div class="pi-sub">AI business analyst for payment performance · evidence-first answers</div></div>
      </div>
      <div class="pi-badges">
        <span class="pi-badge pi-badge-warn">Synthetic data · not real Pine Labs data</span>
        <span class="pi-badge">👤 {esc(role_label)}</span>
        <span class="pi-badge {'pi-badge-ok' if llm_ok else 'pi-badge-muted'}">🧠 {esc(llm_label)}</span>
      </div>
    </div>""", unsafe_allow_html=True)


def kpi(label: str, value: str, delta: str = "", good: bool = True, sub: str = ""):
    cls = "pi-up" if good else "pi-down"
    delta_html = f'<span class="pi-delta {cls}">{esc(delta)}</span>' if delta else ""
    return f"""<div class="pi-kpi"><div class="pi-kpi-label">{esc(label)}</div>
      <div class="pi-kpi-value">{esc(value)}</div><div class="pi-kpi-foot">{delta_html}<span class="pi-kpi-sub">{esc(sub)}</span></div></div>"""


def kpi_row(cards):
    st.markdown('<div class="pi-kpi-row">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def headline(text: str, summary: str = None, source: str = "template"):
    src = (f"Summary written by {source.title()} · passed grounding check" if summary and source != "template"
           else "Summary generated deterministically from computed results")
    summ = f'<div class="pi-summary">{esc(summary)}</div>' if summary else ""
    st.markdown(f"""<div class="pi-headline"><div class="pi-eyebrow">Answer</div>
        <div class="pi-headline-text">{esc(text)}</div>{summ}<div class="pi-src">{esc(src)}</div></div>""",
                unsafe_allow_html=True)


KIND = {
    "facts": ("Observed facts", "pi-fact", "●", "From executed queries"),
    "hypotheses": ("Hypotheses", "pi-hyp", "◆", "Plausible drivers - need confirmation"),
    "recommendations": ("Recommendations", "pi-rec", "➜", "Suggested next actions"),
    "caveats": ("Caveats & data quality", "pi-cav", "!", "Limits on confidence"),
}


def section(kind: str, items):
    if not items:
        return
    title, cls, icon, sub = KIND[kind]
    rows = "".join(f"""<div class="pi-item"><span class="pi-dot">{icon}</span><div>
        <div class="pi-item-text">{esc(i['text'])}</div>
        <div class="pi-evidence">{esc(i['evidence'])}</div></div></div>""" for i in items)
    st.markdown(f"""<div class="pi-card {cls}"><div class="pi-card-head"><span>{title}</span>
        <span class="pi-card-sub">{sub}</span></div>{rows}</div>""", unsafe_allow_html=True)


def steps_tracker(steps):
    icon = {"ok": "✓", "warn": "!", "fail": "✕"}
    rows = "".join(f"""<div class="pi-step pi-step-{s['status']}"><span class="pi-step-ic">{icon.get(s['status'], '•')}</span>
        <div><div class="pi-step-name">{esc(s['name'])}</div><div class="pi-step-det">{esc(s['detail'])[:90]}</div></div>
        <span class="pi-step-ms">{s['ms']:.0f}ms</span></div>""" for s in steps)
    st.markdown(f'<div class="pi-steps">{rows}</div>', unsafe_allow_html=True)


def alert(kind: str, title: str, body: str):
    st.markdown(f"""<div class="pi-alert pi-alert-{kind}"><div class="pi-alert-title">{esc(title)}</div>
        <div class="pi-alert-body">{esc(body)}</div></div>""", unsafe_allow_html=True)


def flow_stepper(steps):
    """Horizontal step chips: 1 Understand → 2 Ambiguity → ... with status colour and timing."""
    parts = []
    for i, s in enumerate(steps, 1):
        parts.append(f'<div class="pi-fs pi-fs-{s["status"]}"><span class="pi-fs-n">{i}</span>'
                     f'<span class="pi-fs-name">{esc(s["name"])}</span><span class="pi-fs-ms">{s["ms"]:.0f}ms</span></div>')
    st.markdown('<div class="pi-flow">' + '<span class="pi-fs-arrow">→</span>'.join(parts) + "</div>",
                unsafe_allow_html=True)


def llm_status(label: str, ok: bool, note: str):
    dot = "pi-led-on" if ok else "pi-led-off"
    st.markdown(f"""<div class="pi-llm"><span class="pi-led {dot}"></span><div>
        <div class="pi-llm-name">{esc(label)}</div><div class="pi-llm-note">{esc(note)}</div></div></div>""",
                unsafe_allow_html=True)


def section_title(title: str, sub: str = ""):
    st.markdown(f'<div class="pi-sec"><span class="pi-sec-title">{esc(title)}</span>'
                f'<span class="pi-sec-sub">{esc(sub)}</span></div>', unsafe_allow_html=True)
