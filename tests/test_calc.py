import json
from types import SimpleNamespace

import pytest
import requests

from jisikin import calc, drafter
from jisikin.drafter import DraftError, generate_draft

KEY = "k" * 43
PILLARS = {k: {"ganji": g} for k, g in zip(("year", "month", "day", "hour"), ("庚午", "壬午", "甲辰", "己巳"))}


class FakeSession:
    """명연당 계산 서버 흉내: 받은 요청을 기록하고 정해 둔 응답을 돌려준다."""

    def __init__(self, status=200, payload=None, error=None):
        self.status, self.payload, self.error = status, payload, error
        self.calls = []

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.calls.append(SimpleNamespace(method=method, url=url, json=json, params=params, headers=headers))
        if self.error:
            raise self.error
        payload = self.payload if self.payload is not None else {"success": True, "data": {"pillars": PILLARS}}
        return SimpleNamespace(status_code=self.status, json=lambda: payload)


def test_status_needs_long_key(monkeypatch):
    assert calc.calc_status()[0] is False
    monkeypatch.setenv("MYD_CALC_KEY", "short")
    assert calc.calc_status()[0] is False
    monkeypatch.setenv("MYD_CALC_KEY", KEY)
    ok, msg = calc.calc_status()
    assert ok and "myeongyeondang.com" in msg
    assert len(calc.new_key()) >= 43 and calc.new_key() != calc.new_key()


def test_tools_keep_one_order_and_no_brand():
    names = [t["name"] for t in calc.tools_for(["hanja", "saju", "name"])]
    assert names == ["saju_calculator", "name_evaluator", "hanja_lookup"]
    assert [t["name"] for t in calc.tools_for(["name"])] == ["name_evaluator"]
    # 명운연구소 답변에도 쓰므로 도구 설명에 상호를 넣지 않는다
    assert not any("명연당" in json.dumps(t, ensure_ascii=False) for t in calc.tools_for(list(calc.TOOL_NAMES)))


def test_run_tool_requests(monkeypatch):
    monkeypatch.setenv("MYD_CALC_KEY", KEY)
    monkeypatch.setenv("MYD_CALC_URL", "http://calc.test/")
    s = FakeSession()
    text, is_error = calc.run_tool("saju_calculator", {"year": 1990, "month": 6, "day": 8, "calendar": "solar", "hour": None}, session=s)
    assert not is_error and json.loads(text)["pillars"]["day"]["ganji"] == "甲辰"
    c = s.calls[0]
    assert (c.method, c.url) == ("POST", "http://calc.test/api/v1/internal/calc/saju")
    assert c.json == {"year": 1990, "month": 6, "day": 8, "calendar": "solar"}  # 비운 값은 보내지 않음(시간 모름)
    assert c.headers["Authorization"] == f"Bearer {KEY}"

    calc.run_tool("name_evaluator", {"name": "김민준", "hanja": ["金", "珉", "俊"]}, session=s)
    assert (s.calls[1].url.endswith("/name"), s.calls[1].json) == (True, {"name": "김민준", "hanja": ["金", "珉", "俊"]})
    calc.run_tool("hanja_lookup", {"eum": "민", "limit": 99}, session=s)
    assert s.calls[2].method == "GET" and s.calls[2].params == {"eum": "민", "lastName": "false", "limit": 30}


def test_run_tool_errors(monkeypatch):
    monkeypatch.setenv("MYD_CALC_KEY", KEY)
    text, is_error = calc.run_tool("saju_calculator", {}, session=FakeSession(400, {"success": False, "error": "없는 날짜"}))
    assert is_error and "없는 날짜" in text  # 입력 오류는 AI 에게 돌려준다
    for session in (FakeSession(401, {}), FakeSession(404, {}), FakeSession(500, {}), FakeSession(error=requests.ConnectionError())):
        with pytest.raises(calc.CalcUnavailable):
            calc.run_tool("saju_calculator", {"year": 1990}, session=session)
    monkeypatch.delenv("MYD_CALC_KEY")
    with pytest.raises(calc.CalcUnavailable):
        calc.run_tool("saju_calculator", {"year": 1990}, session=FakeSession())


def test_check_connection(monkeypatch):
    assert calc.check_connection(session=FakeSession())[0] is False  # 열쇠 없음
    monkeypatch.setenv("MYD_CALC_KEY", KEY)
    ok, msg = calc.check_connection(session=FakeSession())
    assert ok and "庚午 壬午 甲辰 己巳" in msg
    assert calc.check_connection(session=FakeSession(401, {}))[0] is False


# ---- 초안: 계산 도구 반복 ----

QUESTION = {"title": "사주 좀 봐주세요", "body": "1990년 6월 8일 오전 10시 30분 여자입니다. 올해 직장운이 궁금해요"}


class ScriptedMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def _install(monkeypatch, responses):
    import anthropic

    messages = ScriptedMessages(responses)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: SimpleNamespace(beta=SimpleNamespace(messages=messages)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    return messages


def _tool_use(name, tool_input, tid="tu_1"):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=tool_input)


def test_saju_draft_calls_calculator(example_cfg, monkeypatch):
    monkeypatch.setenv("MYD_CALC_KEY", KEY)
    ran = []
    monkeypatch.setattr(calc, "run_tool", lambda name, inp: ran.append((name, inp)) or ('{"pillars":{}}', False))
    first = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="thinking"), _tool_use("saju_calculator", {"year": 1990, "month": 6, "day": 8, "hour": 10, "minute": 30, "calendar": "solar", "gender": "female"})])
    final = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=" 갑진(甲辰) 일주시네요 ")])
    messages = _install(monkeypatch, [first, final])

    text = generate_draft(example_cfg, example_cfg.product("myeongyeon"), QUESTION)
    assert text == "갑진(甲辰) 일주시네요"
    assert ran == [("saju_calculator", {"year": 1990, "month": 6, "day": 8, "hour": 10, "minute": 30, "calendar": "solar", "gender": "female"})]
    c1, c2 = messages.calls
    assert [t["name"] for t in c1["tools"]] == ["saju_calculator", "name_evaluator"]
    assert c1["output_config"] == {"effort": "medium"}  # low 설정이어도 계산 초안은 medium
    assert "계산 도구" in c1["system"][0]["text"] and "명연당" in c1["system"][0]["text"]
    assert c1["system"][0]["cache_control"] == {"type": "ephemeral"}
    # 두 번째 요청: 앞 응답(생각 블록 포함)을 그대로 돌려주고 도구 결과를 한 번에
    assert c2["messages"][1] == {"role": "assistant", "content": first.content}
    assert c2["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": '{"pillars":{}}'}]}


def test_naming_draft_has_name_tools_without_other_brand(example_cfg, monkeypatch):
    monkeypatch.setenv("MYD_CALC_KEY", KEY)
    messages = _install(monkeypatch, [SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="초안")])])
    generate_draft(example_cfg, example_cfg.product("myeongun"), {"title": "아기 이름 풀이 부탁드려요", "body": "김민준 金珉俊"})
    c = messages.calls[0]
    assert [t["name"] for t in c["tools"]] == ["saju_calculator", "name_evaluator", "hanja_lookup"]
    assert "명연당" not in c["system"][0]["text"] and "명연당" not in json.dumps(c["tools"], ensure_ascii=False)


def test_without_key_drafts_without_tools(example_cfg, monkeypatch):
    messages = _install(monkeypatch, [SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="초안")])])
    generate_draft(example_cfg, example_cfg.product("myeongyeon"), QUESTION)
    c = messages.calls[0]
    assert "tools" not in c and c["output_config"] == {"effort": "low"}
    assert "짐작해서 쓰지 말고" in c["system"][0]["text"]
    # 계산을 안 쓰는 제품은 예전 그대로
    messages = _install(monkeypatch, [SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="초안")])])
    generate_draft(example_cfg, example_cfg.product("daksaren"), {"title": "건선 크림", "body": ""})
    assert "tools" not in messages.calls[0] and "계산" not in messages.calls[0]["system"][0]["text"]


def test_calc_errors_stop_the_draft(example_cfg, monkeypatch):
    monkeypatch.setenv("MYD_CALC_KEY", KEY)

    def broken(name, inp):
        raise calc.CalcUnavailable("명연당 계산 열쇠가 맞지 않습니다")

    monkeypatch.setattr(calc, "run_tool", broken)
    _install(monkeypatch, [SimpleNamespace(stop_reason="tool_use", content=[_tool_use("saju_calculator", {"year": 1990})])])
    with pytest.raises(DraftError, match="열쇠"):
        generate_draft(example_cfg, example_cfg.product("myeongyeon"), QUESTION)


def test_tool_rounds_are_capped(example_cfg, monkeypatch):
    monkeypatch.setenv("MYD_CALC_KEY", KEY)
    monkeypatch.setattr(calc, "run_tool", lambda name, inp: ("{}", False))
    loop = [SimpleNamespace(stop_reason="tool_use", content=[_tool_use("saju_calculator", {"year": 1990}, f"t{i}")]) for i in range(drafter.MAX_TOOL_ROUNDS + 1)]
    _install(monkeypatch, loop)
    with pytest.raises(DraftError, match="반복"):
        generate_draft(example_cfg, example_cfg.product("myeongyeon"), QUESTION)
