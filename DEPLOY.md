# Deploying PayInsight Analytics on Modal

**Live:** https://modanwalsarthak0806--payinsight-analytics-ui.us-east.modal.run (workspace `modanwalsarthak0806`, app `payinsight-analytics`)

The app runs as one small Streamlit container (Python 3.11, 1 CPU, 1 GB) and **scales to zero** after 10 idle minutes. Modal's free monthly credits easily cover a demo. Visitors need no login and no API key: the default model is OpenRouter Nemotron (free) on the app's key.

## One-time setup (in `e:\Pinelabs\payinsight-ai`)
The Modal CLI needs Python 3.10 or later. The app's `.venv` is Python 3.9, so deploys use a separate `.venv-deploy`:
```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe" -m venv .venv-deploy
.\.venv-deploy\Scripts\activate
pip install --upgrade modal
modal setup                     # browser login; or: modal token set --token-id ... --token-secret ...
```
Run every `modal ...` command with `.venv-deploy` activated. Modal will drop Python 3.10 in a future client release, so moving `.venv-deploy` to Python 3.11 or later is recommended.

## The secret
The app reads keys from the Modal secret `payinsight-secrets`. The local `.env` is **never uploaded** (`modal_app.py` excludes it).
```powershell
modal secret create payinsight-secrets OPENROUTER_API_KEY=sk-or-v1-... --force
```
- Put **only the OpenRouter key** here. On the shared key the app locks the model to the free Nemotron.
- **Don't add `ANTHROPIC_API_KEY` or `MOONSHOT_API_KEY`.** If they're present, every visitor who picks Claude or Kimi would be using your paid keys. Without them, the app asks each visitor for their own key.
- With no key at all (`modal secret create payinsight-secrets LLM_MODE=mock`), the app runs offline.

## Deploying changes
1. Edit locally and preview with `.\run_app.bat` (http://localhost:8501).
2. Test: `.\.venv\Scripts\python.exe -m pytest -q tests`.
3. Deploy: `.\.venv-deploy\Scripts\activate` then `modal deploy modal_app.py`. It takes 5–40 seconds, and the URL stays the same. Open pages need a refresh.

Optional live preview without touching the public URL: `modal serve modal_app.py` gives a temporary `-dev.modal.run` URL that hot-reloads on save. Ctrl+C stops it.

| Change | Rebuild time |
|---|---|
| Code, CSS or docs | Fast (seconds) |
| `requirements.txt` | Slower (a few minutes, image rebuild) |
| Secret value | Applied on the next container start; redeploy to apply now |

## Operating it
| Task | Command |
|---|---|
| Live logs | `modal app logs payinsight-analytics` |
| Stop / take offline | `modal app stop payinsight-analytics` |
| Roll back | Modal dashboard → payinsight-analytics → Deployments → roll back |
| Rotate the OpenRouter key | `modal secret create payinsight-secrets OPENROUTER_API_KEY=... --force`, then `modal deploy modal_app.py` |

## Design notes
- **Public URL.** The shared OpenRouter key is capped by `LLM_DAILY_CAP` (200 calls/day). To require Modal login instead, set `requires_proxy_auth=True` in `@modal.web_server`.
- **Single container** (`max_containers=1`): Streamlit sessions live in memory, so each visitor must stay on one container.
- **Ephemeral logs:** the audit log and LLM cache reset when the container scales to zero. Persisting them would need a `modal.Volume`.
- **Data:** the synthetic SQLite database ships in the image and is opened read-only.
