from types import SimpleNamespace

from src.analysis import cursor_agent


def test_sandbox_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CURSOR_SANDBOX_ENABLED", raising=False)
    assert cursor_agent.sandbox_enabled() is True


def test_sandbox_can_be_disabled_for_unsupported_runner(monkeypatch):
    monkeypatch.setenv("CURSOR_SANDBOX_ENABLED", "0")
    assert cursor_agent.sandbox_enabled() is False


def test_call_passes_disabled_sandbox_to_sdk(monkeypatch):
    captured = {}
    monkeypatch.setenv("CURSOR_SANDBOX_ENABLED", "false")

    def fake_prompt(message, options):
        captured["message"] = message
        captured["options"] = options
        return SimpleNamespace(
            status="completed", id="run-1", result='{"ok": true}',
            usage=SimpleNamespace(total_tokens=12),
        )

    monkeypatch.setattr(cursor_agent.Agent, "prompt", fake_prompt)

    text, tokens = cursor_agent.call_llm_cursor_agent(
        "system", "user", api_key="key", model="model",
    )

    assert text == '{"ok": true}'
    assert tokens == 12
    assert captured["options"].local.cwd == cursor_agent.SANDBOX_CWD
    assert captured["options"].local.sandbox_options.enabled is False
    assert "不要使用任何工具" in captured["message"]
