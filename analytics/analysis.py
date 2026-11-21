"""Deterministic analytics engine (no LLM here - every number shown to the user comes from this module).

* success / failure rates with Wilson 95% confidence intervals and small-sample flags
* period comparison (A = later/current, B = earlier/baseline)
* Kitagawa rate-vs-mix decomposition per segment, with an exact-sum check
* drill-down on combined dimensions, monthly trend
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config

VALID_METHODS = {"UPI", "Card", "Wallet", "NetBanking"}
VALID_CATEGORIES = {"Retail", "Food", "Travel", "Utilities", "Electronics"}
REASON_CANON = {"tech_error": "Technical Error", "technical error": "Technical Error"}
DIM_COLUMNS = {
    "payment_method": "payment_method",
    "bank": "bank_name",
    "merchant_category": "merchant_category",
    "region": "region",
    "merchant": "merchant_name",
    "failure_reason": "failure_reason",
    "bank_type": "bank_type",
}
DIM_LABELS = {"payment_method": "Payment method", "bank": "Bank", "merchant_category": "Merchant category",
              "region": "Region", "merchant": "Merchant", "failure_reason": "Failure reason",
              "bank_type": "Bank type", "method_x_bank": "Payment method × Bank"}


# --------------------------------------------------------------------------- basics
def wilson_ci(successes: int, n: int, z: float = 1.96):
    if n == 0:
        return (None, None)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def confidence_label(n: int) -> str:
    if n < config.VERY_LOW_SAMPLE_N:
        return "very low"
    if n < config.LOW_SAMPLE_N:
        return "low"
    return "ok"


def safe_rate(s: int, n: int) -> Optional[float]:
    return None if n == 0 else s / n


@dataclass
class PrepReport:
    rows_in: int
    rows_out: int
    duplicates_removed: int
    notes: List[str] = field(default_factory=list)


def prepare(df: pd.DataFrame, period_a=None, period_b=None):
    """Clean row-level evidence deterministically and tag periods. Returns (clean_df, PrepReport)."""
    rows_in = len(df)
    d = df.drop_duplicates("transaction_id", keep="first").copy()
    rep = PrepReport(rows_in, len(d), rows_in - len(d))
    if rep.duplicates_removed:
        rep.notes.append(f"{rep.duplicates_removed} duplicate transaction_id row(s) removed before computing rates.")

    if "merchant_category" in d:
        raw = d["merchant_category"]
        norm = raw.where(raw.isna(), raw.astype(str).str.strip().str.title())
        unexpected = norm.notna() & ~norm.isin(VALID_CATEGORIES)
        missing = norm.isna()
        n_fixed = int((raw.notna() & (raw != norm) & norm.isin(VALID_CATEGORIES)).sum())
        if n_fixed:
            rep.notes.append(f"{n_fixed} row(s) had non-standard category casing (e.g. 'food') - normalised.")
        if unexpected.any():
            rep.notes.append(f"{int(unexpected.sum())} row(s) had an unexpected category - grouped as 'Other'.")
        if missing.any():
            rep.notes.append(f"{int(missing.sum())} row(s) had a missing/orphan merchant - grouped as 'Unknown'.")
        norm = norm.where(~unexpected, "Other").fillna("Unknown")
        d["merchant_category"] = norm
    for col, fill in (("region", "Unknown"), ("merchant_name", "Unknown merchant"), ("bank_name", "Unknown bank"),
                      ("bank_type", "Unknown")):
        if col in d:
            n_null = int(d[col].isna().sum())
            if n_null and col == "bank_name":
                rep.notes.append(f"{n_null} row(s) had no bank - grouped as 'Unknown bank'.")
            d[col] = d[col].fillna(fill)
    if "payment_method" in d:
        bad = ~d["payment_method"].isin(VALID_METHODS)
        if bad.any():
            rep.notes.append(f"{int(bad.sum())} row(s) had an invalid payment_method - grouped as 'Invalid'.")
            d.loc[bad, "payment_method"] = "Invalid"
    if "failure_reason" in d:
        fr = d["failure_reason"]
        canon = fr.map(lambda x: REASON_CANON.get(str(x).strip().lower(), x) if isinstance(x, str) else x)
        n_c = int((canon.fillna("") != fr.fillna("")).sum())
        if n_c:
            rep.notes.append(f"{n_c} failure_reason value(s) normalised to 'Technical Error'.")
        d["failure_reason"] = canon
    d["is_success"] = (d["status"] == "SUCCESS").astype(int)
    if "txn_timestamp" in d:
        ts = pd.to_datetime(d["txn_timestamp"], errors="coerce")
        d["month"] = ts.dt.strftime("%Y-%m")
        if period_a and period_b:
            d["period"] = np.where((ts >= period_a[0]) & (ts < period_a[1]), "A",
                                   np.where((ts >= period_b[0]) & (ts < period_b[1]), "B", None))
    rep.rows_out = len(d)
    return d, rep


# --------------------------------------------------------------------------- rates
def overall(df: pd.DataFrame) -> dict:
    n, s = int(len(df)), int(df["is_success"].sum()) if len(df) else 0
    lo, hi = wilson_ci(s, n)
    return {"total": n, "successes": s, "failures": n - s, "success_rate": safe_rate(s, n),
            "ci_low": lo, "ci_high": hi, "confidence": confidence_label(n)}


def rate_table(df: pd.DataFrame, dim: str) -> pd.DataFrame:
    col = _dim_col(df, dim)
    g = df.groupby(col, dropna=False)["is_success"].agg(total="size", successes="sum").reset_index()
    g = g.rename(columns={col: "segment"})
    g["failures"] = g["total"] - g["successes"]
    g["success_rate"] = g["successes"] / g["total"]
    ci = [wilson_ci(int(s), int(n)) for s, n in zip(g.successes, g.total)]
    g["ci_low"] = [c[0] for c in ci]
    g["ci_high"] = [c[1] for c in ci]
    g["confidence"] = [confidence_label(int(n)) for n in g.total]
    return g.sort_values("total", ascending=False).reset_index(drop=True)


def _dim_col(df: pd.DataFrame, dim: str) -> str:
    if dim == "method_x_bank":
        if "method_x_bank" not in df:
            df["method_x_bank"] = df["payment_method"] + " @ " + df["bank_name"]
        return "method_x_bank"
    return DIM_COLUMNS.get(dim, dim)


# --------------------------------------------------------------------------- Kitagawa
@dataclass
class Decomposition:
    dimension: str
    R_A: Optional[float]
    R_B: Optional[float]
    delta: Optional[float]
    rate_effect: float
    mix_effect: float
    table: pd.DataFrame
    n_A: int
    n_B: int
    check_ok: bool
    empty_period: bool = False

    @property
    def rate_share(self) -> Optional[float]:
        return None if not self.delta else self.rate_effect / self.delta

    @property
    def mix_share(self) -> Optional[float]:
        return None if not self.delta else self.mix_effect / self.delta


def kitagawa(df: pd.DataFrame, dim: str) -> Decomposition:
    """Kitagawa decomposition of R_A - R_B into rate effect and mix effect across segments of `dim`.

    For each segment k:  w̄ = (w_A + w_B)/2,  r̄ = (r_A + r_B)/2
        rate_k = w̄ (r_A - r_B)      mix_k = r̄ (w_A - w_B)
    and  Σ rate_k + Σ mix_k == R_A - R_B  exactly (verified).
    A segment absent in one period has an undefined rate there; it is filled with the other
    period's rate (its weight is 0 in that period, so the identity still holds exactly).
    """
    col = _dim_col(df, dim)
    d = df[df["period"].isin(["A", "B"])]
    g = d.groupby([col, "period"])["is_success"].agg(n="size", s="sum").unstack("period", fill_value=0)
    for p in ("A", "B"):
        if ("n", p) not in g.columns:
            g[("n", p)] = 0
            g[("s", p)] = 0
    nA, nB = int(g[("n", "A")].sum()), int(g[("n", "B")].sum())
    t = pd.DataFrame({
        "segment": g.index.astype(str),
        "n_A": g[("n", "A")].values, "s_A": g[("s", "A")].values,
        "n_B": g[("n", "B")].values, "s_B": g[("s", "B")].values,
    })
    if nA == 0 or nB == 0:
        return Decomposition(dim, safe_rate(int(t.s_A.sum()), nA), safe_rate(int(t.s_B.sum()), nB), None,
                             0.0, 0.0, t, nA, nB, False, empty_period=True)
    t["w_A"] = t.n_A / nA
    t["w_B"] = t.n_B / nB
    t["r_A"] = np.where(t.n_A > 0, t.s_A / t.n_A.replace(0, np.nan), np.nan)
    t["r_B"] = np.where(t.n_B > 0, t.s_B / t.n_B.replace(0, np.nan), np.nan)
    t["r_A"] = t.r_A.fillna(t.r_B)
    t["r_B"] = t.r_B.fillna(t.r_A)
    t["w_bar"] = (t.w_A + t.w_B) / 2
    t["r_bar"] = (t.r_A + t.r_B) / 2
    t["rate_contrib"] = t.w_bar * (t.r_A - t.r_B)
    t["mix_contrib"] = t.r_bar * (t.w_A - t.w_B)
    t["total_contrib"] = t.rate_contrib + t.mix_contrib
    t["min_n"] = t[["n_A", "n_B"]].min(axis=1)
    t["confidence"] = [confidence_label(int(n)) for n in t.min_n]
    R_A, R_B = t.s_A.sum() / nA, t.s_B.sum() / nB
    delta = R_A - R_B
    rate_eff, mix_eff = float(t.rate_contrib.sum()), float(t.mix_contrib.sum())
    ok = abs((rate_eff + mix_eff) - delta) < 1e-9
    t = t.reindex(t.total_contrib.abs().sort_values(ascending=False).index).reset_index(drop=True)
    return Decomposition(dim, float(R_A), float(R_B), float(delta), rate_eff, mix_eff, t, nA, nB, ok)


def decompose_all(df: pd.DataFrame, dims: List[str]) -> Dict[str, Decomposition]:
    return {d: kitagawa(df, d) for d in dims if _dim_available(df, d)}


def _dim_available(df: pd.DataFrame, dim: str) -> bool:
    if dim == "method_x_bank":
        return "payment_method" in df and "bank_name" in df
    return DIM_COLUMNS.get(dim, dim) in df


DRIVER_PREFERENCE = ["method_x_bank", "bank", "payment_method", "merchant_category", "region", "merchant"]


def rate_drivers(decs: Dict[str, Decomposition]) -> Dict[str, pd.Series]:
    """Per dimension: the segment whose within-segment rate change contributed most in the direction of the change."""
    out = {}
    for dim, dec in decs.items():
        if dec.empty_period or not dec.delta:
            continue
        sign = -1 if dec.delta < 0 else 1
        cand = dec.table[dec.table.min_n >= config.VERY_LOW_SAMPLE_N]
        if not cand.empty:
            out[dim] = cand.loc[(cand.rate_contrib * sign).idxmax()]
    return out


def top_rate_driver(decs: Dict[str, Decomposition]):
    """Most specific driver available (e.g. 'UPI @ Cobalt Bank' rather than just 'UPI')."""
    drivers = rate_drivers(decs)
    for dim in DRIVER_PREFERENCE:
        if dim in drivers:
            row = drivers[dim]
            return dim, row, float(row.rate_contrib)
    return None


def mix_story(decs: Dict[str, Decomposition]):
    """Dimension with the largest mix effect in the direction of the change + its biggest mover."""
    best = None
    for dim, dec in decs.items():
        if dec.empty_period or not dec.delta or dim in ("method_x_bank", "merchant", "failure_reason"):
            continue
        score = dec.mix_effect * (-1 if dec.delta < 0 else 1)
        if best is None or score > best[1]:
            best = (dim, score)
    if not best:
        return None
    dec = decs[best[0]]
    mover = dec.table.loc[(dec.table.w_A - dec.table.w_B).abs().idxmax()]
    return dec, mover


def trend(df: pd.DataFrame) -> pd.DataFrame:
    d = df.dropna(subset=["month"]) if "month" in df else df
    g = d.groupby("month")["is_success"].agg(total="size", successes="sum").reset_index()
    g["success_rate"] = g.successes / g.total
    return g


def failure_reasons(df: pd.DataFrame) -> pd.DataFrame:
    f = df[df.is_success == 0]
    return f.groupby(f["failure_reason"].fillna("Unspecified")).size().rename("failures").reset_index() \
        .sort_values("failures", ascending=False)


def top_affected(df: pd.DataFrame, dim: str, n: int = 10) -> pd.DataFrame:
    """Top segments by number of failures in period A, with rate change vs B where available."""
    a = rate_table(df[df.period == "A"], dim) if "period" in df else rate_table(df, dim)
    if "period" in df and (df.period == "B").any():
        b = rate_table(df[df.period == "B"], dim)[["segment", "success_rate"]].rename(
            columns={"success_rate": "success_rate_prev"})
        a = a.merge(b, on="segment", how="left")
        a["change_pt"] = (a.success_rate - a.success_rate_prev) * 100
    return a.sort_values("failures", ascending=False).head(n).reset_index(drop=True)
