import pandas as pd
import pytest

from analytics import analysis as an
from analytics import quality, sqlgen
from utils.db import get_db


def _rows(period, seg, n, s):
    return [{"transaction_id": f"{period}{seg}{i}", "period": period, "payment_method": seg,
             "is_success": int(i < s)} for i in range(n)]


def test_kitagawa_sums_exactly_to_delta():
    df = pd.DataFrame(_rows("B", "UPI", 500, 460) + _rows("B", "Card", 500, 450)
                      + _rows("A", "UPI", 700, 560) + _rows("A", "Card", 300, 270))
    dec = an.kitagawa(df, "payment_method")
    assert dec.check_ok
    assert dec.R_B == pytest.approx(0.91)
    assert dec.R_A == pytest.approx(0.83)
    assert dec.rate_effect + dec.mix_effect == pytest.approx(dec.delta, abs=1e-12)
    # hand-computed: UPI r 0.92->0.80 (w̄=.6), Card 0.90->0.90; mix: UPI w .5->.7 (r̄=.86), Card .5->.3 (r̄=.90)
    assert dec.rate_effect == pytest.approx(0.6 * -0.12)
    assert dec.mix_effect == pytest.approx(0.86 * 0.2 + 0.90 * -0.2)


def test_pure_mix_shift_has_zero_rate_effect():
    df = pd.DataFrame(_rows("B", "Util", 100, 80) + _rows("B", "Retail", 900, 855)
                      + _rows("A", "Util", 300, 240) + _rows("A", "Retail", 700, 665))
    dec = an.kitagawa(df, "payment_method")
    assert dec.rate_effect == pytest.approx(0.0)
    assert dec.mix_effect == pytest.approx(dec.delta)
    assert dec.delta < 0


def test_segment_missing_in_one_period_still_sums():
    df = pd.DataFrame(_rows("B", "UPI", 100, 90) + _rows("A", "UPI", 100, 80) + _rows("A", "Wallet", 50, 49))
    dec = an.kitagawa(df, "payment_method")
    assert dec.check_ok


def test_empty_period_handled():
    df = pd.DataFrame(_rows("A", "UPI", 10, 9))
    dec = an.kitagawa(df, "payment_method")
    assert dec.empty_period and dec.delta is None


def test_wilson_ci_and_zero_division():
    assert an.wilson_ci(0, 0) == (None, None)
    lo, hi = an.wilson_ci(90, 100)
    assert 0.82 < lo < 0.9 < hi < 0.95
    assert an.safe_rate(0, 0) is None
    assert an.confidence_label(5) == "very low"


def _evidence(intent, role="analyst"):
    sql, params = sqlgen.evidence_sql(intent, role)
    raw = get_db().safe_query(sql, params, role=role, max_rows=50000).df
    pa, pb = intent["period_a"], intent["period_b"]
    df, rep = an.prepare(raw, (pa["start"], pa["end"]), (pb["start"], pb["end"]))
    return raw, df, rep


SEP = {"start": "2026-09-01", "end": "2026-10-01", "label": "Sep 2026"}
OCT = {"start": "2026-10-01", "end": "2026-11-01", "label": "Oct 2026"}
JUL = {"start": "2026-07-01", "end": "2026-08-01", "label": "Jul 2026"}
AUG = {"start": "2026-08-01", "end": "2026-09-01", "label": "Aug 2026"}


def test_scenario1_oct_decline_driven_by_upi_at_cobalt():
    raw, df, rep = _evidence({"period_a": OCT, "period_b": SEP, "filters": {}})
    assert rep.duplicates_removed == 5
    decs = an.decompose_all(df, ["payment_method", "bank", "merchant_category", "method_x_bank"])
    assert all(d.check_ok for d in decs.values())
    d = decs["payment_method"]
    assert -0.09 < d.delta < -0.06
    dim, row, _ = an.top_rate_driver(decs)
    assert row.segment == "UPI @ Cobalt Bank"


def test_scenario2_aug_decline_is_mostly_mix():
    _, df, _ = _evidence({"period_a": AUG, "period_b": JUL, "filters": {}})
    dec = an.kitagawa(df, "merchant_category")
    assert dec.delta < -0.015
    assert dec.mix_share > 0.8


def test_quality_checks_flag_planted_defects():
    raw, df, rep = _evidence({"period_a": OCT, "period_b": SEP, "filters": {}})
    checks = {c.name: c for c in quality.result_checks(raw)}
    assert checks["Unique transaction_id"].count == 5
    assert checks["Unique transaction_id"].status == "warn"
    ds = {c.name: c for c in quality.dataset_checks(get_db(), "analyst")}
    assert ds["Timestamp present (dataset)"].count == 3
    assert ds["Merchant referential integrity (dataset)"].count == 5


def test_reconciliation_detects_october_disagreement():
    r = quality.reconcile_settlements(get_db(), "analyst", "2026-10-01", "2026-11-01")
    assert r["success_no_settlement"] == 30
    assert r["success_pending"] == 20
    assert r["failed_but_settled"] == 12
    assert r["gap"] == 30 + 20 - 12


def test_reconciliation_denied_for_restricted_role():
    from analytics.security import SQLValidationError
    with pytest.raises(SQLValidationError):
        quality.reconcile_settlements(get_db(), "merchant_analyst", "2026-10-01", "2026-11-01")
