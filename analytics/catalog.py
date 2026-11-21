"""Dataset catalog / semantic layer: tables, columns, measures, dimensions.

Single source of truth for (a) the SQL allowlist, (b) schema context in LLM prompts,
(c) the data dictionary shown in the UI and README.
"""
from __future__ import annotations

from typing import Dict, Iterable, List

CATALOG: Dict[str, dict] = {
    "transactions": {
        "description": "One row per payment attempt (grain: transaction). Source: payment gateway.",
        "source_system": "Payment gateway",
        "columns": {
            "transaction_id": ("TEXT", "Intended unique ID (duplicates possible - checked)"),
            "txn_timestamp": ("TEXT", "Attempt time, ISO 'YYYY-MM-DD HH:MM:SS'"),
            "amount": ("REAL", "Transaction amount in INR"),
            "currency": ("TEXT", "Always INR"),
            "merchant_id": ("TEXT", "FK -> merchants.merchant_id"),
            "bank_id": ("TEXT", "FK -> banks.bank_id (issuing bank / PSP)"),
            "payment_method": ("TEXT", "UPI | Card | Wallet | NetBanking"),
            "status": ("TEXT", "SUCCESS | FAILED"),
            "failure_reason": ("TEXT", "NULL if SUCCESS; Insufficient Funds | Technical Error | Card Expired | Limit Exceeded | Timeout"),
        },
    },
    "merchants": {
        "description": "One row per merchant (grain: merchant). Source: merchant master.",
        "source_system": "Merchant master",
        "columns": {
            "merchant_id": ("TEXT", "Primary key"),
            "merchant_name": ("TEXT", "Synthetic merchant name"),
            "category": ("TEXT", "Retail | Food | Travel | Utilities | Electronics"),
            "region": ("TEXT", "North | South | East | West"),
        },
    },
    "banks": {
        "description": "One row per issuing bank / PSP (grain: bank). Source: bank master.",
        "source_system": "Bank master",
        "columns": {
            "bank_id": ("TEXT", "Primary key"),
            "bank_name": ("TEXT", "Synthetic bank name"),
            "bank_type": ("TEXT", "Domestic | International"),
        },
    },
    "settlements": {
        "description": "One row per settlement record (grain: settlement). Source: settlement system - a second source for 'successful' payments.",
        "source_system": "Settlement system",
        "columns": {
            "settlement_id": ("TEXT", "Primary key"),
            "transaction_id": ("TEXT", "FK -> transactions.transaction_id"),
            "settlement_date": ("TEXT", "'YYYY-MM-DD'"),
            "settled_amount": ("REAL", "Amount net of MDR; NULL when PENDING"),
            "settlement_status": ("TEXT", "SETTLED | PENDING"),
        },
    },
}

RELATIONSHIPS = [
    "transactions.merchant_id -> merchants.merchant_id (many:1)",
    "transactions.bank_id -> banks.bank_id (many:1)",
    "settlements.transaction_id -> transactions.transaction_id (many:1)",
]

MEASURES = {
    "success_rate": "COUNT(status='SUCCESS') / COUNT(*) over de-duplicated transactions",
    "failure_rate": "1 - success_rate",
    "txn_count": "COUNT(*) of de-duplicated transactions",
    "txn_value": "SUM(amount) in INR",
    "settlement_gap": "successful transactions minus SETTLED settlement records",
}

# Dimension key -> (SQL expression, display label, tables needed)
DIMENSIONS = {
    "payment_method": ("t.payment_method", "Payment method", {"transactions"}),
    "bank": ("b.bank_name", "Bank", {"transactions", "banks"}),
    "merchant_category": ("m.category", "Merchant category", {"transactions", "merchants"}),
    "region": ("m.region", "Region", {"transactions", "merchants"}),
    "merchant": ("m.merchant_name", "Merchant", {"transactions", "merchants"}),
    "failure_reason": ("t.failure_reason", "Failure reason", {"transactions"}),
    "bank_type": ("b.bank_type", "Bank type", {"transactions", "banks"}),
}

ALLOWED_TABLES = set(CATALOG)
ALLOWED_COLUMNS = {c for t in CATALOG.values() for c in t["columns"]}


def schema_prompt(tables: Iterable[str] | None = None) -> str:
    """Compact schema text for LLM prompts (only the relevant tables)."""
    tables = list(tables or CATALOG)
    lines: List[str] = []
    for t in tables:
        meta = CATALOG[t]
        cols = ", ".join(f"{c} {typ}" for c, (typ, _) in meta["columns"].items())
        lines.append(f"{t}({cols})  -- {meta['description']}")
    lines.append("Relationships: " + "; ".join(r for r in RELATIONSHIPS if any(t in r.split('->')[0] for t in tables)))
    return "\n".join(lines)


def relevant_tables(question: str) -> List[str]:
    """Simple retrieval: choose only the tables the question needs (keeps prompts small)."""
    q = question.lower()
    tables = ["transactions"]
    if any(w in q for w in ("merchant", "category", "categories", "region", "mix", "segment", "retail",
                            "food", "travel", "utilit", "electronic", "north", "south", "east", "west")):
        tables.append("merchants")
    if "bank" in q or "issuer" in q or "psp" in q:
        tables.append("banks")
    if "settle" in q or "reconcil" in q or "disagree" in q or "mismatch" in q:
        tables.append("settlements")
    return tables
