"""Provider tests with a stubbed Anthropic client (no network, no quota)."""
from types import SimpleNamespace

import pytest

import config
from utils import llm


class StubMessages:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.resp


def _provider(monkeypatch, tmp_path, resp=None, exc=None):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(config, "LLM_USAGE_PATH", tmp_path / "usage.json")
    p = llm.ClaudeProvider()
    p.client = SimpleNamespace(messages=StubMessages(resp, exc))
    return p


def _resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="thinking", thinking=""),
                                                       SimpleNamespace(type="text", text=text)])


def test_claude_returns_text_and_uses_haiku_low_effort(monkeypatch, tmp_path):
    p = _provider(monkeypatch, tmp_path, _resp('{"ok": true}'))
    out = p.complete([{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}], json_mode=True)
    assert out == '{"ok": true}'
    kw = p.client.messages.calls[0]
    assert kw["model"] == config.CLAUDE_MODEL            # whatever .env configures (default claude-haiku-5-5)
    assert kw["output_config"] == {"effort": config.CLAUDE_EFFORT}
    assert "JSON object" in kw["system"] and kw["messages"] == [{"role": "user", "content": "q"}]


def test_claude_response_is_cached(monkeypatch, tmp_path):
    p = _provider(monkeypatch, tmp_path, _resp("hello"))
    msgs = [{"role": "user", "content": "same question"}]
    p.complete(msgs); p.complete(msgs)
    assert len(p.client.messages.calls) == 1


def test_claude_refusal_raises_unavailable(monkeypatch, tmp_path):
    p = _provider(monkeypatch, tmp_path, _resp("", stop="refusal"))
    with pytest.raises(llm.LLMUnavailable):
        p.complete([{"role": "user", "content": "q"}])


class StubCompletions:
    def __init__(self, text):
        self.text, self.calls = text, []

    def create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self.text)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_openrouter_defaults_reasoning_and_ui_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "LLM_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(config, "LLM_USAGE_PATH", tmp_path / "usage.json")
    with pytest.raises(llm.LLMUnavailable):
        llm.build_provider("openrouter")                      # no key anywhere -> not enabled
    p = llm.build_provider("openrouter", api_key="sk-or-ui-key")  # key typed in the UI
    assert p.model == "nvidia/nemotron-3.5-super-vl:free"
    assert str(p.client.base_url).startswith("https://openrouter.ai/api/v1")
    stub = StubCompletions('{"x": 1}')
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    assert p.complete([{"role": "user", "content": "q"}], json_mode=True) == '{"x": 1}'
    kw = stub.calls[0]
    assert kw["extra_body"] == {"reasoning": {"enabled": True}}
    assert kw["model"] == "nvidia/nemotron-3.5-super-vl:free"
    # the key must never end up in the on-disk cache or usage files
    assert "sk-or-ui-key" not in (tmp_path / "cache.json").read_text()
    assert "sk-or-ui-key" not in (tmp_path / "usage.json").read_text()


def test_openrouter_empty_reasoning_only_reply_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(config, "LLM_USAGE_PATH", tmp_path / "usage.json")
    p = llm.build_provider("openrouter", api_key="k")
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=StubCompletions(None)))
    with pytest.raises(llm.LLMUnavailable):
        p.complete([{"role": "user", "content": "q"}])


def test_ui_model_override(monkeypatch):
    p = llm.build_provider("claude", api_key="k", model="claude-sonnet-5-5")
    assert p.model == "claude-sonnet-5-5"


def test_missing_key_falls_back_to_mock(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    prov, note = llm.get_provider("claude")
    assert prov.name == "mock" and "ANTHROPIC_API_KEY" in note
