"""Deterministic SQL templates built from a structured intent.

Used (a) in mock mode, (b) as the fallback when LLM-drafted SQL fails validation, and
(c) for the row-level evidence query that feeds the analytics engine.
All user-supplied values are bound as `?` params - never interpolated.
"""
from __future__ import annotations

from typing import List, Tuple

import config
from analytics.catalog import DIMENSIONS

FILTER_COLUMNS = {
    "payment_method": "t.payment_method",
    "bank": "b.bank_name",
    "bank_id": "t.bank_id",
    "merchant_category": "m.category",
    "region": "m.region",
    "status": "t.status",
}


def _role_has_banks(role: str) -> bool:
    return "banks" not in config.ROLES.get(role, config.ROLES[config.DEFAULT_ROLE])["denied_tables"]


def _filters_sql(filters: dict) -> Tuple[List[str], list]:
    clauses, params = [], []
    for key, val in (filters or {}).items():
        col = FILTER_COLUMNS.get(key)
        if not col or val in (None, "", []):
            continue
        if key == "merchant_category":           # tolerate casing defects ('food')
            clauses.append(f"UPPER({col}) = UPPER(?)")
        else:
            clauses.append(f"{col} = ?")
        params.append(val)
    return clauses, params


def _period_clause(intent) -> Tuple[str, list]:
    pa, pb = intent.get("period_a"), intent.get("period_b")
    if pa and pb:
        return ("((t.txn_timestamp >= ? AND t.txn_timestamp < ?) OR (t.txn_timestamp >= ? AND t.txn_timestamp < ?))",
                [pa["start"], pa["end"], pb["start"], pb["end"]])
    if pa:
        return "(t.txn_timestamp >= ? AND t.txn_timestamp < ?)", [pa["start"], pa["end"]]
    return "", []


def evidence_sql(intent: dict, role: str) -> Tuple[str, list]:
    """Row-level evidence for the analytics engine (pandas computes all metrics from this)."""
    banks = _role_has_banks(role)
    cols = ["t.transaction_id", "t.txn_timestamp", "t.amount", "t.payment_method", "t.status", "t.failure_reason",
            "m.category AS merchant_category", "m.region", "m.merchant_name"]
    joins = ["LEFT JOIN merchants m ON t.merchant_id = m.merchant_id"]
    if banks:
        cols += ["b.bank_name", "b.bank_type"]
        joins.append("LEFT JOIN banks b ON t.bank_id = b.bank_id")
    where, params = [], []
    pc, pp = _period_clause(intent)
    if pc:
        where.append(pc); params += pp
    else:
        where.append("t.txn_timestamp IS NOT NULL")   # undated rows can't be placed in any window
    fc, fp = _filters_sql({k: v for k, v in (intent.get("filters") or {}).items()
                           if banks or k not in ("bank", "bank_id")})
    where += fc; params += fp
    sql = f"SELECT {', '.join(cols)} FROM transactions t {' '.join(joins)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, params


def primary_sql(intent: dict, role: str) -> Tuple[str, list]:
    """Aggregated, human-readable 'answer' query (shown in the Query Flow tab)."""
    metric = intent.get("metric", "success_rate")
    group_by = [g for g in (intent.get("group_by") or []) if g in DIMENSIONS]
    atype = intent.get("analysis_type", "segment_breakdown")
    pa, pb = intent.get("period_a"), intent.get("period_b")

    select, group_cols, joins = [], [], {"transactions"}
    if atype == "trend":
        select.append("strftime('%Y-%m', t.txn_timestamp) AS month")
        group_cols.append("month")
    elif pa and pb:
        select.append("CASE WHEN t.txn_timestamp >= ? AND t.txn_timestamp < ? THEN ? ELSE ? END AS period")
    for g in group_by:
        expr, _, tables = DIMENSIONS[g]
        select.append(f"{expr} AS {g}")
        group_cols.append(g)
        joins |= tables

    measures = [
        "COUNT(DISTINCT t.transaction_id) AS transactions",
        "COUNT(DISTINCT CASE WHEN t.status = 'SUCCESS' THEN t.transaction_id END) AS successes",
        "ROUND(100.0 * COUNT(DISTINCT CASE WHEN t.status = 'SUCCESS' THEN t.transaction_id END) "
        "/ COUNT(DISTINCT t.transaction_id), 2) AS success_rate_pct",
    ]
    if metric == "txn_value":
        measures.append("ROUND(SUM(t.amount), 2) AS total_value_inr")
    if metric == "failure_rate":
        measures.append("ROUND(100.0 - 100.0 * COUNT(DISTINCT CASE WHEN t.status = 'SUCCESS' THEN t.transaction_id END) "
                        "/ COUNT(DISTINCT t.transaction_id), 2) AS failure_rate_pct")
    select += measures
    for k in (intent.get("filters") or {}):
        if k in ("merchant_category", "region"):
            joins.add("merchants")
        if k == "bank":
            joins.add("banks")

    join_sql = ""
    if "merchants" in joins:
        join_sql += " LEFT JOIN merchants m ON t.merchant_id = m.merchant_id"
    if "banks" in joins:
        join_sql += " LEFT JOIN banks b ON t.bank_id = b.bank_id"

    params: list = []
    if atype != "trend" and pa and pb:
        params += [pa["start"], pa["end"], pa["label"], pb["label"]]
    sql = f"SELECT {', '.join(select)} FROM transactions t{join_sql}"
    where, wp = [], []
    pc, pp = _period_clause(intent)
    if pc:
        where.append(pc); wp += pp
    else:
        where.append("t.txn_timestamp IS NOT NULL")
    fc, fp = _filters_sql(intent.get("filters") or {})
    where += fc; wp += fp
    sql += " WHERE " + " AND ".join(where)
    params += wp
    if atype != "trend" and pa and pb:
        group_cols = ["period"] + group_cols
    if group_cols:
        sql += " GROUP BY " + ", ".join(group_cols)
        order = "month" if atype == "trend" else ", ".join(group_cols)
        sql += f" ORDER BY {order}"
    return sql, params


def trend_sql(intent: dict, role: str) -> Tuple[str, list]:
    """Monthly success-rate trend across the whole dataset (with the question's filters)."""
    t = dict(intent); t["analysis_type"] = "trend"; t["group_by"] = []
    t["period_a"] = None; t["period_b"] = None
    if not _role_has_banks(role):
        t["filters"] = {k: v for k, v in (intent.get("filters") or {}).items() if k not in ("bank", "bank_id")}
    return primary_sql(t, role)
