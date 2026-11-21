"""SQL validation & safety (layer 1 of defense-in-depth).

Every SQL string - LLM-drafted or template - passes through `validate_sql` before execution:
  * exactly one statement, no stacked statements (';'), no comments
  * SELECT only (no DDL/DML, PRAGMA, ATTACH, UNION/INTERSECT/EXCEPT)
  * only allowlisted tables/columns; role-based table/column restrictions
  * dangerous functions blocked (load_extension, readfile, ...)
  * cost guard: no cartesian joins, join-count and subquery-depth limits
  * forced LIMIT (row cap)
  * placeholder count must match bound params (user values are never interpolated)
Layer 2 is the read-only SQLite connection + authorizer in utils/db.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import sqlglot
from sqlglot import exp

import config
from analytics.catalog import ALLOWED_COLUMNS, ALLOWED_TABLES

FORBIDDEN_NODE_NAMES = ["Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Pragma", "Attach",
                        "Detach", "Union", "Intersect", "Except", "Merge", "TruncateTable", "Transaction",
                        "Commit", "Rollback", "Set", "Into", "Copy", "Use"]
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in FORBIDDEN_NODE_NAMES if hasattr(exp, n))
FORBIDDEN_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "randomblob",
                       "zeroblob", "sqlite_compileoption_get", "sqlite_offset"}


class SQLValidationError(Exception):
    pass


@dataclass
class ValidationResult:
    ok: bool
    sql: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    access_denied: bool = False

    def raise_if_invalid(self):
        if not self.ok:
            raise SQLValidationError("; ".join(self.errors))


def _subquery_depth(node: exp.Expression) -> int:
    depth = 0
    for sel in node.find_all(exp.Select):
        d, p = 0, sel.parent
        while p is not None:
            if isinstance(p, exp.Select):
                d += 1
            p = p.parent
        depth = max(depth, d)
    return depth


def validate_sql(sql: str, params: Sequence = (), role: str = config.DEFAULT_ROLE,
                 max_rows: Optional[int] = None) -> ValidationResult:
    max_rows = max_rows or config.MAX_RESULT_ROWS
    res = ValidationResult(ok=False)
    if not sql or not sql.strip():
        res.errors.append("Empty SQL.")
        return res
    raw = sql.strip().rstrip(";").strip()
    if ";" in raw:
        res.errors.append("Multiple statements / semicolons are not allowed.")
    if "--" in raw or "/*" in raw:
        res.errors.append("SQL comments are not allowed.")
    if res.errors:
        return res

    try:
        trees = sqlglot.parse(raw, read="sqlite")
    except Exception as e:  # sqlglot ParseError / TokenError
        res.errors.append(f"SQL does not parse: {str(e).splitlines()[0][:200]}")
        return res
    trees = [t for t in trees if t is not None]
    if len(trees) != 1:
        res.errors.append("Exactly one statement is allowed.")
        return res
    tree = trees[0]

    if not isinstance(tree, exp.Select):
        res.errors.append(f"Only SELECT queries are allowed (got {type(tree).__name__.upper()}).")
        return res
    bad = [type(n).__name__.upper() for n in tree.find_all(*FORBIDDEN_NODES)] if FORBIDDEN_NODES else []
    if bad:
        res.errors.append(f"Forbidden operation(s): {sorted(set(bad))}.")

    # --- functions -----------------------------------------------------------
    for fn in tree.find_all(exp.Anonymous):
        if str(fn.name).lower() in FORBIDDEN_FUNCTIONS:
            res.errors.append(f"Function '{fn.name}' is not allowed.")

    # --- tables / columns allowlist + RBAC ------------------------------------
    role_cfg = config.ROLES.get(role, config.ROLES[config.DEFAULT_ROLE])
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = sorted({t.name.lower() for t in tree.find_all(exp.Table)} - cte_names)
    res.tables = tables
    for t in tables:
        if t not in ALLOWED_TABLES:
            res.errors.append(f"Table '{t}' is not in the allowlist {sorted(ALLOWED_TABLES)}.")
        elif t in role_cfg["denied_tables"]:
            res.errors.append(f"Access denied: role '{role}' may not query table '{t}'.")
            res.access_denied = True

    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    aliases |= {c.name.lower() for cte in tree.find_all(exp.CTE) for c in cte.find_all(exp.Identifier)}
    cols = sorted({c.name.lower() for c in tree.find_all(exp.Column) if c.name})
    res.columns = cols
    for c in cols:
        if c not in ALLOWED_COLUMNS and c not in aliases:
            res.errors.append(f"Column '{c}' is not in the allowlist.")
        elif c in role_cfg["denied_columns"]:
            res.errors.append(f"Access denied: role '{role}' may not query column '{c}'.")
            res.access_denied = True

    # --- cost guard -----------------------------------------------------------
    joins = list(tree.find_all(exp.Join))
    for j in joins:
        if j.args.get("on") is None and not j.args.get("using"):
            res.errors.append("Cartesian join (join without ON/USING condition) blocked by cost guard.")
            break
    if len(joins) > config.MAX_JOINS:
        res.errors.append(f"Too many joins ({len(joins)} > {config.MAX_JOINS}) - blocked by cost guard.")
    if _subquery_depth(tree) > config.MAX_SUBQUERY_DEPTH:
        res.errors.append(f"Subquery nesting deeper than {config.MAX_SUBQUERY_DEPTH} - blocked by cost guard.")

    # --- parameter binding ----------------------------------------------------
    n_placeholders = len(list(tree.find_all(exp.Placeholder)))
    if n_placeholders != len(params):
        res.errors.append(f"Placeholder count ({n_placeholders}) does not match bound params ({len(params)}).")

    if res.errors:
        return res

    # --- forced LIMIT ---------------------------------------------------------
    limit = tree.args.get("limit")
    current = None
    if limit is not None:
        try:
            current = int(limit.expression.this)
        except Exception:
            current = None
    if current is None or current > max_rows:
        tree = tree.limit(max_rows, copy=True)
        res.warnings.append(f"Row cap applied: LIMIT {max_rows}.")

    res.sql = tree.sql(dialect="sqlite", pretty=True)
    res.ok = True
    return res
