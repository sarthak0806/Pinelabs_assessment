"""Headless UI smoke test: every example question renders without exceptions (mock mode)."""
import pytest
from streamlit.testing.v1 import AppTest

import config

QUESTIONS = [
    ("Why did our overall success rate fall from Sept to Oct?", "analyst", "Answer"),
    ("Did our merchant mix shift from July to August?", "analyst", "Answer"),
    ("Why did UPI success drop?", "analyst", "Answer"),
    ("Show me success rates by bank", "analyst", "Answer"),
    ("Do the gateway and settlement sources disagree in October?", "analyst", "Answer"),
    ("Show the monthly success rate trend", "analyst", "Answer"),
    ("Total transactions in Aug 2026?", "analyst", "Answer"),
    ("Who are the problematic banks?", "analyst", "clarification"),
    ("Show me success rates by bank", "merchant_analyst", "Access denied"),
]


@pytest.mark.parametrize("mode", ["claude", "kimi", "openrouter"])
def test_provider_without_key_asks_for_one(monkeypatch, mode):
    for k in ("ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setattr(config, k, "")
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=120)
    at.run()
    at.radio[0].set_value(mode).run()
    assert not at.exception
    assert any("API key to enable" in str(i.value) for i in at.info)
    assert any("Offline" in m.value for m in at.markdown)          # header badge still offline


def test_provider_with_ui_key_is_enabled(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=120)
    at.run()
    at.radio[0].set_value("openrouter").run()
    at.text_input(key="apikey_openrouter").input("sk-or-test").run()
    assert not at.exception
    assert any("Enabled with your key" in str(c.value) for c in at.caption)
    assert all("sk-or-test" not in m.value for m in at.markdown)   # key never rendered


def test_shared_openrouter_needs_no_key_and_locks_model(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-app-key")       # as on the Modal deployment
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "MOONSHOT_API_KEY", "")
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=120)
    at.run()
    at.radio[0].set_value("openrouter").run()
    assert not at.exception
    keys = [t.key for t in at.text_input]
    assert "apikey_openrouter" not in keys and "model_openrouter" not in keys   # nothing to type, model locked
    assert any("free, no key needed" in str(c.value) for c in at.caption)
    assert all("sk-or-app-key" not in m.value for m in at.markdown)
    # Claude / Kimi still ask the visitor for their own key
    for mode in ("claude", "kimi"):
        at.radio[0].set_value(mode).run()
        assert f"apikey_{mode}" in [t.key for t in at.text_input]
        assert any("API key to enable" in str(i.value) for i in at.info)


@pytest.mark.parametrize("question,role,expect", QUESTIONS)
def test_app_renders(question, role, expect):
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception
    if role != "analyst":
        at.selectbox[0].select(role).run()
    at.session_state["pending"] = question
    at.run()
    assert not at.exception, at.exception
    banners = " ".join(str(w.value) for w in at.warning)
    assert "deprecated" not in banners.lower(), banners
    html = " ".join(m.value for m in at.markdown)
    assert expect.lower() in html.lower()
