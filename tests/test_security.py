import sqlite3

import pytest

from analytics.security import SQLValidationError, validate_sql
from utils.db import get_db

MALICIOUS = [
    "DROP TABLE transactions",
    "DELETE FROM transactions",
    "UPDATE transactions SET status='SUCCESS'",
    "INSERT INTO banks VALUES ('X','x','x')",
    "SELECT 1; DROP TABLE transactions",
    "SELECT transaction_id FROM transactions UNION SELECT bank_id FROM banks",
    "PRAGMA table_info(transactions)",
    "ATTACH DATABASE 'evil.db' AS evil",
    "SELECT load_extension('evil')",
    "SELECT * FROM sqlite_master",
    "SELECT secret FROM transactions",
    "SELECT transaction_id FROM transactions -- sneaky comment",
    "SELECT * FROM transactions t, banks b",                      # cartesian join
    "CREATE TABLE x AS SELECT * FROM transactions",
]


@pytest.mark.parametrize("sql", MALICIOUS)
def test_malicious_sql_rejected(sql):
    assert not validate_sql(sql).ok


def test_valid_select_passes_and_gets_limit():
    v = validate_sql("SELECT b.bank_name, COUNT(*) AS n FROM transactions t "
                     "JOIN banks b ON t.bank_id = b.bank_id WHERE t.status = ? GROUP BY b.bank_name",
                     params=["SUCCESS"])
    assert v.ok, v.errors
    assert "LIMIT 1000" in v.sql


def test_existing_large_limit_is_capped():
    v = validate_sql("SELECT transaction_id FROM transactions LIMIT 999999")
    assert v.ok and "LIMIT 1000" in v.sql


def test_placeholder_param_mismatch_rejected():
    assert not validate_sql("SELECT COUNT(*) FROM transactions WHERE status = ?", params=[]).ok


def test_role_denied_table_and_column():
    v = validate_sql("SELECT bank_name FROM banks", role="merchant_analyst")
    assert not v.ok and v.access_denied
    v = validate_sql("SELECT bank_id, COUNT(*) FROM transactions GROUP BY bank_id", role="merchant_analyst")
    assert not v.ok and v.access_denied
    assert validate_sql("SELECT m.category, COUNT(*) FROM transactions t JOIN merchants m "
                        "ON t.merchant_id = m.merchant_id GROUP BY m.category", role="merchant_analyst").ok


def test_too_many_joins_blocked():
    sql = ("SELECT 1 FROM transactions t JOIN banks b ON t.bank_id=b.bank_id JOIN merchants m ON t.merchant_id=m.merchant_id "
           "JOIN settlements s ON s.transaction_id=t.transaction_id JOIN banks b2 ON b2.bank_id=t.bank_id "
           "JOIN merchants m2 ON m2.merchant_id=t.merchant_id")
    assert not validate_sql(sql).ok


def test_connection_is_read_only_even_without_validator():
    with pytest.raises(sqlite3.DatabaseError):
        get_db().raw_write_attempt("DELETE FROM transactions")


def test_authorizer_blocks_role_even_if_validator_bypassed():
    with pytest.raises(SQLValidationError):
        get_db().execute("SELECT bank_name FROM banks", role="merchant_analyst", use_cache=False)


def test_authorizer_blocks_schema_discovery_even_if_validator_bypassed():
    with pytest.raises(SQLValidationError):
        get_db().execute("SELECT name FROM sqlite_master", use_cache=False)


def test_query_timeout_cancels_runaway_query():
    """Engine-level timeout (validator deliberately bypassed): a 3-way cross join must be cancelled, not run to the end."""
    from utils.db import QueryTimeout, ReadOnlyDB
    db = ReadOnlyDB(timeout_s=0.2)
    with pytest.raises(QueryTimeout):
        db.execute("SELECT COUNT(*) FROM transactions a, transactions b, transactions c", use_cache=False)


def test_deep_subquery_nesting_blocked():
    sql = ("SELECT transaction_id FROM (SELECT transaction_id FROM (SELECT transaction_id FROM "
           "(SELECT transaction_id FROM transactions) a) b) c")
    v = validate_sql(sql)
    assert not v.ok and any("nesting" in e.lower() for e in v.errors)


def test_safe_query_executes_with_params():
    r = get_db().safe_query("SELECT COUNT(*) AS n FROM transactions WHERE payment_method = ?", ["UPI"])
    assert r.df.iloc[0, 0] > 5000


def test_safe_query_raises_on_unsafe():
    with pytest.raises(SQLValidationError):
        get_db().safe_query("DROP TABLE banks")
