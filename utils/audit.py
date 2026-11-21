"""Append-only audit trail (JSONL): question, role, SQL, params, timing, outcome. No secrets."""
from __future__ import annotations

import datetime as dt
import json
from typing import List

import config


def log(entry: dict) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **entry}
    with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read(limit: int = 200) -> List[dict]:
    if not config.AUDIT_LOG_PATH.exists():
        return []
    lines = config.AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out
