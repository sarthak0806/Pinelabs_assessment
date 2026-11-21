import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LLM_MODE"] = "mock"   # tests never spend LLM quota

import config  # noqa: E402

config.LLM_MODE = "mock"

if not config.DB_PATH.exists():
    from data import seed_data
    seed_data.main()
