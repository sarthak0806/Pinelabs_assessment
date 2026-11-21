import json
import sqlite3

import config
from data import seed_data


def _con():
    return sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True)


def test_scenario_targets_hold():
    m = json.loads(config.MANIFEST_PATH.read_text())
    assert seed_data.verify(m) == []


def test_defect_counts_match_manifest():
    m = json.loads(config.MANIFEST_PATH.read_text())
    con = _con()
    q = lambda s: con.execute(s).fetchone()[0]
    assert q("SELECT COUNT(*) - COUNT(DISTINCT transaction_id) FROM transactions") == m["duplicate_transaction_ids"]
    assert q("SELECT COUNT(*) FROM transactions WHERE bank_id IS NULL") == m["null_bank_id"]
    assert q("SELECT COUNT(*) FROM transactions WHERE merchant_id IS NULL") == m["null_merchant_id"]
    assert q("SELECT COUNT(*) FROM transactions WHERE merchant_id='M999'") == m["orphan_merchant_id"]
    assert q("SELECT COUNT(*) FROM transactions WHERE payment_method NOT IN ('UPI','Card','Wallet','NetBanking')") == 1
    assert q("SELECT COUNT(*) FROM transactions WHERE txn_timestamp IS NULL") == m["null_timestamp"]
    assert q("SELECT COUNT(*) FROM merchants WHERE category NOT IN ('Retail','Food','Travel','Utilities','Electronics')") == 3


def test_seed_is_deterministic(tmp_path):
    a = seed_data.main(tmp_path / "a.db")
    b = seed_data.main(tmp_path / "b.db")
    assert a["monthly_truth_clean"] == b["monthly_truth_clean"]
    assert a["row_counts"] == b["row_counts"]
