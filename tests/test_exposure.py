import shutil
import sqlite3
from datetime import datetime, timedelta

import pytest

from jisikin.config import EXAMPLE_CONFIG_PATH, ConfigError, parse_config
from jisikin.exposure import check_exposure
from jisikin.naver import (
    INTEGRATED_MOBILE_URL,
    INTEGRATED_PC_URL,
    KST,
    MOBILE_USER_AGENT,
    NaverClient,
    NaverError,
    QuestionDetail,
    RawQuestion,
    extract_questions,
    parse_detail,
)
from jisikin.storage import Store, now_kst, views_per_day

MINI = """
settings:
  exposure_top_n: 3
  exposure_sources: [pc, kin]
products:
  - id: dak
    name: 닥사렌
    keywords: [건선]
    exposure:
      keywords: [건선 크림 추천]
      regions: [인천, 부천]
      region_terms: [건선, 건선 피부과]
"""


def rq(doc_id, title="질문"):
    return RawQuestion(doc_id=str(doc_id), url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title)


# ------------------------------------------------------------------ 설정


def test_exposure_queries_combine_regions_and_terms():
    cfg = parse_config(MINI)
    assert cfg.product("dak").exposure.queries() == [
        "건선 크림 추천", "인천 건선", "인천 건선 피부과", "부천 건선", "부천 건선 피부과",
    ]
    assert cfg.settings.exposure_sources == ["pc", "kin"]
    assert len(cfg.exposure_targets()) == 5


def test_example_config_has_incheon_psoriasis(example_cfg):
    kws = [kw for _, kw in example_cfg.exposure_targets()]
    assert "인천 건선" in kws


@pytest.mark.parametrize(
    "text",
    [
        MINI.replace("[pc, kin]", "[pc, naver]"),
        MINI.replace("      region_terms: [건선, 건선 피부과]\n", ""),
        MINI.replace("    exposure:\n", "    exposure: [a]\n    x:\n"),
    ],
)
def test_exposure_config_errors(text):
    with pytest.raises(ConfigError):
        parse_config(text)


# ------------------------------------------------------------------ 네이버 읽기

INTEGRATED_HTML = """
<html><body>
<a href="https://adcr.naver.com/adcr?x=1">광고 건선 크림</a>
<section class="sc_new sp_nkin">
  <ul class="lst_total">
    <li class="bx">
      <a class="thumb" href="https://kin.naver.com/qna/detail.naver?d1id=7&dirId=70112&docId=400000003&qb=x"><img src="a.jpg"></a>
      <div class="question_group"><a class="question_text" href="https://kin.naver.com/qna/detail.naver?d1id=7&dirId=70112&docId=400000003&qb=x">인천 건선 피부과 추천해주세요</a></div>
      <div class="answer_group"><a href="https://kin.naver.com/qna/detail.naver?d1id=7&dirId=70112&docId=400000003#answer1">인천에 건선 잘 보는 곳은...</a></div>
    </li>
    <li class="bx">
      <div class="question_group"><a class="question_text" href="https://kin.naver.com/qna/detail.naver?d1id=7&dirId=70112&docId=390000001">건선 관리 어떻게 하나요</a></div>
    </li>
  </ul>
</section>
<section><a href="https://m.kin.naver.com/mobile/qna/detail.naver?d1id=7&dirId=70112&docId=410000000">부천 건선 병원</a></section>
</body></html>
"""


def test_integrated_search_order_and_dedupe():
    items = extract_questions(INTEGRATED_HTML, INTEGRATED_PC_URL)
    assert [q.doc_id for q in items] == ["400000003", "390000001", "410000000"]
    assert items[0].title == "인천 건선 피부과 추천해주세요"
    assert items[2].url == "https://kin.naver.com/qna/detail.naver?d1id=7&dirId=70112&docId=410000000"


class Resp:
    status_code = 200
    encoding = "utf-8"
    apparent_encoding = "utf-8"

    def __init__(self, text="", json_data=None):
        self.text = text
        self._json = json_data

    def json(self):
        return self._json


class Session:
    def __init__(self, resp):
        self.resp = resp
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.resp


def test_search_integrated_pc_and_mobile():
    s = Session(Resp(INTEGRATED_HTML))
    client = NaverClient(session=s, delay_seconds=0)
    assert len(client.search_integrated("인천 건선", "pc")) == 3
    assert len(client.search_integrated("인천 건선", "mobile")) == 3
    (url1, p1, h1), (url2, p2, h2) = s.calls
    assert url1 == INTEGRATED_PC_URL and p1["query"] == "인천 건선" and p1["where"] == "nexearch"
    assert url2 == INTEGRATED_MOBILE_URL and h2 == {"User-Agent": MOBILE_USER_AGENT}


def test_search_kin_ranked_uses_relevance_sort():
    data = {"items": [{"title": "인천 건선", "link": "https://kin.naver.com/qna/detail.naver?docId=5", "description": ""}]}
    s = Session(Resp(json_data=data))
    NaverClient(credentials=("id", "sec"), session=s).search_kin_ranked("인천 건선", 5)
    assert s.calls[0][1]["sort"] == "sim"
    s2 = Session(Resp(INTEGRATED_HTML))
    NaverClient(session=s2, delay_seconds=0).search_kin_ranked("인천 건선", 5)
    assert "sort" not in s2.calls[0][1]  # 웹 검색 기본값 = 정확도순


def test_parse_views():
    html = """<div class="c-heading"><div class="c-heading__title">제목</div>
      <span class="c-userinfo__info">작성일 2023.05.01.</span><span class="c-userinfo__info">조회수 12,345</span></div>"""
    d = parse_detail(html)
    assert d.views == 12345


# ------------------------------------------------------------------ 저장 / 순위 변화


def test_latest_exposures_ranks_and_changes():
    store = Store(":memory:")
    t0 = now_kst() - timedelta(hours=12)
    for d in ("1", "2", "3"):
        store.upsert_raw(rq(d, f"글{d}"), "노출:인천 건선", feed=False)
    store.record_exposure("dak", "인천 건선", "pc", ["1", "2"], now=t0)
    store.record_exposure("dak", "인천 건선", "pc", ["2", "1", "3"])
    store.record_exposure("dak", "인천 건선", "kin", ["3"])
    store.record_exposure("dak", "인천 건선", "kin", [], error="연결 실패")  # 최신이 실패해도 이전 결과 유지

    [g] = store.latest_exposures()
    assert g["keyword"] == "인천 건선"
    assert g["sources"]["pc"]["count"] == 3 and g["sources"]["kin"]["error"] == "연결 실패"
    posts = {p["doc_id"]: p for p in g["posts"]}
    assert posts["2"]["ranks"] == {"pc": 1} and posts["2"]["prev_ranks"] == {"pc": 2}
    assert posts["1"]["prev_ranks"] == {"pc": 1}
    assert posts["3"]["ranks"] == {"pc": 3, "kin": 1} and posts["3"]["prev_ranks"]["pc"] is None  # 새로 진입
    assert [p["doc_id"] for p in g["posts"]][0] in ("2", "3")  # 최고 순위 1위부터
    assert store.exposure_product("3") == "dak"


def test_views_per_day():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=KST)
    q = {"views": 3000, "asked_at": datetime(2026, 6, 1, tzinfo=KST).isoformat()}
    hist = [((now - timedelta(days=2)).isoformat(), 2800), (now.isoformat(), 3000)]
    assert views_per_day(q, hist, now) == (100.0, "recent")
    v, kind = views_per_day(q, hist[-1:], now)
    assert kind == "lifetime" and 24 < v < 26  # 3000회 / 약 120.5일
    assert views_per_day({"views": None}, [], now) == (None, "")


def test_update_detail_records_views_history():
    store = Store(":memory:")
    store.upsert_raw(rq(1), "노출:x", feed=False)
    store.update_detail("1", QuestionDetail(views=10), now=now_kst() - timedelta(days=1))
    store.update_detail("1", QuestionDetail(views=60))
    store.record_exposure("dak", "x", "pc", ["1"])
    [g] = store.latest_exposures()
    assert g["posts"][0]["views"] == 60
    assert g["posts"][0]["views_per_day_kind"] == "recent" and 45 <= g["posts"][0]["views_per_day"] <= 55


def test_exposure_posts_do_not_appear_in_new_question_feed(example_cfg):
    store = Store(":memory:")
    store.upsert_raw(rq(1, "건선 크림 추천"), "노출:건선 크림 추천", feed=False)
    from jisikin.matcher import Matcher

    store.classify(Matcher(example_cfg.products))
    assert store.get("1")["product"] == "daksaren"
    assert store.list_questions() == []
    assert store.todo_counts()["products"] == {}
    # 나중에 새 질문 검색에서도 발견되면 새 질문 목록에도 나온다
    store.upsert_raw(rq(1, "건선 크림 추천"), "검색:건선")
    assert [r["doc_id"] for r in store.list_questions()] == ["1"]


def test_migration_adds_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE questions (doc_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
           snippet TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', dir_name TEXT NOT NULL DEFAULT '',
           answer_count INTEGER, reward INTEGER, asked_at TEXT, sources TEXT NOT NULL DEFAULT '[]', product TEXT,
           score REAL NOT NULL DEFAULT 0, categories TEXT NOT NULL DEFAULT '[]', matches TEXT NOT NULL DEFAULT '[]',
           status TEXT NOT NULL DEFAULT 'new', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
           status_changed_at TEXT, detail_fetched_at TEXT, detail_error TEXT, draft TEXT, draft_at TEXT)"""
    )
    conn.execute("INSERT INTO questions (doc_id, url, title, product, first_seen, last_seen) VALUES ('1','u','사주','sinui','2026-09-29T10:00:00+09:00','2026-09-29T10:00:00+09:00')")
    conn.commit()
    conn.close()
    store = Store(db)
    assert store.get("1")["in_feed"] == 1 and store.get("1")["views"] is None
    assert [r["doc_id"] for r in store.list_questions()] == ["1"]


# ------------------------------------------------------------------ 확인 1회 실행


class FakeClient:
    credentials = None

    def __init__(self, pages, details=None, fail=False):
        self.pages = pages  # (keyword, source) -> [RawQuestion]
        self.details = details or {}
        self.fail = fail
        self.calls = []
        self.detail_calls = []

    def search_integrated(self, keyword, platform="pc"):
        self.calls.append((keyword, platform))
        if self.fail:
            raise NaverError("연결 실패")
        return self.pages.get((keyword, platform), [])

    def search_kin_ranked(self, keyword, count=10):
        self.calls.append((keyword, "kin"))
        if self.fail:
            raise NaverError("연결 실패")
        return self.pages.get((keyword, "kin"), [])

    def fetch_detail(self, url):
        doc_id = url.rsplit("=", 1)[1]
        self.detail_calls.append(doc_id)
        return self.details.get(doc_id, QuestionDetail(views=100))


def test_check_exposure_records_and_refreshes(tmp_path):
    cfg = parse_config(MINI)
    store = Store(":memory:")
    pages = {
        ("인천 건선", "pc"): [rq(1, "인천 건선 피부과"), rq(2, "건선 병원"), rq(3), rq(4)],
        ("인천 건선", "kin"): [rq(2, "건선 병원")],
    }
    client = FakeClient(pages, {"1": QuestionDetail(views=5000, answer_count=4)})
    s = check_exposure(cfg, store, client=client, log=lambda m: None)
    assert s.keywords == 5 and s.checks == 10 and not s.errors
    assert s.posts == 3  # top_n=3 → 4번 글은 제외
    assert sorted(client.detail_calls) == ["1", "2", "3"]
    groups = {g["keyword"]: g for g in store.latest_exposures()}
    # 2번: PC 2위 + 지식iN탭 1위 (두 곳 노출) → 1번: PC 1위 → 3번: PC 3위
    assert [p["doc_id"] for p in groups["인천 건선"]["posts"]] == ["2", "1", "3"]
    assert groups["인천 건선"]["posts"][1]["views"] == 5000
    assert groups["부천 건선"]["posts"] == []
    assert store.list_questions() == []  # 새 질문 목록에는 섞이지 않음
    assert store.recent_runs(1, mode="exposure")[0]["fetched"] == 3
    assert store.recent_runs(1, exclude_mode="exposure") == []

    # 바로 다시 확인하면 상세(조회수)는 주기 절반이 지나기 전까지 다시 읽지 않음
    client.detail_calls.clear()
    check_exposure(cfg, store, client=client, log=lambda m: None)
    assert client.detail_calls == []


def test_check_exposure_stops_after_repeated_failures():
    cfg = parse_config(MINI)
    store = Store(":memory:")
    client = FakeClient({}, fail=True)
    s = check_exposure(cfg, store, client=client, log=lambda m: None)
    assert len(client.calls) == 3
    assert "중단" in s.errors[-1]


# ------------------------------------------------------------------ 화면 API


@pytest.fixture
def web(tmp_path):
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env")
    store = state.store
    store.upsert_raw(rq(11, "인천 건선 피부과 추천"), "노출:인천 건선", feed=False)
    store.upsert_raw(rq(12, "병원 추천 부탁드려요"), "노출:인천 건선", feed=False)  # 키워드 매칭 안 되는 글
    store.record_exposure("daksaren", "인천 건선", "pc", ["11", "12"])
    store.record_exposure("daksaren", "삭제된 검색어", "pc", ["12"])
    store.set_status("12", "answered")
    return state, create_app(state).test_client()


def test_api_exposure(web):
    state, client = web
    data = client.get("/api/exposure").get_json()
    kws = [g["keyword"] for g in data["groups"]]
    assert kws == ["인천 건선"]  # 설정에 없는 검색어는 숨김
    posts = data["groups"][0]["posts"]
    assert [p["doc_id"] for p in posts] == ["11", "12"] and posts[0]["ranks"] == {"pc": 1}
    only = client.get("/api/exposure?unanswered=1").get_json()["groups"][0]["posts"]
    assert [p["doc_id"] for p in only] == ["11"]
    meta = client.get("/api/meta").get_json()
    assert meta["exposure"]["keywords"] == len(state.cfg.exposure_targets())
    assert [s["id"] for s in meta["exposure"]["sources"]] == ["pc", "mobile", "kin"]


def test_api_exposure_check_and_draft_product(web, monkeypatch):
    state, client = web
    H = {"X-Jisikin": "1"}
    assert client.post("/api/exposure/check", json={}).status_code == 403
    called = []
    monkeypatch.setattr(state, "trigger", lambda kind="collect": called.append(kind) or True)
    assert client.post("/api/exposure/check", json={}, headers=H).get_json() == {"started": True}
    assert called == ["exposure"]
    # 노출 글은 분류 점수가 없어도 검색어의 제품으로 초안을 만든다 (여기선 키가 없어 키 안내가 나와야 함)
    r = client.post("/api/questions/12/draft", json={}, headers=H)
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.get_json()["error"]
