"""Generate the SYNTHETIC PayInsight database (not real Pine Labs data).

Run:  python data/seed_data.py

Deterministic (seed=42). Builds banks, merchants, ~12k transactions (May–Oct 2026),
and a separate settlements feed, with three planted business scenarios and a set
of planted data defects recorded in data/defects_manifest.json.

Scenarios
  1. Sep -> Oct decline (~92% -> ~85%): UPI at Cobalt Bank (B03) collapses
     (gateway "Technical Error"), plus a mix shift away from Retail toward Utilities.
  2. Jul -> Aug decline driven purely by merchant-mix shift (Utilities 10% -> 30%),
     per-segment success rates unchanged.
  3. Oct: settlements feed disagrees with transactions (unsettled successes,
     settled failures).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

SEED = 42
MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
VOLUMES = {"2026-05": 1900, "2026-06": 1950, "2026-07": 2000, "2026-08": 2100, "2026-09": 2167, "2026-10": 2000}

CATEGORIES = ["Retail", "Food", "Travel", "Utilities", "Electronics"]
BASE_CAT_W = {"Retail": .35, "Food": .25, "Travel": .15, "Utilities": .10, "Electronics": .15}
CAT_W = {m: dict(BASE_CAT_W) for m in MONTHS}
# Scenario 2: Aug mix shift into Utilities (other categories scaled down proportionally)
CAT_W["2026-08"] = {c: (.30 if c == "Utilities" else w * .70 / .90) for c, w in BASE_CAT_W.items()}
# Scenario 1: Oct mix shift away from Retail
CAT_W["2026-10"] = {"Retail": .22, "Food": .25, "Travel": .18, "Utilities": .22, "Electronics": .13}

METHODS = ["UPI", "Card", "Wallet", "NetBanking"]
METHOD_W = [.50, .25, .10, .15]
METHOD_BASE = {"UPI": .945, "Card": .93, "Wallet": .955, "NetBanking": .90}
CAT_ADJ = {"Retail": .01, "Food": 0.0, "Travel": -.02, "Utilities": -.12, "Electronics": -.01}

BANKS = [
    ("B01", "Alpha Bank", "Domestic", .11, .005), ("B02", "Beacon Bank", "Domestic", .10, 0.0),
    ("B03", "Cobalt Bank", "Domestic", .20, 0.0), ("B04", "Delta Bank", "Domestic", .10, -.01),
    ("B05", "Ember Bank", "Domestic", .10, .01), ("B06", "Falcon Bank", "Domestic", .09, -.005),
    ("B07", "Granite Bank", "Domestic", .08, 0.0), ("B08", "Harbor Bank", "International", .08, -.015),
    ("B09", "Iris Bank", "International", .07, -.01), ("B10", "Juniper Bank", "International", .07, 0.0),
]
REGIONS = ["North", "South", "East", "West"]
MERCHANTS_PER_CAT = {"Retail": 18, "Food": 14, "Travel": 9, "Utilities": 8, "Electronics": 11}
NAME_A = ["Sunrise", "Blue Lotus", "Metro", "Urban", "Golden", "Green Leaf", "Silver", "Bright", "Nova", "Prime",
          "Coastal", "Summit", "Maple", "River", "Orbit", "Zenith", "Velvet", "Crimson"]
NAME_B = {"Retail": "Mart", "Food": "Kitchen", "Travel": "Journeys", "Utilities": "Power & Water", "Electronics": "Gadgets"}
AMOUNT_MEAN = {"Retail": 1500, "Food": 400, "Travel": 8000, "Utilities": 2000, "Electronics": 12000}

FAIL_REASONS_CARD = (["Insufficient Funds", "Card Expired", "Technical Error", "Limit Exceeded", "Timeout"],
                     [.30, .20, .15, .20, .15])
FAIL_REASONS_OTHER = (["Insufficient Funds", "Technical Error", "Limit Exceeded", "Timeout"], [.40, .20, .20, .20])
FAIL_REASONS_GATEWAY = (["Technical Error", "Timeout", "Insufficient Funds"], [.75, .15, .10])


def success_prob(month: str, method: str, bank_id: str, category: str, bank_adj: float) -> float:
    p = METHOD_BASE[method] + CAT_ADJ[category] + bank_adj
    if month == "2026-10" and method == "UPI":
        p = (0.42 + CAT_ADJ[category]) if bank_id == "B03" else p - 0.01   # Scenario 1
    return float(np.clip(p, 0.05, 0.995))


def build_dimensions(rng):
    banks = pd.DataFrame([(b, n, t) for b, n, t, _, _ in BANKS], columns=["bank_id", "bank_name", "bank_type"])
    rows, i = [], 1
    for cat in CATEGORIES:
        for k in range(MERCHANTS_PER_CAT[cat]):
            rows.append((f"M{i:03d}", f"{NAME_A[k % len(NAME_A)]} {NAME_B[cat]}", cat, REGIONS[rng.integers(0, 4)]))
            i += 1
    merchants = pd.DataFrame(rows, columns=["merchant_id", "merchant_name", "category", "region"])
    return banks, merchants


def build_transactions(rng, merchants):
    bank_ids = [b[0] for b in BANKS]
    bank_w = np.array([b[3] for b in BANKS]); bank_w /= bank_w.sum()
    bank_adj = {b[0]: b[4] for b in BANKS}
    by_cat = {c: merchants.loc[merchants.category == c, "merchant_id"].tolist() for c in CATEGORIES}
    hour_w = np.array([1, 1, 1, 1, 1, 2, 3, 5, 6, 7, 8, 8, 9, 8, 7, 7, 8, 9, 10, 11, 10, 7, 4, 2], float)
    hour_w /= hour_w.sum()

    frames, txn_no = [], 1
    for month in MONTHS:
        n = VOLUMES[month]
        cw = CAT_W[month]
        cats = rng.choice(CATEGORIES, size=n, p=np.array([cw[c] for c in CATEGORIES]) / sum(cw.values()))
        merch = [by_cat[c][min(int(rng.zipf(1.6)) - 1, len(by_cat[c]) - 1)] for c in cats]
        y, mo = map(int, month.split("-"))
        days_in_month = pd.Period(month).days_in_month
        ts = [pd.Timestamp(y, mo, int(rng.integers(1, days_in_month + 1)), int(rng.choice(24, p=hour_w)),
                           int(rng.integers(0, 60)), int(rng.integers(0, 60))) for _ in range(n)]
        df = pd.DataFrame({
            "transaction_id": [f"T{txn_no + k:06d}" for k in range(n)],
            "ts": ts,
            "merchant_id": merch,
            "category": cats,
            "bank_id": rng.choice(bank_ids, size=n, p=bank_w),
            "payment_method": rng.choice(METHODS, size=n, p=METHOD_W),
            "month": month,
        })
        txn_no += n
        df["p"] = [success_prob(month, m, b, c, bank_adj[b])
                   for m, b, c in zip(df.payment_method, df.bank_id, df.category)]
        # Exact-count assignment per cell -> scenario numbers are stable, not noise-driven.
        df["status"] = "FAILED"
        for _, idx in df.groupby(["payment_method", "bank_id", "category"]).groups.items():
            idx = rng.permutation(np.array(idx))
            k = int(round(df.loc[idx, "p"].sum()))
            df.loc[idx[:k], "status"] = "SUCCESS"
        frames.append(df)

    tx = pd.concat(frames, ignore_index=True).sort_values("ts").reset_index(drop=True)
    tx["amount"] = [round(float(rng.lognormal(np.log(AMOUNT_MEAN[c]), 0.6)), 2) for c in tx.category]
    tx["currency"] = "INR"

    reasons = []
    for st, m, b, mon in zip(tx.status, tx.payment_method, tx.bank_id, tx.month):
        if st == "SUCCESS":
            reasons.append(None); continue
        if mon == "2026-10" and m == "UPI" and b == "B03":
            opts, w = FAIL_REASONS_GATEWAY
        elif m == "Card":
            opts, w = FAIL_REASONS_CARD
        else:
            opts, w = FAIL_REASONS_OTHER
        reasons.append(str(rng.choice(opts, p=w)))
    tx["failure_reason"] = reasons
    tx["txn_timestamp"] = tx.ts.dt.strftime("%Y-%m-%d %H:%M:%S")
    return tx


def build_settlements(rng, tx):
    """Settlement feed derived from the clean transactions, with Oct disagreements planted."""
    succ = tx[tx.status == "SUCCESS"]
    oct_succ = succ[succ.month == "2026-10"].index
    oct_fail = tx[(tx.status == "FAILED") & (tx.month == "2026-10")].index
    missing = set(rng.choice(oct_succ, size=30, replace=False))                    # success, no settlement row
    pending_oct = set(rng.choice(sorted(set(oct_succ) - missing), size=20, replace=False))
    other_succ = succ[succ.month != "2026-10"].index
    pending_lag = set(rng.choice(other_succ, size=int(len(other_succ) * 0.002), replace=False))
    settled_failures = list(rng.choice(oct_fail, size=12, replace=False))           # failed, but settled

    rows = []
    for i in list(succ.index) + settled_failures:
        if i in missing:
            continue
        r = tx.loc[i]
        pending = i in pending_oct or i in pending_lag
        sdate = (r.ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        rows.append((r.transaction_id, sdate, None if pending else round(r.amount * 0.982, 2),
                     "PENDING" if pending else "SETTLED"))
    st = pd.DataFrame(rows, columns=["transaction_id", "settlement_date", "settled_amount", "settlement_status"])
    st.insert(0, "settlement_id", [f"S{k + 1:06d}" for k in range(len(st))])
    truth = {"oct_success_missing_settlement": 30, "oct_success_pending": 20,
             "oct_failed_but_settled": 12, "other_months_pending_lag": len(pending_lag)}
    return st, truth


def plant_defects(rng, tx, merchants):
    """Plant data-quality defects; return modified frames and the manifest of exactly what was planted."""
    tx = tx.copy()
    pool = list(rng.permutation(tx.index))
    take = lambda k, cond=None: [pool.pop(pool.index(i)) for i in [j for j in pool if cond is None or cond(j)][:k]]

    null_ts = take(3, lambda j: tx.at[j, "month"] == "2026-05")
    tx.loc[null_ts, "txn_timestamp"] = None
    null_bank = take(20); tx.loc[null_bank, "bank_id"] = None
    null_merch = take(10); tx.loc[null_merch, "merchant_id"] = None
    orphan = take(5); tx.loc[orphan, "merchant_id"] = "M999"
    invalid_pm = take(1); tx.loc[invalid_pm, "payment_method"] = "Crypto"
    tech = take(15, lambda j: tx.at[j, "failure_reason"] == "Technical Error")
    tx.loc[tech[:8], "failure_reason"] = "tech_error"
    tx.loc[tech[8:], "failure_reason"] = "technical error"
    succ_reason = take(2, lambda j: tx.at[j, "status"] == "SUCCESS")
    tx.loc[succ_reason, "failure_reason"] = "Timeout"
    dup_src = take(5, lambda j: tx.at[j, "month"] in ("2026-09", "2026-10"))
    tx = pd.concat([tx, tx.loc[dup_src]], ignore_index=True)

    merchants = merchants.copy()
    # pick mid-popularity merchants so the defects are visible but don't swamp the scenarios
    food = merchants.index[merchants.category == "Food"][[5, 8]]
    merchants.loc[food[0], "category"] = "food"
    merchants.loc[food[1], "category"] = "FOOD"
    retail = merchants.index[merchants.category == "Retail"][6]
    merchants.loc[retail, "category"] = "Misc"

    manifest = {
        "duplicate_transaction_ids": 5,
        "null_bank_id": 20,
        "null_merchant_id": 10,
        "orphan_merchant_id": 5,
        "invalid_payment_method": 1,
        "failure_reason_variants": 15,
        "null_timestamp": 3,
        "success_with_failure_reason": 2,
        "merchant_category_case_variants": 2,
        "merchant_unexpected_category": 1,
        "affected_merchants": merchants.loc[[food[0], food[1], retail], "merchant_id"].tolist(),
    }
    return tx, merchants, manifest


def monthly_truth(tx):
    clean = tx.drop_duplicates("transaction_id")
    out = {}
    for m, g in clean.groupby("month"):
        out[m] = {"total": int(len(g)), "success": int((g.status == "SUCCESS").sum()),
                  "success_rate": round(float((g.status == "SUCCESS").mean()), 4)}
    return out


def main(db_path: Path = config.DB_PATH) -> dict:
    rng = np.random.default_rng(SEED)
    banks, merchants = build_dimensions(rng)
    tx = build_transactions(rng, merchants)
    settlements, settle_truth = build_settlements(rng, tx)
    truth_before_defects = monthly_truth(tx)
    tx, merchants, manifest = plant_defects(rng, tx, merchants)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript((ROOT / "data" / "schema.sql").read_text())
    banks.to_sql("banks", con, if_exists="append", index=False)
    merchants.to_sql("merchants", con, if_exists="append", index=False)
    cols = ["transaction_id", "txn_timestamp", "amount", "currency", "merchant_id", "bank_id",
            "payment_method", "status", "failure_reason"]
    tx[cols].to_sql("transactions", con, if_exists="append", index=False)
    settlements.to_sql("settlements", con, if_exists="append", index=False)
    con.commit()
    con.close()

    manifest.update({
        "synthetic": True,
        "seed": SEED,
        "row_counts": {"banks": len(banks), "merchants": len(merchants), "transactions": len(tx),
                       "settlements": len(settlements)},
        "monthly_truth_clean": truth_before_defects,
        "settlement_truth": settle_truth,
    })
    if Path(db_path).resolve() == config.DB_PATH.resolve():
        config.MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    return manifest


def verify(manifest: dict) -> list:
    """Scenario gate: returns a list of failures (empty = pass)."""
    t = manifest["monthly_truth_clean"]
    fails = []
    if not 0.91 <= t["2026-09"]["success_rate"] <= 0.93:
        fails.append(f"Sep rate {t['2026-09']['success_rate']} not ~92%")
    if not 0.84 <= t["2026-10"]["success_rate"] <= 0.86:
        fails.append(f"Oct rate {t['2026-10']['success_rate']} not ~85%")
    drop = t["2026-07"]["success_rate"] - t["2026-08"]["success_rate"]
    if not 0.015 <= drop <= 0.035:
        fails.append(f"Jul->Aug drop {drop:.4f} not 1.5-3.5pt")
    return fails


if __name__ == "__main__":
    m = main()
    for month, v in m["monthly_truth_clean"].items():
        print(f"{month}: {v['success']}/{v['total']} = {v['success_rate']:.2%}")
    print("rows:", m["row_counts"])
    problems = verify(m)
    if problems:
        print("SCENARIO CHECK FAILED:", problems)
        sys.exit(1)
    print("Scenario checks passed. DB ->", config.DB_PATH)
