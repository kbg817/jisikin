"""명연당 계산 API 연결 — AI 초안이 사주·이름을 짐작하지 않고 계산한 값으로 쓰게 한다.

명연당(myeongyeondang.com) 서버의 계산은 손님 분석과 같은 코드다.
- 사주: 절기 기준 월주, 지방시(동경 127.5°)·서머타임, 한국천문연구원 음력표, 오행·십성·십이운성·신살·귀인·대운·세운
- 이름: 발음오행·발음음양·사격수리(81수)·수리오행·수리음양·자원오행 (인명용 한자 사전)
- 한자 찾기: 음으로 인명용 한자 후보
명연당 서버는 계산만 하고 아무것도 저장하지 않는다. 열쇠(MYD_CALC_KEY)가 없으면 이 기능은 꺼진다.

도구 이름·설명에는 상호를 넣지 않는다. 명운연구소 답변에도 같은 계산을 쓰기 때문이다.
"""
from __future__ import annotations

import json
import os
import secrets

import requests

DEFAULT_URL = "https://myeongyeondang.com"
TIMEOUT = 15
KEY_ENV = "MYD_CALC_KEY"
URL_ENV = "MYD_CALC_URL"
MIN_KEY_LENGTH = 32

TOOL_NAMES = ("saju", "name", "hanja")


class CalcUnavailable(Exception):
    """계산 서버에 연결할 수 없거나 열쇠가 맞지 않음 — 초안을 만들지 않고 운영자에게 알린다."""


def new_key() -> str:
    """명연당 서버와 함께 쓸 새 열쇠 (영문·숫자·-_ 43자)."""
    return secrets.token_urlsafe(32)


def _key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def _base_url() -> str:
    return (os.environ.get(URL_ENV, "").strip() or DEFAULT_URL).rstrip("/")


def calc_status() -> tuple[bool, str]:
    key = _key()
    if not key:
        return False, "열쇠 없음 — [설정] > 명연당 계산 연결에서 만들어 넣으면 사주·이름 초안이 정확해집니다."
    if len(key) < MIN_KEY_LENGTH:
        return False, f"열쇠가 너무 짧습니다 ({MIN_KEY_LENGTH}자 이상)."
    return True, f"열쇠 있음 ({_base_url()})"


# ---- Claude 도구 정의 (순서·내용이 바뀌면 캐시가 깨지므로 고정) ----

SAJU_TOOL = {
    "name": "saju_calculator",
    "description": (
        "한국 만세력으로 사주팔자를 계산한다. 절기 기준 월주, 출생지 지방시와 서머타임, 한국천문연구원 음력표를 반영한다. "
        "결과: 년·월·일·시주 간지, 일간, 오행 개수, 자리별 십성·십이운성·신살·귀인, 십성 분류별 개수, 대운(성별이 있을 때), "
        "올해·내년 세운, 신강신약(strength: 7단계·점수·득령/득지/득세·오행 비율)과 용신·희신·기신(억부·조후). "
        "질문에 생년월일이 있으면 사주 이야기를 하기 전에 반드시 이 도구로 계산한다. "
        "연도가 두 자리면(예: 95년생) 1900·2000년대 중 맞는 쪽으로 넣는다. 음력·양력이 없으면 양력으로 넣고 답변에 그렇게 가정했다고 쓴다. "
        "태어난 시간을 모르면 hour 를 넣지 않는다. 입력 오류가 나면 그 이유를 답변에 반영한다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "year": {"type": "integer", "description": "태어난 연도 (1900~2050)"},
            "month": {"type": "integer", "description": "월 1~12"},
            "day": {"type": "integer", "description": "일 1~31"},
            "hour": {"type": "integer", "description": "태어난 시 0~23 (모르면 넣지 않음). '오시'처럼 시진만 알면 그 시진의 가운데 시각"},
            "minute": {"type": "integer", "description": "분 0~59 (모르면 0)"},
            "calendar": {"type": "string", "enum": ["solar", "lunar"], "description": "양력 solar / 음력 lunar"},
            "leap": {"type": "boolean", "description": "음력 윤달이면 true"},
            "gender": {"type": "string", "enum": ["male", "female"], "description": "성별 (모르면 넣지 않음 — 대운 계산에 필요)"},
        },
        "required": ["year", "month", "day", "calendar"],
        "additionalProperties": False,
    },
}

NAME_TOOL = {
    "name": "name_evaluator",
    "description": (
        "성명학 기준으로 이름을 판정한다. 결과: 글자별 한자·뜻·획수(원획)·자원오행, 발음오행 배열과 등급, 발음음양, "
        "사격수리(원격·형격·이격·정격의 81수 번호·이름·길흉과 종합), 수리오행, 수리음양. "
        "이름 풀이·좋은 이름인지 묻는 질문이나 이름 후보를 평가할 때 반드시 이 도구로 계산한다. 획수·수리를 직접 세지 않는다. "
        "한자를 모르면 hanja 를 비운다(수리는 한글 획수로 본 참고값). 한자가 없는 글자만 있으면 그 자리에 한글 음절을 그대로 넣는다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "한글 이름 (성 포함, 2~5글자). 예: 김민준"},
            "hanja": {
                "type": "array",
                "items": {"type": "string"},
                "description": "이름 글자마다 한자 한 글자씩, 글자 수와 같게. 예: [\"金\", \"珉\", \"俊\"]. 모르면 넣지 않음",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}

HANJA_TOOL = {
    "name": "hanja_lookup",
    "description": (
        "음(소리) 한 글자로 대법원 인명용 한자 후보를 찾는다(뜻·획수·자원오행). 이름에 쓸 한자를 추천하거나 "
        "질문자가 쓴 한자가 인명용인지 볼 때 쓴다. 성씨 글자면 last_name 을 true 로."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "eum": {"type": "string", "description": "한글 한 글자. 예: 민"},
            "last_name": {"type": "boolean", "description": "성씨 한자를 찾으면 true"},
            "limit": {"type": "integer", "description": "최대 개수 (기본 15, 최대 30)"},
        },
        "required": ["eum"],
        "additionalProperties": False,
    },
}

_TOOL_BY_KEY = {"saju": SAJU_TOOL, "name": NAME_TOOL, "hanja": HANJA_TOOL}


def tools_for(keys: list[str]) -> list[dict]:
    """제품 설정(calc_tools)의 순서와 관계없이 늘 같은 순서로 (캐시가 깨지지 않게)."""
    return [_TOOL_BY_KEY[k] for k in TOOL_NAMES if k in keys]


# ---- 실행 ----


def _request(method: str, path: str, *, body: dict | None = None, params: dict | None = None, session=None) -> dict:
    key = _key()
    if len(key) < MIN_KEY_LENGTH:
        raise CalcUnavailable("명연당 계산 열쇠가 없습니다. [설정] > 명연당 계산 연결을 확인하세요.")
    http = session or requests
    try:
        res = http.request(
            method,
            f"{_base_url()}/api/v1/internal/calc/{path}",
            json=body,
            params=params,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "jisikin-drafter"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise CalcUnavailable(f"명연당 계산 서버에 연결할 수 없습니다: {e.__class__.__name__}") from e
    if res.status_code in (401, 404):
        raise CalcUnavailable(
            "명연당 계산 열쇠가 맞지 않거나 명연당 서버에 아직 열쇠가 없습니다 "
            f"(응답 {res.status_code}). 명연당 Actions 'Ops - Set jisikin calc key (prod)'를 확인하세요."
        )
    if res.status_code == 429:
        raise CalcUnavailable("명연당 계산 요청이 너무 많습니다. 1분 뒤 다시 시도하세요.")
    try:
        data = res.json()
    except ValueError as e:
        raise CalcUnavailable(f"명연당 계산 서버 응답을 읽을 수 없습니다 (응답 {res.status_code}).") from e
    if res.status_code == 400:
        return {"error": data.get("error") or "입력값이 올바르지 않습니다."}
    if res.status_code != 200 or not data.get("success"):
        raise CalcUnavailable(f"명연당 계산 서버 오류 (응답 {res.status_code}).")
    return {"data": data.get("data")}


def run_tool(name: str, tool_input: dict, session=None) -> tuple[str, bool]:
    """Claude 가 부른 도구를 실행해 (tool_result 내용, 오류 여부)를 돌려준다.

    입력 오류(없는 날짜 등)는 오류 결과로 돌려 AI 가 답변에 반영하게 하고,
    연결·열쇠 문제는 CalcUnavailable 로 올려 초안을 멈춘다.
    """
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "saju_calculator":
        body = {k: tool_input.get(k) for k in ("year", "month", "day", "hour", "minute", "calendar", "leap", "gender") if tool_input.get(k) is not None}
        result = _request("POST", "saju", body=body, session=session)
    elif name == "name_evaluator":
        body = {"name": tool_input.get("name", "")}
        if tool_input.get("hanja"):
            body["hanja"] = tool_input["hanja"]
        result = _request("POST", "name", body=body, session=session)
    elif name == "hanja_lookup":
        try:
            limit = max(1, min(int(tool_input.get("limit") or 15), 30))
        except (TypeError, ValueError):
            limit = 15
        params = {"eum": tool_input.get("eum", ""), "lastName": "true" if tool_input.get("last_name") else "false", "limit": limit}
        result = _request("GET", "hanja", params=params, session=session)
    else:
        return f"알 수 없는 도구: {name}", True
    if "error" in result:
        return f"입력 오류: {result['error']}", True
    return json.dumps(result["data"], ensure_ascii=False, separators=(",", ":")), False


def check_connection(session=None) -> tuple[bool, str]:
    """[연결 점검]용: 시험 사주 한 번 계산 (양력 1990-06-08 10:30 여 → 庚午 壬午 甲辰 己巳)."""
    ok, reason = calc_status()
    if not ok:
        return False, reason
    try:
        text, is_error = run_tool(
            "saju_calculator",
            {"year": 1990, "month": 6, "day": 8, "hour": 10, "minute": 30, "calendar": "solar", "gender": "female"},
            session=session,
        )
    except CalcUnavailable as e:
        return False, str(e)
    if is_error:
        return False, text
    pillars = json.loads(text).get("pillars") or {}
    got = " ".join((pillars.get(k) or {}).get("ganji", "?") for k in ("year", "month", "day", "hour"))
    expected = "庚午 壬午 甲辰 己巳"
    if got != expected:
        return False, f"계산 결과가 예상과 다릅니다: {got} (예상 {expected})"
    return True, f"연결됨 — 시험 사주 {got} ({_base_url()})"
