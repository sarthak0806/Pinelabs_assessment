"""Central configuration. All secrets come from environment variables / .env (never hardcoded)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# --- Data -------------------------------------------------------------------
DB_PATH = Path(os.getenv("PAYINSIGHT_DB", str(ROOT / "data" / "payinsight.db")))
MANIFEST_PATH = ROOT / "data" / "defects_manifest.json"
LOG_DIR = ROOT / "logs"
AUDIT_LOG_PATH = LOG_DIR / "audit.jsonl"
LLM_CACHE_PATH = LOG_DIR / "llm_cache.json"
LLM_USAGE_PATH = LOG_DIR / "llm_usage.json"

# --- Query guard rails ------------------------------------------------------
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "1000"))        # LLM / user-facing queries
MAX_EVIDENCE_ROWS = int(os.getenv("MAX_EVIDENCE_ROWS", "50000"))   # internal row-level evidence queries
QUERY_TIMEOUT_S = float(os.getenv("QUERY_TIMEOUT_S", "5"))
MAX_JOINS = 4
MAX_SUBQUERY_DEPTH = 2

# --- Analytics --------------------------------------------------------------
LOW_SAMPLE_N = 30        # segments below this are flagged "low confidence"
VERY_LOW_SAMPLE_N = 10   # below this: "very low confidence"
DATA_YEAR = 2026         # synthetic data covers May–Oct 2026
LATEST_MONTH = "2026-10"

# --- LLM --------------------------------------------------------------------
# Default provider shown in the sidebar: "openrouter" | "claude" | "kimi" | "mock" (offline)
LLM_MODE = os.getenv("LLM_MODE", "openrouter").strip().lower()
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "30"))
LLM_DAILY_CAP = int(os.getenv("LLM_DAILY_CAP", "200"))      # calls per provider per day

# OpenRouter - default; the app's key is shared with visitors, model locked to the free one
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free")
OPENROUTER_REASONING = os.getenv("OPENROUTER_REASONING", "true").strip().lower() in ("1", "true", "yes", "on")

# Claude (Anthropic) - optional; visitors supply their own key in the UI
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")       # smallest / cheapest current Claude model
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "low")                  # low | medium | high (low = fastest)

# Kimi (Moonshot) - optional; visitors supply their own key in the UI
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY", "")
MOONSHOT_BASE_URL = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-k3")

# --- Roles (simulated RBAC; no real auth) -----------------------------------
ROLES = {
    "analyst": {
        "label": "Payments Analyst (full access)",
        "denied_tables": set(),
        "denied_columns": set(),
    },
    "merchant_analyst": {
        "label": "Merchant Analyst (no bank / settlement data)",
        "denied_tables": {"banks", "settlements"},
        "denied_columns": {"bank_id"},
    },
}
DEFAULT_ROLE = "analyst"
