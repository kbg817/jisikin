import shutil

from jisikin.cafe import collect_cafe, to_post
from jisikin.config import EXAMPLE_CONFIG_PATH, parse_config
from jisikin.migrations import _cafe
from jisikin.naver import NaverClient, NaverError
from jisikin.storage import Store
from jisikin.web import AppState, create_app

H = {"X-Jisikin": "1"}

ITEM = {
    "title": "[사주질문] 출산 후 사주 <b>작명</b> 문의드립니다",
    "link": "https://cafe.naver.com/sajupuli/12345",
    "description": "다음 달 출산 예정인데 아기 이름 작명소 추천 부탁드려요 &amp; 개명도 고민",
    "cafename": "사주풀이 카페",
    "cafeurl": "https://cafe.naver.com/sajupuli",
}


def test_to_post_parses_cafe_item():
    post = to_post(ITEM)
    assert post.platform == "cafe" and post.post_id == "cafe:sajupuli/12345"
    assert post.url == "https://cafe.naver.com/sajupuli/12345"
    assert post.title == "[사주질문] 출산 후 사주 작명 문의드립니다"
    assert "&" in post.body and "<b>" not in post.title
    assert post.author == "사주풀이 카페" and post.author_url == "https://cafe.naver.com/sajupuli"
    assert to_post({"link": "https://blog.naver.com/x/1"}) is None


class FakeClient:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error
        self.calls = []

    def search_cafe(self, query, count=50, sort="date"):
        self.calls.append((query, count, sort))
        if self.error:
            raise self.error
        return self.items


def test_collect_cafe_saves_and_classifies(tmp_path, example_cfg):
    store = Store(tmp_path / "db.sqlite")
    client = FakeClient([ITEM, {"link": "https://blog.naver.com/x/1"}])
    s = collect_cafe(example_cfg, store, client, log=lambda m: None)
    assert s.queries == len(example_cfg.cafe_queries()) > 0
    assert all(c[2] == "date" for c in client.calls)
    assert s.fetched == s.queries and s.new_total == 1 and s.new_relevant == 1
    assert store.get_social("cafe:sajupuli/12345")["product"] == "myeongun"
    again = collect_cafe(example_cfg, store, client, log=lambda m: None)
    assert again.new_total == 0


def test_collect_cafe_stops_on_fatal_error(tmp_path, example_cfg):
    store = Store(tmp_path / "db.sqlite")
    client = FakeClient(error=NaverError("키 없음", fatal=True))
    s = collect_cafe(example_cfg, store, client, log=lambda m: None)
    assert len(client.calls) == 1 and len(s.errors) == 1


class FakeResponse:
    status_code = 200

    def json(self):
        return {"items": [ITEM]}


class FakeSession:
    def __init__(self):
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse()


def test_search_cafe_uses_cafe_endpoint():
    session = FakeSession()
    items = NaverClient(credentials=("id", "secret"), session=session).search_cafe("작명", 30)
    assert items[0]["cafename"] == "사주풀이 카페"
    url, params = session.calls[0]
    assert url == "https://naverapihub.apigw.ntruss.com/search/v1/cafearticle"
    assert params["display"] == 30 and params["sort"] == "date"


def test_cafe_migration_adds_keywords_once():
    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    start = example.index("    cafe:\n      # [카페]")
    end = example.index("\n", example.index("      keywords:", start)) + 1
    old = example[:start] + example[end:]
    assert parse_config(old).cafe_queries() == []
    new = _cafe(old, example)
    assert new == example and _cafe(new, example) is None
    assert {p.id for p, _ in parse_config(new).cafe_queries()} == {"myeongun"}


def test_cafe_collect_api_needs_key(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env")
    client = create_app(state).test_client()
    r = client.post("/api/cafe/collect", json={}, headers=H)
    assert r.status_code == 400 and "네이버 API 키" in r.get_json()["error"]
    meta = client.get("/api/meta").get_json()
    assert meta["cafe"]["enabled"] is False and meta["cafe"]["queries"] > 0
    state.store.upsert_social(to_post(ITEM), "작명")
    from jisikin.social import social_matcher

    state.store.classify_social(social_matcher(state.cfg))
    items = client.get("/api/social?platform=cafe").get_json()["items"]
    assert [i["post_id"] for i in items] == ["cafe:sajupuli/12345"]


def test_collect_cafe_auth_error_explains_how_to_enable(tmp_path, example_cfg):
    store = Store(tmp_path / "db.sqlite")
    err = NaverError("NAVER API HUB 401 요청한 API는 이 Application에서 활성화되어 있지 않습니다.", fatal=True, auth=True)
    s = collect_cafe(example_cfg, store, FakeClient(error=err), log=lambda m: None)
    assert "카페글" in s.errors[0]


def test_cafe_post_with_slash_in_id_routes(tmp_path):
    """카페 글 번호엔 '/' 가 들어 있음 (cafe:카페/글번호) — 초안·상태 주소가 404 가 나면 안 됨."""
    from urllib.parse import quote

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env")
    client = create_app(state).test_client()
    state.store.upsert_social(to_post(ITEM), "작명")
    pid = quote("cafe:sajupuli/12345", safe="")
    r = client.post(f"/api/social/{pid}/draft", json={}, headers=H)
    assert r.status_code == 400 and "error" in r.get_json()  # AI 키가 없을 뿐, 글은 찾음
    r = client.post(f"/api/social/{pid}/draft/save", json={"draft": "좋은 이름 지으세요"}, headers=H)
    assert r.status_code == 200
    r = client.post(f"/api/social/{pid}/status", json={"status": "answered", "draft": "좋은 이름 지으세요"}, headers=H)
    assert r.get_json() == {"ok": True}
    assert state.store.get_social("cafe:sajupuli/12345")["status"] == "answered"
