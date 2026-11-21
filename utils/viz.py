"""Plotly chart builders - one shared colorblind-safe (Okabe-Ito) theme, labelled axes, legends."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from analytics.analysis import DIM_LABELS

INK = "#0F172A"
MUTED = "#64748B"
GRID = "#E2E8F0"
ACCENT = "#4F46E5"
NEG = "#D55E00"      # vermillion
POS = "#009E73"      # bluish green
MIX = "#E69F00"      # orange
BLUE = "#0072B2"
SKY = "#56B4E9"
PALETTE = [BLUE, MIX, POS, NEG, "#CC79A7", SKY, "#F0E442", "#999999"]


def _style(fig, title=None, height=340):
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=INK), x=0, xanchor="left") if title else None,
        height=height, margin=dict(l=10, r=10, t=48 if title else 10, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, Segoe UI, sans-serif", color=INK, size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, font=dict(size=11)),
        hoverlabel=dict(bgcolor="white", font_size=12),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, tickfont=dict(color=MUTED))
    return fig


def trend_line(tr: pd.DataFrame, highlight=(), title="Monthly success rate"):
    if tr is None or not len(tr):
        return None
    tr = tr.copy()
    rate_col = "success_rate" if "success_rate" in tr else "success_rate_pct"
    y = tr[rate_col] * (100 if rate_col == "success_rate" else 1)
    colors = [NEG if m in highlight else BLUE for m in tr["month"]]
    fig = go.Figure(go.Scatter(x=tr["month"], y=y, mode="lines+markers+text", line=dict(color=BLUE, width=3),
                               marker=dict(size=10, color=colors, line=dict(color="white", width=2)),
                               text=[f"{v:.1f}%" for v in y], textposition="top center",
                               hovertemplate="%{x}<br>Success rate %{y:.2f}%<extra></extra>"))
    fig.update_yaxes(title="Success rate (%)", range=[max(0, float(y.min()) - 4), min(100, float(y.max()) + 3)])
    fig.update_xaxes(title=None)
    return _style(fig, title)


def waterfall(dec, top_n=5, title=None):
    """R_B -> top segment rate effects -> other rate effects -> mix effect -> R_A (percentage points)."""
    t = dec.table.copy()
    t = t.reindex(t.rate_contrib.abs().sort_values(ascending=False).index)
    top, rest = t.head(top_n), t.iloc[top_n:]
    x = [f"Baseline<br>{dec.R_B * 100:.1f}%"] + [f"{s}<br>(rate)" for s in top.segment]
    y = [dec.R_B * 100] + list(top.rate_contrib * 100)
    measure = ["absolute"] + ["relative"] * len(top)
    if len(rest):
        x.append("Other segments<br>(rate)"); y.append(float(rest.rate_contrib.sum() * 100)); measure.append("relative")
    x.append("Mix shift<br>(all segments)"); y.append(dec.mix_effect * 100); measure.append("relative")
    x.append(f"Current<br>{dec.R_A * 100:.1f}%"); y.append(dec.R_A * 100); measure.append("total")
    fig = go.Figure(go.Waterfall(
        x=x, y=y, measure=measure, connector=dict(line=dict(color=GRID)),
        decreasing=dict(marker=dict(color=NEG)), increasing=dict(marker=dict(color=POS)),
        totals=dict(marker=dict(color=BLUE)),
        text=[f"{v:.1f}%" if m != "relative" else f"{v:+.2f}pt" for v, m in zip(y, measure)], textposition="outside",
        hovertemplate="%{x}<br>%{y:.2f}<extra></extra>"))
    lo = min(dec.R_A, dec.R_B) * 100
    fig.update_yaxes(title="Success rate (%) / contribution (pt)", range=[max(0, lo - 6), max(dec.R_A, dec.R_B) * 100 + 3])
    return _style(fig, title or f"What moved the success rate · by {DIM_LABELS.get(dec.dimension, dec.dimension).lower()}",
                  height=380)


def contribution_bars(dec, top_n=10, title=None):
    t = dec.table.copy()
    t = t.reindex(t.total_contrib.abs().sort_values(ascending=False).index).head(top_n).iloc[::-1]
    fig = go.Figure()
    fig.add_bar(y=t.segment, x=t.rate_contrib * 100, name="Rate effect", orientation="h", marker_color=BLUE,
                hovertemplate="%{y}<br>Rate effect %{x:+.2f}pt<extra></extra>")
    fig.add_bar(y=t.segment, x=t.mix_contrib * 100, name="Mix effect", orientation="h", marker_color=MIX,
                hovertemplate="%{y}<br>Mix effect %{x:+.2f}pt<extra></extra>")
    fig.update_layout(barmode="relative")
    fig.update_xaxes(title="Contribution to change (percentage points)", zeroline=True, zerolinecolor=MUTED)
    return _style(fig, title or f"Rate vs mix contribution · {DIM_LABELS.get(dec.dimension, dec.dimension)}",
                  height=max(300, 34 * len(t) + 110))


DQ_BUCKET = r"^(Unknown|Other|Invalid)"


def segment_bars(table: pd.DataFrame, dim_label: str, title=None):
    """Horizontal bars with 95% CI whiskers. Rate label sits at the bar START so it never collides with the
    whisker; n is in the axis label. Data-quality buckets / low-n segments are greyed and pushed to the bottom."""
    t = table.copy()
    t["dq"] = t.segment.astype(str).str.match(DQ_BUCKET) | (t.confidence != "ok")
    t = pd.concat([t[t.dq].sort_values("success_rate"), t[~t.dq].sort_values("success_rate")])
    avg = (t.successes.sum() / t.total.sum()) * 100
    labels = [f"{s}  ·  n={n:,}" + ("  ⚠" if dq else "") for s, n, dq in zip(t.segment.astype(str), t.total, t.dq)]
    worst_ok = t[~t.dq].success_rate.min() if (~t.dq).any() else None
    colors = [SKY if dq else (NEG if r == worst_ok else BLUE) for r, dq in zip(t.success_rate, t.dq)]
    fig = go.Figure(go.Bar(
        y=labels, x=t.success_rate * 100, orientation="h", marker_color=colors,
        error_x=dict(type="data", symmetric=False, array=(t.ci_high - t.success_rate) * 100,
                     arrayminus=(t.success_rate - t.ci_low) * 100, color="#334155", thickness=1.3, width=4),
        text=[f"{v * 100:.1f}%" for v in t.success_rate], textposition="inside", insidetextanchor="start",
        textfont=dict(color="white", size=12),
        customdata=list(zip(t.ci_low * 100, t.ci_high * 100, t.failures)),
        hovertemplate="%{y}<br>Success %{x:.2f}%<br>95% CI %{customdata[0]:.1f}–%{customdata[1]:.1f}%"
                      "<br>Failures %{customdata[2]:,}<extra></extra>"))
    fig.add_vline(x=avg, line_dash="dash", line_color=ACCENT, annotation_text=f"overall {avg:.1f}%",
                  annotation_position="top", annotation_font=dict(color=ACCENT, size=11))
    lo = float(t[~t.dq].success_rate.min() if (~t.dq).any() else t.success_rate.min()) * 100
    fig.update_xaxes(title="Success rate (%) · whiskers = 95% CI · ⚠ = low-n or data-quality bucket",
                     range=[max(0, lo - 8), 100.5])
    return _style(fig, title or f"Success rate by {dim_label.lower()}", height=max(320, 34 * len(t) + 110))


def period_compare(dec, label_b: str, label_a: str, top_n: int = 10, title=None):
    """Dumbbell: each segment's success rate in baseline vs current period, sorted by change."""
    t = dec.table[dec.table.min_n >= 10].copy()
    t["chg"] = t.r_A - t.r_B
    t = t.reindex(t.chg.abs().sort_values(ascending=False).index).head(top_n).sort_values("chg", ascending=False)
    fig = go.Figure()
    for _, r in t.iterrows():
        fig.add_trace(go.Scatter(x=[r.r_B * 100, r.r_A * 100], y=[str(r.segment)] * 2, mode="lines",
                                 line=dict(color=NEG if r.chg < 0 else POS, width=4), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=t.r_B * 100, y=t.segment.astype(str), mode="markers", name=label_b,
                             marker=dict(size=12, color=MUTED, line=dict(color="white", width=1.5)),
                             hovertemplate="%{y}<br>" + label_b + " %{x:.1f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=t.r_A * 100, y=t.segment.astype(str), mode="markers+text", name=label_a,
                             marker=dict(size=12, color=BLUE, line=dict(color="white", width=1.5)),
                             text=[f"{c * 100:+.1f}pt" for c in t.chg], textposition="middle left",
                             textfont=dict(size=11, color=INK),
                             hovertemplate="%{y}<br>" + label_a + " %{x:.1f}%<extra></extra>"))
    fig.update_xaxes(title="Success rate (%)")
    return _style(fig, title or f"{label_b} → {label_a} by {DIM_LABELS.get(dec.dimension, dec.dimension).lower()}",
                  height=max(320, 34 * len(t) + 110))


def failure_pareto(table: pd.DataFrame, dim_label: str, title=None):
    """Where do the failures concentrate? Bars = failures, line = cumulative share."""
    t = table.sort_values("failures", ascending=False).head(12).copy()
    t["cum"] = t.failures.cumsum() / table.failures.sum() * 100
    fig = go.Figure()
    fig.add_bar(x=t.segment.astype(str), y=t.failures, name="Failures", marker_color=BLUE,
                text=t.failures, textposition="outside", hovertemplate="%{x}<br>%{y:,} failures<extra></extra>")
    fig.add_scatter(x=t.segment.astype(str), y=t.cum, name="Cumulative %", yaxis="y2", mode="lines+markers",
                    line=dict(color=MIX, width=3), hovertemplate="%{x}<br>cumulative %{y:.0f}%<extra></extra>")
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 105], ticksuffix="%", showgrid=False,
                                  tickfont=dict(color=MUTED)))
    fig.update_yaxes(title="Failed transactions")
    return _style(fig, title or f"Failure concentration by {dim_label.lower()}", height=360)


def volume_bars(tr: pd.DataFrame, title="Monthly volume and failures"):
    if tr is None or not len(tr):
        return None
    vol = tr["transactions"] if "transactions" in tr else tr["total"]
    fails = vol - tr["successes"]
    fig = go.Figure()
    fig.add_bar(x=tr["month"], y=tr["successes"], name="Successful", marker_color=BLUE)
    fig.add_bar(x=tr["month"], y=fails, name="Failed", marker_color=NEG, text=fails, textposition="outside")
    fig.update_layout(barmode="stack")
    fig.update_yaxes(title="Transactions")
    return _style(fig, title, height=360)


def reasons_donut(reasons: pd.DataFrame, title="Failure reasons"):
    if reasons is None or not len(reasons):
        return None
    fig = go.Figure(go.Pie(labels=reasons.failure_reason, values=reasons.failures, hole=0.55, sort=True,
                           marker=dict(colors=PALETTE, line=dict(color="white", width=2)),
                           textinfo="percent", hovertemplate="%{label}<br>%{value} failures (%{percent})<extra></extra>"))
    fig.update_layout(legend=dict(orientation="v", x=1.02, y=0.5, yanchor="middle", xanchor="left"))
    return _style(fig, title, height=320)


def reconciliation_bars(r: dict):
    fig = go.Figure()
    fig.add_bar(x=["Gateway: SUCCESS", "Settlement: SETTLED"], y=[r["gateway_success"], r["settled_records"]],
                marker_color=[BLUE, MIX], text=[f"{r['gateway_success']:,}", f"{r['settled_records']:,}"],
                textposition="outside", showlegend=False)
    fig.update_yaxes(title="Transactions", range=[min(r["gateway_success"], r["settled_records"]) * 0.97,
                                                  max(r["gateway_success"], r["settled_records"]) * 1.01])
    return _style(fig, "Same metric, two sources", height=320)


def reconciliation_breakdown(r: dict):
    cats = ["Success, no settlement", "Success, PENDING", "Failed, but SETTLED"]
    vals = [r["success_no_settlement"], r["success_pending"], -r["failed_but_settled"]]
    fig = go.Figure(go.Bar(x=cats, y=vals, marker_color=[NEG, MIX, POS], text=[abs(v) for v in vals],
                           textposition="outside"))
    fig.update_yaxes(title="Transactions (gap contribution)")
    return _style(fig, f"Why they differ · net gap {r['gap']}", height=320)
