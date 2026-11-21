"""Data-quality checks + source reconciliation (deterministic).

Result-level checks run on the row-level evidence actually used for the answer;
dataset-level checks run validated SELECTs against the full tables.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List

import pandas as pd

from analytics.analysis import VALID_CATEGORIES, VALID_METHODS


@dataclass
class Check:
    name: str
    status: str          # "pass" | "warn" | "fail"
    count: int
    detail: str

    def as_dict(self):
        return asdict(self)


def result_checks(df: pd.DataFrame) -> List[Check]:
    """Checks on the raw (pre-cleaning) evidence rows."""
    out: List[Check] = []
    n = len(df)
    if n == 0:
        return [Check("Non-empty result", "fail", 0, "Query returned no rows for the selected scope.")]
    dup = int(df.duplicated("transaction_id").sum())
    out.append(Check("Unique transaction_id", "warn" if dup else "pass", dup,
                     f"{dup} duplicate transaction row(s) found (removed before computing rates)." if dup else "No duplicates."))
    if "bank_name" in df:
        nb = int(df["bank_name"].isna().sum())
        out.append(Check("Bank present", "warn" if nb else "pass", nb,
                         f"{nb} row(s) ({nb / n:.1%}) have no bank - shown as 'Unknown bank'." if nb else "All rows have a bank."))
    if "merchant_category" in df:
        nm = int(df["merchant_category"].isna().sum())
        out.append(Check("Merchant resolvable", "warn" if nm else "pass", nm,
                         f"{nm} row(s) ({nm / n:.1%}) have a missing or orphan merchant_id - category unknown." if nm else "All merchants resolve."))
        cats = df["merchant_category"].dropna()
        bad = int((~cats.isin(VALID_CATEGORIES)).sum())
        out.append(Check("Category taxonomy", "warn" if bad else "pass", bad,
                         f"{bad} row(s) use non-standard categories (e.g. 'food', 'Misc')." if bad else "Categories consistent."))
    pm = int((~df["payment_method"].isin(VALID_METHODS)).sum())
    out.append(Check("Payment method accepted values", "warn" if pm else "pass", pm,
                     f"{pm} row(s) with invalid payment_method (e.g. 'Crypto')." if pm else "All values valid."))
    fr = df["failure_reason"].dropna()
    var = int(fr.isin(["tech_error", "technical error"]).sum())
    out.append(Check("Failure-reason taxonomy", "warn" if var else "pass", var,
                     f"{var} failure_reason value(s) use inconsistent spellings (normalised)." if var else "Consistent."))
    sw = int(((df["status"] == "SUCCESS") & df["failure_reason"].notna()).sum())
    out.append(Check("Status / reason consistency", "warn" if sw else "pass", sw,
                     f"{sw} SUCCESS row(s) carry a failure_reason." if sw else "Consistent."))
    return out


def dataset_checks(db, role: str) -> List[Check]:
    """Full-table checks via validated, parameter-free SELECTs (respecting role)."""
    out = []
    q = lambda sql: int(db.safe_query(sql, role=role).df.iloc[0, 0])
    nt = q("SELECT COUNT(*) FROM transactions WHERE txn_timestamp IS NULL")
    out.append(Check("Timestamp present (dataset)", "warn" if nt else "pass", nt,
                     f"{nt} transaction(s) have no timestamp and are excluded from every time-based analysis." if nt else "All rows timestamped."))
    orphan = q("SELECT COUNT(*) FROM transactions t LEFT JOIN merchants m ON t.merchant_id = m.merchant_id "
               "WHERE t.merchant_id IS NOT NULL AND m.merchant_id IS NULL")
    out.append(Check("Merchant referential integrity (dataset)", "warn" if orphan else "pass", orphan,
                     f"{orphan} transaction(s) reference a merchant_id not in merchants (orphans)." if orphan else "OK."))
    return out


def summarize(checks: List[Check], rows_analyzed: int) -> dict:
    issues = [c for c in checks if c.status != "pass"]
    affected = sum(c.count for c in issues if "dataset" not in c.name)
    return {
        "rows_analyzed": rows_analyzed,
        "issues": len(issues),
        "affected_rows": affected,
        "affected_pct": (affected / rows_analyzed) if rows_analyzed else 0.0,
        "score": max(0.0, 1 - (affected / rows_analyzed)) if rows_analyzed else 0.0,
    }


def reconcile_settlements(db, role: str, start: str, end: str) -> dict:
    """Source disagreement: success per gateway (transactions) vs per settlement system."""
    sql = """
    SELECT
      SUM(CASE WHEN t.status = 'SUCCESS' THEN 1 ELSE 0 END) AS gateway_success,
      SUM(CASE WHEN t.status = 'SUCCESS' AND s.settlement_status = 'SETTLED' THEN 1 ELSE 0 END) AS both_agree,
      SUM(CASE WHEN t.status = 'SUCCESS' AND s.transaction_id IS NULL THEN 1 ELSE 0 END) AS success_no_settlement,
      SUM(CASE WHEN t.status = 'SUCCESS' AND s.settlement_status = 'PENDING' THEN 1 ELSE 0 END) AS success_pending,
      SUM(CASE WHEN t.status = 'FAILED' AND s.settlement_status = 'SETTLED' THEN 1 ELSE 0 END) AS failed_but_settled,
      SUM(CASE WHEN s.settlement_status = 'SETTLED' THEN 1 ELSE 0 END) AS settled_records,
      COUNT(*) AS total
    FROM (SELECT DISTINCT transaction_id, status, txn_timestamp FROM transactions
          WHERE txn_timestamp >= ? AND txn_timestamp < ?) AS t
    LEFT JOIN settlements s ON s.transaction_id = t.transaction_id
    """
    r = db.safe_query(sql, [start, end], role=role)
    row = {k: int(v or 0) for k, v in r.df.iloc[0].items()}
    total = row["total"] or 1
    row["gateway_success_rate"] = row["gateway_success"] / total
    row["settlement_success_rate"] = row["settled_records"] / total
    row["gap"] = row["gateway_success"] - row["settled_records"]
    row["gap_pt"] = (row["gateway_success_rate"] - row["settlement_success_rate"]) * 100
    row["sql"] = r.sql
    row["params"] = [start, end]
    return row
