import base64
import hashlib
import hmac
import shutil
import subprocess
import sys
from datetime import timedelta

import pytest

from jisikin.config import EXAMPLE_CONFIG_PATH, ROOT, parse_config
from jisikin.exposure import all_targets
from jisikin.keywords import due_products, expand, generate_for_product, parse_ai_lines, save_seed_settings, seed_settings
from jisikin.naver import NaverError, parse_autocomplete
from jisikin.searchad import SearchAdClient, _count
from jisikin.storage import Store, iso, now_kst

CFG = """
products:
  - id: dak
    name: 닥사렌
    keywords: [건선, 모공 각화증]
    categories:
      - name: 병원
        keywords: [피부과]
    exposure:
      regions: [인천, 부천]
      region_terms: [건선]
"""


# ------------------------------------------------------------------ 자동완성 응답 해석


@pytest.mark.parametrize(
    "text",
    [
        '{"query":["건선"],"items":[[["건선"],["건선 원인"],["두피 건선"]],[]]}',
        '{"query":["건선"],"items":[[["건선","0"],["건선 원인","0"],["두피 건선","0"]]]}',
        '_jsonp_0({"items":[[["건선"],["건선 원인"],["두피 건선"]]]});',
        '{"items":[[{"keyword":"건선"},{"keyword":"건선 원인"},{"keyword":"두피 건선"}]]}',
    ],
)
def test_parse_autocomplete_formats(text):
    assert parse_autocomplete(text) == ["건선", "건선 원인", "두피 건선"]


def test_parse_autocomplete_garbage():
    assert parse_autocomplete("<html>차단</html>") == []
    assert parse_autocomplete("") == []


def test_parse_ai_lines():
    text = "1. 건선 원인\n2) 인천 건선 피부과\n- 두피 건선 샴푸 추천\n\"건선 초기 증상\"\n\n여기는 설명이 너무 길어서 검색어가 아닌 문장이니 무시되어야 합니다 정말로요"
    assert parse_ai_lines(text) == ["건선 원인", "인천 건선 피부과", "두피 건선 샴푸 추천", "건선 초기 증상"]


# ------------------------------------------------------------------ 검색광고 API


def test_count_values():
    assert _count(1200) == 1200
    assert _count("< 10") == 5
    assert _count("1,234") == 1234
    assert _count(None) is None


class AdResp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class AdSession:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        r = AdResp(self.data)
        r.status_code = self.status
        return r


def test_searchad_signs_request():
    s = AdSession({"keywordList": [{"relKeyword": "인천건선", "monthlyPcQcCnt": 120, "monthlyMobileQcCnt": "< 10"}]})
    client = SearchAdClient("1234", "LICENSE", "SECRET", session=s, delay_seconds=0)
    rows = client.keyword_stats(["인천 건선", "건선"])
    assert rows == [{"keyword": "인천건선", "pc": 120, "mobile": 5}]
    url, params, headers = s.calls[0]
    assert url == "https://api.naver.com/keywordstool"
    assert params == {"hintKeywords": "인천건선,건선", "showDetail": "1"}  # 띄어쓰기 제거
    expected = base64.b64encode(
        hmac.new(b"SECRET", f"{headers['X-Timestamp']}.GET./keywordstool".encode(), hashlib.sha256).digest()
    ).decode()
    assert headers["X-Signature"] == expected and headers["X-API-KEY"] == "LICENSE" and headers["X-Customer"] == "1234"


def test_searchad_auth_error():
    client = SearchAdClient("1", "L", "S", session=AdSession({}, status=401), delay_seconds=0)
    with pytest.raises(NaverError) as e:
        client.keyword_stats(["건선"])
    assert e.value.fatal


# ------------------------------------------------------------------ 확장


class FakeNaver:
    credentials = None

    def __init__(self, tree, fail=False):
        self.tree = tree
        self.fail = fail
        self.calls = []

    def autocomplete(self, q):
        self.calls.append(q)
        if self.fail:
            raise NaverError("자동완성 연결 실패")
        return self.tree.get(q, [])


TREE = {
    "건선": ["건선", "건선 원인", "두피 건선", "건선 크림", "건선 연예인"],
    "건선 원인": ["건선 원인 스트레스", "건선 원인 음식"],
    "두피 건선": ["두피 건선 샴푸", "두피 건선 병원"],
    "건선 크림": ["건선 크림 추천"],
    "건선 연예인": ["건선 연예인 누구"],
}


def test_expand_without_volumes_uses_estimated_scores():
    cfg = parse_config(CFG)
    client = FakeNaver({**TREE, "모공각화증": ["모공각화증 크림"]})
    res = expand(cfg, cfg.product("dak"), ["건선"], client, use_ai=False)
    kws = [c.keyword for c in res.candidates]
    assert kws[0] == "건선"  # 메인 키워드 자체가 1순위
    assert "건선 원인" in kws[:6] and "두피 건선 샴푸" in kws
    assert "인천 건선" in kws and "부천 건선" in kws  # 지역 조합
    assert "건선 추천" in kws  # 의도 조합
    assert kws.index("건선 원인") < kws.index("건선 비용")  # 자동완성 > 조합
    assert res.used == ["autocomplete", "combo"]
    assert client.calls == ["건선", "건선 원인", "두피 건선", "건선 크림", "건선 연예인"]


def test_expand_filters_unrelated_and_ranks_by_volume():
    cfg = parse_config(CFG)

    class Ad:
        def __init__(self):
            self.calls = []

        def keyword_stats(self, hints):
            self.calls.append(hints)
            table = {
                "건선": (3000, 20000), "인천건선": (100, 900), "건선원인": (500, 4000),
                "피부과추천": (800, 5000),   # 메인 키워드는 없지만 제품 카테고리(피부과)에 걸림 → 통과
                "다이어트": (9000, 90000),   # 관련 없음 → 탈락
            }
            if len(hints) == 1 and hints[0] == "건선":
                return [{"keyword": k, "pc": p, "mobile": m} for k, (p, m) in table.items()]
            return [{"keyword": k, "pc": table[k][0], "mobile": table[k][1]} for k in (h.replace(" ", "") for h in hints) if k in table]

    ad = Ad()
    res = expand(cfg, cfg.product("dak"), ["건선"], FakeNaver(TREE), ad_client=ad, use_ai=False)
    kws = [c.keyword for c in res.candidates]
    assert "다이어트" not in kws
    assert kws[:4] == ["건선", "피부과추천", "건선 원인", "인천 건선"]  # 월간 검색수 순
    c = res.candidates[0]
    assert (c.pc, c.mobile) == (3000, 20000) and "searchad" in c.sources
    assert "searchad" in res.used


def test_expand_with_ai(monkeypatch):
    cfg = parse_config(CFG)
    import jisikin.keywords as kwmod

    monkeypatch.setattr(kwmod, "ai_status", lambda: (True, "ok"))
    monkeypatch.setattr(kwmod, "ask_claude", lambda cfg, system, user, effort=None: "건선 초기 증상\n건선에 좋은 음식\n오늘 날씨")
    res = expand(cfg, cfg.product("dak"), ["건선"], FakeNaver({}), use_ai=True)
    kws = [c.keyword for c in res.candidates]
    assert "건선 초기 증상" in kws and "건선에 좋은 음식" in kws and "오늘 날씨" not in kws
    assert "ai" in res.used


def test_expand_reports_autocomplete_failure():
    cfg = parse_config(CFG)
    res = expand(cfg, cfg.product("dak"), ["건선"], FakeNaver({}, fail=True), use_ai=False)
    assert res.errors and "자동완성" in res.errors[0]
    assert [c.keyword for c in res.candidates][0] == "건선"  # 조합만으로도 후보는 나옴


# ------------------------------------------------------------------ 저장 / 사람이 고른 것 유지


def test_generate_saves_and_keeps_user_choices():
    cfg = parse_config(CFG)
    store = Store(":memory:")
    save_seed_settings(store, "dak", ["건선", " 건선 ", ""], 3)
    assert seed_settings(store, "dak")["seeds"] == ["건선"]
    client = FakeNaver(TREE)
    generate_for_product(cfg, store, "dak", client=client, log=lambda m: None)
    rows = store.auto_keywords("dak")
    assert sum(r["enabled"] for r in rows) == 3
    assert seed_settings(store, "dak")["generated_at"]

    off = next(r["keyword"] for r in rows if r["enabled"] and r["keyword"] != "건선")
    store.set_auto_keyword("dak", off, False)               # 사람이 끔
    store.set_auto_keyword("dak", "인천 건선 한의원", True, sources=["manual"])  # 직접 추가
    generate_for_product(cfg, store, "dak", client=client, log=lambda m: None)
    rows = {r["keyword"]: r for r in store.auto_keywords("dak")}
    assert rows[off]["enabled"] == 0 and rows[off]["user_set"] == 1
    assert rows["인천 건선 한의원"]["enabled"] == 1
    assert sum(r["enabled"] for r in rows.values() if not r["user_set"]) == 3

    targets = [kw for _, kw in all_targets(cfg, store)]
    assert targets[:2] == ["인천 건선", "부천 건선"]  # 설정 파일 검색어 먼저
    assert "인천 건선 한의원" in targets and off not in targets
    assert targets.count("인천 건선") == 1  # 중복 제거


def test_due_products():
    cfg = parse_config(CFG)
    store = Store(":memory:")
    assert due_products(cfg, store) == []
    save_seed_settings(store, "dak", ["건선"], 10)
    assert due_products(cfg, store) == ["dak"]
    s = seed_settings(store, "dak")
    s["generated_at"] = iso(now_kst() - timedelta(days=2))
    store.kv_set("seeds:dak", s)
    assert due_products(cfg, store) == []
    s["generated_at"] = iso(now_kst() - timedelta(days=8))
    store.kv_set("seeds:dak", s)
    assert due_products(cfg, store) == ["dak"]


# ------------------------------------------------------------------ 화면 API


@pytest.fixture
def web(tmp_path):
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", data_dir=tmp_path)
    return state, create_app(state).test_client()


def test_keywords_api(web, monkeypatch):
    state, client = web
    H = {"X-Jisikin": "1"}
    triggered = []
    monkeypatch.setattr(state, "trigger", lambda kind="collect", product=None, then_exposure=False: triggered.append((kind, product, then_exposure)) or True)

    assert client.get("/keywords").status_code == 200
    data = client.get("/api/keywords").get_json()
    dak = next(p for p in data["products"] if p["id"] == "daksaren")
    assert dak["seeds"] == [] and dak["max"] == 20
    assert any(k["keyword"] == "인천 건선" for k in dak["config_keywords"])
    assert data["sources"] == {"autocomplete": True, "searchad": False, "ai": False}

    r = client.post("/api/keywords/seeds", json={"product": "daksaren", "seeds": "건선, 모공각화증", "max": 15}, headers=H).get_json()
    assert r["seeds"] == ["건선", "모공각화증"] and r["max"] == 15 and r["started"] is True
    assert triggered == [("keywords", "daksaren", True)]
    assert client.post("/api/keywords/seeds", json={"product": "nope", "seeds": "x"}, headers=H).status_code == 404

    assert client.post("/api/keywords/add", json={"product": "daksaren", "keyword": "  부평   건선 "}, headers=H).get_json() == {"ok": True}
    assert client.post("/api/keywords/toggle", json={"product": "daksaren", "keyword": "부평 건선", "enabled": False}, headers=H).get_json() == {"ok": True}
    dak = next(p for p in client.get("/api/keywords").get_json()["products"] if p["id"] == "daksaren")
    assert dak["auto"] == [
        {"keyword": "부평 건선", "seed": "", "sources": ["직접"], "pc": None, "mobile": None, "score": 0.0,
         "enabled": False, "user_set": True, "exposure": None}
    ]
    assert client.post("/api/keywords/add", json={"product": "daksaren", "keyword": "x" * 50}, headers=H).status_code == 400


def test_generate_requires_seeds(web):
    _, client = web
    r = client.post("/api/keywords/generate", json={"product": "daksaren"}, headers={"X-Jisikin": "1"})
    assert r.status_code == 400


# ------------------------------------------------------------------ 서버용 데이터 폴더


def test_server_data_dir(tmp_path):
    code = "from jisikin import config as c; print(c.CONFIG_PATH); print(c.ENV_PATH); print(c.DB_PATH)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True,
        env={"JISIKIN_DATA_DIR": str(tmp_path), "PATH": ""},
    ).stdout.split()
    assert out == [str(tmp_path / "config.yaml"), str(tmp_path / ".env"), str(tmp_path / "jisikin.db")]
