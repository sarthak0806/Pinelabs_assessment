"""Deploy PayInsight Analytics (Streamlit) to Modal.

See DEPLOY.md. In short (with .venv-deploy, Python 3.10+, activated):
    modal setup                                                           # one-time login
    modal secret create payinsight-secrets OPENROUTER_API_KEY=sk-or-...   # only the OpenRouter key
    modal serve modal_app.py     # temporary dev URL with hot reload (Ctrl+C to stop)
    modal deploy modal_app.py    # persistent public URL

Design notes
* The SQLite DB (data/payinsight.db, synthetic) ships inside the image and is opened read-only.
* Secrets come from the Modal secret "payinsight-secrets" as env vars; the local .env is never uploaded.
* max_containers=1 keeps every Streamlit websocket session on the same container (session state is in memory).
* Scales to zero when idle (scaledown_window), so it only consumes credits while someone is using it.
"""
import shlex
import subprocess
from pathlib import Path

import modal

LOCAL_DIR = Path(__file__).resolve().parent
REMOTE_DIR = "/root/app"
PORT = 8000

# Never ship secrets, the local venv, logs/caches or tests into the image.
IGNORE = [".env", ".venv", ".venv-deploy", "logs", "**/__pycache__", "**/*.pyc", ".pytest_cache", "tests", "*.bat", ".git"]

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements(str(LOCAL_DIR / "requirements.txt"))
    .env({"LLM_MODE": "openrouter", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false", "PYTHONUNBUFFERED": "1"})
    .add_local_dir(LOCAL_DIR, REMOTE_DIR, ignore=IGNORE)   # must be the last image step
)

app = modal.App("payinsight-analytics", image=image)


@app.function(
    secrets=[modal.Secret.from_name("payinsight-secrets")],
    cpu=1.0,
    memory=1024,
    max_containers=1,          # Streamlit sessions are stateful -> single container
    scaledown_window=600,      # stay warm 10 min after the last request, then scale to zero
    timeout=3600,
)
@modal.concurrent(max_inputs=100)
@modal.web_server(PORT, startup_timeout=120)
def ui():
    cmd = (
        f"streamlit run {REMOTE_DIR}/app.py --server.port {PORT} --server.address 0.0.0.0 "
        "--server.headless true --server.enableCORS false --server.enableXsrfProtection false"
    )
    subprocess.Popen(shlex.split(cmd), cwd=REMOTE_DIR)
