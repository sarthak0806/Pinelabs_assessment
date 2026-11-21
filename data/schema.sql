-- PayInsight Analytics — SYNTHETIC payments schema (not real Pine Labs data)
-- NOTE: transaction_id is the intended primary key but is deliberately NOT enforced,
-- so the seed script can plant duplicate IDs for data-quality testing.

DROP TABLE IF EXISTS settlements;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS merchants;
DROP TABLE IF EXISTS banks;

CREATE TABLE banks (
    bank_id     TEXT PRIMARY KEY,
    bank_name   TEXT NOT NULL,
    bank_type   TEXT NOT NULL            -- Domestic | International
);

CREATE TABLE merchants (
    merchant_id   TEXT PRIMARY KEY,
    merchant_name TEXT NOT NULL,
    category      TEXT,                  -- Retail | Food | Travel | Utilities | Electronics (+ planted bad values)
    region        TEXT                   -- North | South | East | West
);

CREATE TABLE transactions (
    transaction_id  TEXT,                -- intended PK (duplicates planted)
    txn_timestamp   TEXT,                -- ISO 'YYYY-MM-DD HH:MM:SS' (a few NULLs planted)
    amount          REAL,
    currency        TEXT,                -- always INR
    merchant_id     TEXT,                -- FK -> merchants (NULLs / orphans planted)
    bank_id         TEXT,                -- FK -> banks (NULLs planted)
    payment_method  TEXT,                -- UPI | Card | Wallet | NetBanking (+1 invalid)
    status          TEXT,                -- SUCCESS | FAILED
    failure_reason  TEXT                 -- NULL when SUCCESS (inconsistent spellings planted)
);

-- Separate "settlement system" feed: second source for the same success metric.
CREATE TABLE settlements (
    settlement_id     TEXT PRIMARY KEY,
    transaction_id    TEXT,              -- FK -> transactions
    settlement_date   TEXT,              -- 'YYYY-MM-DD'
    settled_amount    REAL,              -- amount net of MDR; NULL when PENDING
    settlement_status TEXT               -- SETTLED | PENDING
);

CREATE INDEX idx_txn_ts       ON transactions (txn_timestamp);
CREATE INDEX idx_txn_merchant ON transactions (merchant_id);
CREATE INDEX idx_txn_bank     ON transactions (bank_id);
CREATE INDEX idx_set_txn      ON settlements (transaction_id);
