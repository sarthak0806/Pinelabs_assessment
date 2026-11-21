"""LLM interface abstraction.

Providers (all optional - the app is fully functional offline):
* ClaudeProvider      - Anthropic Claude via the official `anthropic` SDK (default claude-haiku-5-5, effort low)
* KimiProvider        - Moonshot official API (OpenAI-compatible)
* OpenRouterProvider  - OpenRouter (OpenAI-compatible), default nvidia/nemotron-3.5-super-vl:free with reasoning on
* MockProvider        - offline; returns None so callers use deterministic rules/templates

API keys come from the UI (session only) or from .env - they are never logged, cached or written to disk.
The LLM is used ONLY for language tasks (intent + SQL drafting, wording the summary).
Guards: response cache, per-provider daily cap, timeout, limited retry, automatic fallback to mock.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from typing import List, Optional

import config


class LLMUnavailable(Exception):
    pass


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output (tolerates ``` fences / leading prose)."""
    if not text:
        raise ValueError("empty response")
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, flags=re.S)
        if not m:
            raise ValueError("no JSON object in response")
        return json.loads(m.group(0))


class MockProvider:
    name = "mock"
    label = "Offline (rules)"
    model = "rules"
    available = True

    def complete(self, messages: List[dict], json_mode: bool = False) -> Optional[str]:
        return None   # signals "use deterministic path"

    def usage_today(self) -> int:
        return 0


class _BaseProvider:
    """Shared cache + per-provider daily-cap bookkeeping."""
    name = "base"
    label = "LLM"
    model = ""

    def _usage_key(self):
        return f"{dt.date.today().isoformat()}:{self.name}"

    def usage_today(self) -> int:
        return int(_load(config.LLM_USAGE_PATH, {}).get(self._usage_key(), 0))

    def _bump_usage(self):
        u = _load(config.LLM_USAGE_PATH, {})
        u[self._usage_key()] = int(u.get(self._usage_key(), 0)) + 1
        _save(config.LLM_USAGE_PATH, u)

    def _cache_key(self, messages, json_mode):
        # NB: the API key is deliberately NOT part of the key and never stored.
        return hashlib.sha256(json.dumps([self.name, self.model, messages, json_mode], sort_keys=True).encode()).hexdigest()

    def complete(self, messages: List[dict], json_mode: bool = False) -> Optional[str]:
        cache = _load(config.LLM_CACHE_PATH, {})
        key = self._cache_key(messages, json_mode)
        if key in cache:
            return cache[key]
        if self.usage_today() >= config.LLM_DAILY_CAP:
            raise LLMUnavailable(f"Daily cap reached for {self.label} ({config.LLM_DAILY_CAP} calls)")
        text = self._call(messages, json_mode)
        cache[key] = text
        _save(config.LLM_CACHE_PATH, cache)
        return text

    def _call(self, messages, json_mode) -> str:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def ping(self) -> str:
        """Cheap connectivity/key check used by the UI 'Test' button (bypasses the cache)."""
        return self._call([{"role": "user", "content": "Reply with the single word OK."}], False)[:40]


# --------------------------------------------------------------------------- OpenAI-compatible (Kimi, OpenRouter)
class _OpenAICompatProvider(_BaseProvider):
    base_url = ""
    extra_body: dict = {}
    default_headers: dict = {}

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise LLMUnavailable(f"No API key for {self.label}")
        if not model:
            raise LLMUnavailable(f"No model set for {self.label}")
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=config.LLM_TIMEOUT_S,
                             max_retries=0, default_headers=self.default_headers or None)
        self._json_mode_supported = True

    def _call(self, messages, json_mode) -> str:
        last_err = None
        for _ in range(2):                                   # at most one retry
            try:
                kwargs = dict(model=self.model, messages=messages, max_tokens=8000)
                if json_mode and self._json_mode_supported:
                    kwargs["response_format"] = {"type": "json_object"}
                if self.extra_body:
                    kwargs["extra_body"] = self.extra_body
                self._bump_usage()
                resp = self.client.chat.completions.create(**kwargs)
                text = (resp.choices[0].message.content or "").strip()
                if not text:
                    raise LLMUnavailable("empty completion (model may have spent its budget on reasoning)")
                return text
            except LLMUnavailable as e:
                last_err = e
                break
            except Exception as e:  # network, auth, 429, bad request ...
                last_err = e
                msg = str(e).lower()
                if "response_format" in msg or ("json" in msg and "support" in msg):
                    self._json_mode_supported = False
                    continue
                if any(s in msg for s in ("401", "403", "404", "invalid api key", "authentication", "permission",
                                          "not found", "no endpoints")):
                    break
                if "timeout" in type(e).__name__.lower() or "timed out" in msg:
                    break
                if "429" in msg or "rate limit" in msg:
                    time.sleep(3)
        raise LLMUnavailable(f"{self.label} call failed: {type(last_err).__name__}: {str(last_err)[:160]}")


class KimiProvider(_OpenAICompatProvider):
    name = "kimi"
    label = "Kimi (Moonshot)"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.base_url = config.MOONSHOT_BASE_URL
        super().__init__(api_key or config.MOONSHOT_API_KEY, model or config.KIMI_MODEL)


class OpenRouterProvider(_OpenAICompatProvider):
    """OpenRouter. Reasoning is enabled via the `reasoning` request field (OpenRouter extension).
    Each call here is single-turn, so `reasoning_details` never needs to be passed back."""
    name = "openrouter"
    label = "OpenRouter"
    default_headers = {"X-Title": "PayInsight Analytics"}

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.base_url = config.OPENROUTER_BASE_URL
        self.extra_body = {"reasoning": {"enabled": config.OPENROUTER_REASONING}}
        super().__init__(api_key or config.OPENROUTER_API_KEY, model or config.OPENROUTER_MODEL)


# --------------------------------------------------------------------------- Claude
class ClaudeProvider(_BaseProvider):
    """Anthropic Claude via the official `anthropic` SDK. Default model claude-haiku-5-5 (smallest/cheapest current
    Claude) at effort "low" for speed."""
    name = "claude"
    label = "Claude (Anthropic)"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        api_key = api_key or config.ANTHROPIC_API_KEY
        if not api_key:
            raise LLMUnavailable("No API key for Claude (ANTHROPIC_API_KEY)")
        import anthropic
        self._anthropic = anthropic
        self.model = model or config.CLAUDE_MODEL
        # SDK retries 429 / 5xx / connection errors itself (with backoff); 1 retry keeps the demo snappy.
        self.client = anthropic.Anthropic(api_key=api_key, timeout=config.LLM_TIMEOUT_S, max_retries=1)

    def _call(self, messages, json_mode) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if json_mode:
            system += "\n\nRespond with a single JSON object only - no prose, no markdown fences."
        convo = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        a = self._anthropic
        self._bump_usage()
        kwargs = dict(model=self.model, max_tokens=8000, messages=convo,
                      output_config={"effort": config.CLAUDE_EFFORT})
        if system.strip():
            kwargs["system"] = system.strip()
        try:
            resp = self.client.messages.create(**kwargs)
        except a.APITimeoutError as e:
            raise LLMUnavailable(f"Claude timed out after {config.LLM_TIMEOUT_S:.0f}s") from e
        except a.AuthenticationError as e:
            raise LLMUnavailable("Claude rejected the API key (401)") from e
        except a.NotFoundError as e:
            raise LLMUnavailable(f"Claude model '{self.model}' not found (404)") from e
        except a.RateLimitError as e:
            raise LLMUnavailable("Claude rate limit (429) after retry") from e
        except a.APIStatusError as e:
            raise LLMUnavailable(f"Claude API error {e.status_code}: {str(e.message)[:120]}") from e
        except a.APIConnectionError as e:
            raise LLMUnavailable("Network error reaching Claude API") from e
        if resp.stop_reason == "refusal":
            raise LLMUnavailable("Claude declined this request (refusal); using deterministic path")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LLMUnavailable(f"empty completion (stop_reason={resp.stop_reason})")
        return text


# --------------------------------------------------------------------------- registry
PROVIDERS = {   # order = order shown in the UI; OpenRouter (Nemotron, free) is the default
    "openrouter": {"cls": OpenRouterProvider, "label": "OpenRouter", "env_key": lambda: config.OPENROUTER_API_KEY,
                   "default_model": lambda: config.OPENROUTER_MODEL, "key_url": "https://openrouter.ai/settings/keys"},
    "claude": {"cls": ClaudeProvider, "label": "Claude (Anthropic)", "env_key": lambda: config.ANTHROPIC_API_KEY,
               "default_model": lambda: config.CLAUDE_MODEL, "key_url": "https://console.anthropic.com/settings/keys"},
    "kimi": {"cls": KimiProvider, "label": "Kimi (Moonshot)", "env_key": lambda: config.MOONSHOT_API_KEY,
             "default_model": lambda: config.KIMI_MODEL, "key_url": "https://platform.moonshot.ai/console/api-keys"},
}


def build_provider(mode: str, api_key: Optional[str] = None, model: Optional[str] = None):
    """Build a provider from UI inputs (falls back to .env values). Raises LLMUnavailable if not configured."""
    spec = PROVIDERS[mode]
    return spec["cls"](api_key=api_key or None, model=model or None)


def get_provider(mode: Optional[str] = None):
    """Returns (provider, note) using .env settings. Falls back to mock with an explanatory note."""
    mode = (mode or config.LLM_MODE).lower()
    if mode in PROVIDERS:
        try:
            return build_provider(mode), PROVIDERS[mode]["label"]
        except LLMUnavailable as e:
            return MockProvider(), f"Mock mode - {e}"
    return MockProvider(), "Mock mode (offline, rules-based)"
