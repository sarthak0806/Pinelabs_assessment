"""Read-only database access (layer 2 of defense-in-depth).

* SQLite opened with `mode=ro` URI + `PRAGMA query_only=ON`
* sqlite3 authorizer: only SELECT/READ/FUNCTION actions allowed; role-denied tables blocked
  at the engine level even if the validator were bypassed
* parameterized execution only (`cursor.execute(sql, params)`)
* wall-clock timeout via progress handler
* small LRU result cache keyed on (sql, params, role)
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Sequence

import pandas as pd

import config
from analytics.catalog import ALLOWED_TABLES
from analytics.security import SQLValidationError, validate_sql

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


class QueryTimeout(Exception):
    pass


@dataclass
class QueryResult:
    df: pd.DataFrame
    sql: str
    params: tuple
    elapsed_ms: float
    cached: bool
    warnings: list

    @property
    def row_count(self) -> int:
        return len(self.df)


class ReadOnlyDB:
    def __init__(self, path=config.DB_PATH, timeout_s: float = config.QUERY_TIMEOUT_S, cache_size: int = 128):
        if not path.exists():
            raise FileNotFoundError(f"Database not found at {path}. Run: python data/seed_data.py")
        self.path = path
        self.timeout_s = timeout_s
        self._con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self._con.execute("PRAGMA query_only = ON")
        self._lock = threading.Lock()
        self._cache: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
        self._cache_size = cache_size

    # -- engine-level guard -------------------------------------------------
    def _authorizer_for(self, role: str):
        denied = config.ROLES.get(role, config.ROLES[config.DEFAULT_ROLE])["denied_tables"]

        def auth(action, arg1, arg2, dbname, source):
            if action not in _ALLOWED_ACTIONS:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and arg1 and (arg1.lower() in denied or arg1.lower() not in ALLOWED_TABLES):
                return sqlite3.SQLITE_DENY       # role-denied tables AND anything off the catalog (e.g. sqlite_master)
            return sqlite3.SQLITE_OK
        return auth

    def execute(self, sql: str, params: Sequence = (), role: str = config.DEFAULT_ROLE,
                use_cache: bool = True) -> QueryResult:
        """Execute an ALREADY-VALIDATED SELECT with bound params."""
        key = (sql, tuple(params), role)
        if use_cache and key in self._cache:
            self._cache.move_to_end(key)
            return QueryResult(self._cache[key].copy(), sql, tuple(params), 0.0, True, [])
        with self._lock:
            deadline = time.perf_counter() + self.timeout_s
            self._con.set_authorizer(self._authorizer_for(role))
            self._con.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 5000)
            t0 = time.perf_counter()
            try:
                cur = self._con.execute(sql, tuple(params))
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
            except sqlite3.OperationalError as e:
                if "interrupted" in str(e).lower():
                    raise QueryTimeout(f"Query exceeded {self.timeout_s}s timeout and was cancelled.") from e
                raise
            except sqlite3.DatabaseError as e:
                if any(w in str(e).lower() for w in ("not authorized", "prohibited")):
                    raise SQLValidationError(f"Blocked by database authorizer: {e}") from e
                raise
            finally:
                self._con.set_progress_handler(None, 0)
                self._con.set_authorizer(None)
            elapsed = (time.perf_counter() - t0) * 1000
        df = pd.DataFrame(rows, columns=cols)
        if use_cache:
            self._cache[key] = df.copy()
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return QueryResult(df, sql, tuple(params), elapsed, False, [])

    def safe_query(self, sql: str, params: Sequence = (), role: str = config.DEFAULT_ROLE,
                   max_rows: Optional[int] = None) -> QueryResult:
        """Validate (layer 1) then execute read-only (layer 2)."""
        v = validate_sql(sql, params, role=role, max_rows=max_rows)
        v.raise_if_invalid()
        res = self.execute(v.sql, params, role=role)
        res.warnings = v.warnings
        return res

    def raw_write_attempt(self, sql: str):
        """Test helper: proves the connection itself refuses writes (bypasses validator)."""
        with self._lock:
            return self._con.execute(sql)


_DB: Optional[ReadOnlyDB] = None


def get_db() -> ReadOnlyDB:
    global _DB
    if _DB is None:
        _DB = ReadOnlyDB()
    return _DB
