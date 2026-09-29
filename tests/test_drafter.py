from types import SimpleNamespace

import pytest

from jisikin import drafter
from jisikin.drafter import DraftError, build_prompt, generate_draft

QUESTION = {"title": "팔 오돌토돌 모공각화증인가요", "body": "여름에도 팔이 까끌해요", "categories": ["모공각화증"]}


def test_prompt_contains_guides(example_cfg):
    product = example_cfg.product("daksaren")
    system, user = build_prompt(example_cfg, product, QUESTION)
    assert "닥사렌 모각크림" in system and "치료" in system
    assert "공정위" in system  # 공통 가이드
    assert "팔 오돌토돌" in user and "모공각화증" in user


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _install_fake(monkeypatch, response):
    import anthropic

    messages = FakeMessages(response)
    fake_client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    return messages


def test_generate_draft_uses_fallbacks_and_effort(example_cfg, monkeypatch):
    resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=" 초안입니다 ")])
    messages = _install_fake(monkeypatch, resp)
    text = generate_draft(example_cfg, example_cfg.product("daksaren"), QUESTION)
    assert text == "초안입니다"
    kw = messages.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"] == {"effort": "medium"}
    assert "thinking" not in kw


def test_generate_draft_refusal(example_cfg, monkeypatch):
    _install_fake(monkeypatch, SimpleNamespace(stop_reason="refusal", content=[]))
    with pytest.raises(DraftError):
        generate_draft(example_cfg, example_cfg.product("sinui"), QUESTION)


def test_haiku_skips_unsupported_params(example_cfg, monkeypatch):
    example_cfg.ai.model = "claude-haiku-4-5"
    resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok")])
    messages = _install_fake(monkeypatch, resp)
    generate_draft(example_cfg, example_cfg.product("sinui"), QUESTION)
    assert "fallbacks" not in messages.kwargs and "output_config" not in messages.kwargs


def test_ai_status_without_key():
    ok, reason = drafter.ai_status()
    assert not ok and "ANTHROPIC_API_KEY" in reason
