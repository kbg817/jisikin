from types import SimpleNamespace

import pytest

from jisikin import drafter
from jisikin.drafter import DraftError, build_prompt, generate_draft

QUESTION = {"title": "팔 오돌토돌 모공각화증인가요", "body": "여름에도 팔이 까끌해요", "categories": ["모공각화증"]}


def test_prompt_contains_guides(example_cfg):
    product = example_cfg.product("daksaren")
    system, user = build_prompt(example_cfg, product, QUESTION)
    assert "닥사렌 모각크림" in system and "피부과 진료" in system and "완치" not in system
    assert "공정위" not in system and "링크(URL)" in system and "larenkorea" not in system  # 운영자 소개·링크 넣지 않음
    from jisikin.drafter import build_social_prompt
    social, _ = build_social_prompt(example_cfg, product, {"platform": "youtube", "title": "t", "body": "b"})
    assert "운영하는 사람인데요\" 같은" in social and "larenkorea" not in social
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
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"] == {"effort": "low"}
    assert "thinking" not in kw
    # 제품마다 같은 system 을 캐시해서, 이어서 만드는 초안은 싸게 읽는다
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and "닥사렌" in kw["system"][0]["text"]


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


def _usage(inp=0, out=0, write=0, read=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=write, cache_read_input_tokens=read)


def test_usage_cost_and_monthly_record():
    from datetime import datetime

    from jisikin.drafter import monthly_usage, record_usage, usage_cost
    from jisikin.storage import KST, Store

    # Sonnet 5.5: 입력 $2, 출력 $10, 캐시 읽기 $0.20, 캐시 쓰기 $2.5 (100만 토큰당)
    assert usage_cost("claude-sonnet-5-5", _usage(1_000_000, 0)) == pytest.approx(2.0)
    assert usage_cost("claude-sonnet-5-5", _usage(0, 1_000_000)) == pytest.approx(10.0)
    assert usage_cost("claude-sonnet-5-5", _usage(0, 0, 1_000_000, 1_000_000)) == pytest.approx(2.5 + 0.2)
    assert usage_cost("claude-opus-5-5", _usage(1000, 1000)) == pytest.approx(0.024)
    assert usage_cost("unknown-model", _usage(1000, 1000)) is None

    store = Store(":memory:")
    sep = datetime(2026, 9, 29, 12, tzinfo=KST)
    record_usage(store, "draft", "claude-sonnet-5-5", _usage(2000, 1000), now=sep)
    record_usage(store, "draft", "claude-sonnet-5-5", _usage(100, 1000, read=1900), now=sep)
    record_usage(store, "keywords", "claude-sonnet-5-5", _usage(300, 500), now=sep)
    record_usage(store, "draft", "claude-sonnet-5-5", _usage(2000, 1000), now=datetime(2026, 10, 1, tzinfo=KST))
    m = monthly_usage(store, "2026-09")
    assert m["calls"] == 3 and m["kinds"]["draft"]["calls"] == 2 and m["kinds"]["draft"]["cache_read"] == 1900
    assert m["cost"] == pytest.approx((2000 * 2 + 1000 * 10 + 100 * 2 + 1900 * 0.2 + 1000 * 10 + 300 * 2 + 500 * 10) / 1e6, abs=1e-4)
    assert monthly_usage(store, "2026-10")["calls"] == 1
    assert monthly_usage(store, "2026-08") == {"kinds": {}, "calls": 0, "cost": 0}


def test_draft_uses_starred_examples_and_records_usage(example_cfg, monkeypatch):
    from jisikin.drafter import MAX_EXAMPLES, monthly_usage
    from jisikin.storage import Store, now_kst

    store = Store(":memory:")
    ids = [store.add_example("daksaren", f"예시 답변 {i} 입니다. 보습을 꾸준히 하세요.", title=f"질문 {i}") for i in range(MAX_EXAMPLES + 2)]
    store.set_example_star(ids[0], False)
    store.add_example("sinui", "다른 제품 예시 답변입니다. 참고만 하세요.")
    resp = SimpleNamespace(
        stop_reason="end_turn", model="claude-sonnet-5-5", usage=_usage(500, 800, read=1500),
        content=[SimpleNamespace(type="text", text="초안")],
    )
    messages = _install_fake(monkeypatch, resp)
    generate_draft(example_cfg, example_cfg.product("daksaren"), QUESTION, store=store)
    system = messages.kwargs["system"][0]["text"]
    assert "실제 답변 예시" in system and "가장 중요한 기준" in system and "관계없는 이야기는 덧붙이지 않습니다" in system
    user = messages.kwargs["messages"][0]["content"]
    assert user.rstrip().endswith("예시와 관계없는 이야기는 넣지 마세요.")  # 질문 바로 뒤에 한 번 더
    used = [i for i in range(MAX_EXAMPLES + 2) if f"예시 답변 {i} " in system]
    assert len(used) == MAX_EXAMPLES and 0 not in used  # ⭐ 뺀 것은 제외, 최근 ⭐ MAX_EXAMPLES 개
    assert used == sorted(used) and "다른 제품" not in system  # 순서 고정 (캐시 적중)
    usage = monthly_usage(store, f"{now_kst():%Y-%m}")
    assert usage["kinds"]["draft"]["calls"] == 1 and usage["cost"] > 0

    # 예시가 없으면 예시 문단도 없음
    system_plain, user_plain = build_prompt(example_cfg, example_cfg.product("eumpa"), QUESTION, [])
    assert "실제 답변 예시" not in system_plain and "실제 답변 예시" not in user_plain
