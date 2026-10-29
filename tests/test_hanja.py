import shutil

import pytest

from jisikin import calc, hanja
from jisikin.config import EXAMPLE_CONFIG_PATH, parse_config
from jisikin.exposure import check_exposure
from jisikin.naver import QuestionDetail, RawQuestion
from jisikin.storage import Store

H = {"X-Jisikin": "1"}

MINI = """
settings:
  exposure_top_n: 3
  exposure_sources: [kin]
  hanja_per_run: 4
products:
  - id: mu
    name: 명운연구소
    keywords: [작명]
    calc_tools: [name, hanja]
    exposure:
      keywords: [아기 작명]
"""


def rq(doc_id, title="질문"):
    return RawQuestion(doc_id=str(doc_id), url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title)


# ------------------------------------------------------------------ 입력 · 한자 사전


def test_parse_input_splits_eum_phrases_and_bad():
    eums, phrases, bad = hanja.parse_input("현, 서\n이슬빛날 현,  밝을   현 , 泫, abc, 현")
    assert eums == ["현", "서"]
    assert [p["keyword"] for p in phrases] == ["이슬빛날 현", "밝을 현"] and phrases[0]["eum"] == "현"
    assert bad == ["泫", "abc"]


def test_expand_makes_meaning_plus_eum():
    table = {"현": [{"hanja": "泫", "meaning": "이슬 빛날"}, {"hanja": "炫", "meaning": "밝을"}], "꿻": []}

    def lookup(eum):
        if eum == "갉":
            raise ValueError("음은 한글 한 글자여야 합니다.")
        return table[eum]

    rows, errors = hanja.expand(["현", "꿻", "갉"], lookup=lookup)
    assert [r["keyword"] for r in rows] == ["이슬 빛날 현", "밝을 현"]
    assert rows[0] == {"keyword": "이슬 빛날 현", "eum": "현", "hanja": "泫"}
    assert len(errors) == 2 and "꿻" in errors[0]


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return {"success": True, "data": self.data}


class FakeSession:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.calls.append((method, url, params))
        return FakeResponse(self.data)


def test_hanja_candidates_skips_rarely_used(monkeypatch):
    monkeypatch.setenv(calc.KEY_ENV, "k" * 40)
    session = FakeSession([
        {"hanja": "玄", "meaning": "검을", "rarelyUsed": True},
        {"hanja": "泫", "meaning": "이슬 빛날", "rarelyUsed": False},
        {"hanja": "?", "meaning": "", "rarelyUsed": False},
    ])
    found = calc.hanja_candidates("현", session=session)
    assert [h["hanja"] for h in found] == ["泫"]
    method, url, params = session.calls[0]
    assert url.endswith("/api/v1/internal/calc/hanja") and params == {"eum": "현", "lastName": "false", "limit": 50}


def test_hanja_candidates_needs_key():
    with pytest.raises(calc.CalcUnavailable):
        calc.hanja_candidates("현", session=FakeSession([]))


# ------------------------------------------------------------------ 저장 · 돌아가며 확인


def test_store_dedupes_and_rotates():
    store = Store(":memory:")
    rows = [{"keyword": f"뜻{i} 현", "eum": "현", "hanja": ""} for i in range(6)]
    assert store.add_hanja_keywords("mu", rows) == 6
    assert store.add_hanja_keywords("mu", [{"keyword": "뜻0  현", "eum": "현"}, {"keyword": "뜻0현", "eum": "현"}]) == 0
    assert store.hanja_count() == 6

    # 처음엔 확인 안 한 것부터 넣은 순서대로
    assert [r["keyword"] for r in store.hanja_due(4)] == ["뜻0 현", "뜻1 현", "뜻2 현", "뜻3 현"]
    for i in range(4):
        store.mark_hanja_checked("mu", f"뜻{i} 현", 2 if i == 1 else 0)
    # 지식iN 글이 나온 검색어(뜻1)는 절반 몫으로 다시, 나머지는 아직 확인 안 한 것 → 오래 전에 확인한 것
    due = [r["keyword"] for r in store.hanja_due(4)]
    assert due[0] == "뜻1 현" and due[1:3] == ["뜻4 현", "뜻5 현"] and len(due) == 4
    assert store.hanja_due(0) == []

    assert store.delete_hanja_keywords("mu", "서") == 0
    assert store.delete_hanja_keywords("mu", "현") == 6 and store.hanja_count() == 0


class FakeClient:
    credentials = None

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def search_integrated(self, keyword, platform="pc"):
        self.calls.append((keyword, platform))
        return self.pages.get((keyword, platform), [])

    def search_kin_ranked(self, keyword, count=10):
        self.calls.append((keyword, "kin"))
        return self.pages.get((keyword, "kin"), [])

    def fetch_detail(self, url):
        return QuestionDetail(views=100)


def test_check_exposure_rotates_hanja_on_integrated_search():
    cfg = parse_config(MINI)
    store = Store(":memory:")
    store.add_hanja_keywords("mu", [{"keyword": f"뜻{i} 현", "eum": "현"} for i in range(6)])
    pages = {
        ("뜻1 현", "mobile"): [rq(1, "이슬빛날 현 이름에 써도 되나요"), rq(2)],
        ("뜻1 현", "pc"): [rq(2), rq(3)],
    }
    client = FakeClient(pages)
    s = check_exposure(cfg, store, client=client, log=lambda m: None)
    assert s.hanja == 4 and not s.errors and "한자 검색어 4개" in s.text()
    hanja_calls = [c for c in client.calls if c[0].startswith("뜻")]
    # 한자 검색어는 설정의 exposure_sources(kin)와 관계없이 통합검색 모바일·PC 에서만
    assert hanja_calls == [(f"뜻{i} 현", src) for i in range(4) for src in ("mobile", "pc")]
    found = {r["keyword"]: r["found"] for r in store.hanja_keywords()}
    assert found["뜻1 현"] == 3 and found["뜻0 현"] == 0 and found["뜻5 현"] is None
    g = next(g for g in store.latest_exposures() if g["keyword"] == "뜻1 현")
    assert [p["doc_id"] for p in g["posts"]] == ["2", "1", "3"] and g["product"] == "mu"

    client.calls.clear()
    check_exposure(cfg, store, client=client, log=lambda m: None)
    assert [c[0] for c in client.calls if c[1] == "mobile"] == ["뜻1 현", "뜻4 현", "뜻5 현", "뜻0 현"]


def test_hanja_only_runs_without_regular_targets():
    cfg = parse_config(MINI.replace("    exposure:\n      keywords: [아기 작명]\n", ""))
    store = Store(":memory:")
    assert check_exposure(cfg, store, client=FakeClient({}), log=lambda m: None).checks == 0
    store.add_hanja_keywords("mu", [{"keyword": "밝을 현", "eum": "현"}])
    client = FakeClient({})
    s = check_exposure(cfg, store, client=client, log=lambda m: None)
    assert s.hanja == 1 and client.calls == [("밝을 현", "mobile"), ("밝을 현", "pc")]


def test_latest_exposures_keeps_rarely_checked_keywords():
    """한자 검색어는 며칠에 한 번 확인 — 그 사이 다른 확인이 많이 쌓여도 마지막 결과가 남아야 함."""
    store = Store(":memory:")
    store.upsert_raw(rq(1), "노출:밝을 현", feed=False)
    store.record_exposure("mu", "밝을 현", "pc", ["1"], error=None)
    store.record_exposure("mu", "밝을 현", "pc", [], error="차단")
    with store._conn() as c:
        c.executemany(
            "INSERT INTO exposure_checks (product, keyword, source, checked_at) VALUES ('mu', ?, 'pc', '2026-10-01T00:00:00+09:00')",
            [(f"다른 {i}",) for i in range(5200)],
        )
    g = next(g for g in store.latest_exposures() if g["keyword"] == "밝을 현")
    assert [p["doc_id"] for p in g["posts"]] == ["1"] and g["sources"]["pc"]["error"] == "차단"


# ------------------------------------------------------------------ 화면 API


@pytest.fixture
def web(tmp_path):
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env")
    return state, create_app(state).test_client()


def test_api_hanja_add_list_delete(web, monkeypatch):
    state, client = web
    table = {"현": [{"hanja": "泫", "meaning": "이슬 빛날"}, {"hanja": "炫", "meaning": "밝을"}]}
    monkeypatch.setattr(calc, "hanja_candidates", lambda eum, session=None: table.get(eum, []))
    assert client.post("/api/keywords/hanja/add", json={"product": "myeongun", "text": "현"}).status_code == 403
    r = client.post("/api/keywords/hanja/add", json={"product": "myeongun", "text": "현, 꿻, 햇살 현, 泫"}, headers=H)
    data = r.get_json()
    assert data["added"] == 3 and len(data["errors"]) == 2
    r = client.post("/api/keywords/hanja/add", json={"product": "myeongun", "text": "이슬빛날 현"}, headers=H)
    assert r.get_json()["added"] == 0  # 띄어쓰기만 다른 같은 검색어
    assert client.post("/api/keywords/hanja/add", json={"product": "myeongun", "text": "abc"}, headers=H).status_code == 400

    products = {p["id"]: p for p in client.get("/api/keywords").get_json()["products"]}
    assert products["myeongun"]["hanja_enabled"] and not products["sinui"]["hanja_enabled"]
    assert products["myeongun"]["hanja"] == [{"eum": "현", "total": 3, "checked": 0, "live": []}]
    meta = client.get("/api/meta").get_json()
    assert meta["exposure"]["hanja"] == 3 and meta["exposure"]["hanja_per_run"] == 60

    r = client.post("/api/keywords/hanja/delete", json={"product": "myeongun", "eum": "현"}, headers=H)
    assert r.get_json()["removed"] == 3 and state.store.hanja_count() == 0


def test_api_hanja_add_reports_calc_unavailable(web, monkeypatch):
    _, client = web

    def down(eum, session=None):
        raise calc.CalcUnavailable("열쇠 없음")

    monkeypatch.setattr(calc, "hanja_candidates", down)
    r = client.post("/api/keywords/hanja/add", json={"product": "myeongun", "text": "현"}, headers=H)
    assert r.status_code == 400 and "한자 사전" in r.get_json()["error"]


def test_api_exposure_shows_only_hanja_groups_with_posts(web):
    state, client = web
    store = state.store
    store.add_hanja_keywords("myeongun", [{"keyword": "밝을 현", "eum": "현"}, {"keyword": "이슬 빛날 현", "eum": "현"}])
    store.upsert_raw(rq(31, "이슬빛날 현 이름 한자 괜찮나요"), "노출:이슬 빛날 현", feed=False)
    store.record_exposure("myeongun", "이슬 빛날 현", "mobile", ["31"])
    store.record_exposure("myeongun", "밝을 현", "mobile", [])
    store.mark_hanja_checked("myeongun", "이슬 빛날 현", 1)
    groups = client.get("/api/exposure").get_json()["groups"]
    hanja_groups = [g for g in groups if g["hanja"]]
    assert [g["keyword"] for g in hanja_groups] == ["이슬 빛날 현"]
    assert hanja_groups[0]["posts"][0]["doc_id"] == "31" and hanja_groups[0]["product"] == "myeongun"
    live = next(p for p in client.get("/api/keywords").get_json()["products"] if p["id"] == "myeongun")["hanja"][0]["live"]
    assert live == [{"keyword": "이슬 빛날 현", "hanja": "", "found": 1}]
