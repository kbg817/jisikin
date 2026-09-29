import shutil

import pytest

from jisikin.collector import FEED_SHARE, effective_interval, estimate_api_calls_per_day
from jisikin.config import EXAMPLE_CONFIG_PATH, parse_config
from jisikin.exposure import check_exposure
from jisikin.naver import NaverClient, NaverError, RawQuestion
from jisikin.storage import ApiBudget, Store

API_DATA = {"items": [{"title": "질문", "link": "https://kin.naver.com/qna/detail.naver?docId=5", "description": ""}]}


class Resp:
    status_code = 200
    encoding = "utf-8"
    apparent_encoding = "utf-8"
    text = ""

    def json(self):
        return API_DATA


class Session:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return Resp()


def test_budget_counts_and_stops_api_calls():
    store = Store(":memory:")
    budget = ApiBudget(store, daily_limit=3)
    session = Session()
    client = NaverClient(credentials=("id", "s"), session=session, budget=budget)
    for _ in range(3):
        client.search_api("건선")
    assert budget.used() == 3
    with pytest.raises(NaverError) as e:
        client.search_api("건선")
    assert e.value.budget and e.value.fatal and "상한" in str(e.value)
    assert session.calls == 3  # 상한에 닿은 뒤로는 네이버에 요청을 보내지 않음
    assert ApiBudget(store, 0).used() == 3  # 0 = 상한 없음 (세기는 계속)


def test_budget_survives_restart(tmp_path):
    db = tmp_path / "db.sqlite"
    ApiBudget(Store(db), 100).record(7)
    assert ApiBudget(Store(db), 100).used() == 7


def test_interval_stretches_when_too_many_keywords(monkeypatch, example_cfg):
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "s")
    cfg = example_cfg
    assert effective_interval(cfg) == 10  # 기본 검색어 58개는 10분이면 충분
    extra = [f"추가 검색어 {i}" for i in range(250)]
    cfg.products[0].keywords.extend(extra)
    n = len(cfg.all_search_queries())
    interval = effective_interval(cfg)
    assert interval > 10
    assert estimate_api_calls_per_day(cfg) <= cfg.settings.api_daily_limit * FEED_SHARE
    assert n * 1440 / interval <= cfg.settings.api_daily_limit * FEED_SHARE
    assert n * 1440 / (interval - 1) > cfg.settings.api_daily_limit * FEED_SHARE  # 딱 필요한 만큼만 늘림
    cfg.settings.api_daily_limit = 0
    assert effective_interval(cfg) == 10  # 상한을 끄면 그대로


def test_exposure_skips_kin_tab_when_budget_is_used_up():
    cfg = parse_config("""
settings:
  exposure_sources: [pc, kin]
products:
  - id: dak
    name: 닥사렌
    keywords: [건선]
    exposure:
      keywords: [인천 건선, 부천 건선]
""")
    store = Store(":memory:")

    class Client:
        credentials = ("id", "s")

        def __init__(self):
            self.calls = []

        def search_integrated(self, kw, platform="pc"):
            self.calls.append((kw, platform))
            return [RawQuestion(doc_id="1", url="https://kin.naver.com/qna/detail.naver?docId=1", title="인천 건선 병원")]

        def search_kin_ranked(self, kw, count=10):
            self.calls.append((kw, "kin"))
            raise NaverError("오늘 네이버 API 호출 상한에 도달", fatal=True, budget=True)

        def fetch_detail(self, url):
            from jisikin.naver import QuestionDetail

            return QuestionDetail(views=10)

    client = Client()
    s = check_exposure(cfg, store, client=client, log=lambda m: None)
    assert client.calls == [("인천 건선", "pc"), ("인천 건선", "kin"), ("부천 건선", "pc")]
    assert s.checks == 2 and any("지식iN탭 순위를 건너뜁니다" in e for e in s.errors)


def test_min_volume_keeps_low_volume_keywords_off():
    store = Store(":memory:")
    cands = [
        {"keyword": "건선", "pc": 3000, "mobile": 20000, "score": 23000},
        {"keyword": "건선 비용", "pc": 5, "mobile": 20, "score": 25},
        {"keyword": "건선 병원", "pc": None, "mobile": None, "score": 40},  # 검색수 모름 → 켤 수 있음
    ]
    store.replace_auto_keywords("dak", cands, max_enabled=10, min_volume=50)
    rows = {r["keyword"]: r["enabled"] for r in store.auto_keywords("dak")}
    assert rows == {"건선": 1, "건선 비용": 0, "건선 병원": 1}


@pytest.fixture
def web(tmp_path):
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env", data_dir=tmp_path)
    return state, create_app(state).test_client()


def test_saving_searchad_keys_regenerates_keywords(web, monkeypatch):
    from jisikin.keywords import save_seed_settings

    state, client = web
    save_seed_settings(state.store, "daksaren", ["건선"], 20)
    calls = []
    monkeypatch.setattr(state, "trigger", lambda kind="collect", product=None, then_exposure=False: calls.append((kind, product)) or True)
    page = client.get("/settings").get_data(as_text=True)
    tok = page.split('name="csrf" value="')[1].split('"')[0]
    client.post("/settings/keys", data={"csrf": tok, "NAVER_AD_CUSTOMER_ID": "123", "NAVER_AD_ACCESS_LICENSE": "L", "NAVER_AD_SECRET_KEY": "S"})
    assert ("keywords", "daksaren") in calls
    r = client.post("/api/keywords/seeds", json={"product": "daksaren", "seeds": "건선", "max": 20, "min_volume": 100}, headers={"X-Jisikin": "1"})
    assert r.get_json()["min_volume"] == 100


def test_settings_page_shows_api_usage(web, monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "s")
    state, client = web
    ApiBudget(state.store, 20000).record(1234)
    page = client.get("/settings").get_data(as_text=True)
    assert "오늘 <b>1,234회</b> 사용" in page and "하루 상한 20,000회" in page
