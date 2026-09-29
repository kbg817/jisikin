import shutil
from datetime import timedelta

import pytest

from jisikin.config import EXAMPLE_CONFIG_PATH, parse_config
from jisikin.drafter import build_social_prompt
from jisikin.migrations import _SOCIAL_RE, _social
from jisikin.social import (
    SocialError,
    SocialPost,
    ThreadsClient,
    YouTubeBudget,
    YouTubeClient,
    collect_social,
    maybe_refresh_threads_token,
    parse_duration,
    social_matcher,
)
from jisikin.storage import Store, now_kst
from jisikin.web import AppState, create_app

H = {"X-Jisikin": "1"}


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.reason = "ERR"

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses, heads=None):
        self.responses = list(responses)
        self.heads = heads or {}
        self.calls = []

    def head(self, url, allow_redirects=True, timeout=None):
        self.calls.append((url, None))
        return FakeResponse(self.heads.get(url, 404), {})

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


YT_SEARCH = {
    "items": [
        {
            "id": {"kind": "youtube#video", "videoId": "abc123"},
            "snippet": {
                "publishedAt": "2026-09-29T10:00:00Z", "channelId": "UC1", "title": "모공각화증 &amp; 닭살 피부 관리법",
                "description": "잘린 설명", "channelTitle": "피부 채널",
                "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/abc123/mqdefault.jpg"}},
            },
        },
        {"id": {"kind": "youtube#channel", "channelId": "UC2"}, "snippet": {}},
    ]
}
YT_VIDEOS = {
    "items": [
        {"id": "abc123", "statistics": {"viewCount": "15000", "likeCount": "300", "commentCount": "3"},
         "snippet": {"description": "팔 오돌토돌 모공각화증 관리 전체 설명"}}
    ]
}


def test_example_config_has_social(example_cfg):
    daksaren = example_cfg.product("daksaren")
    assert daksaren.social_queries() == ["닥사렌", "모공각화증", "건선 관리"]
    assert example_cfg.settings.social_interval_hours == 6
    assert "관계를 밝힙니다" in example_cfg.ai.social_guide
    # social 이 없으면 제품 이름으로 검색
    cfg = parse_config("products:\n  - id: a\n    name: 우리제품\n    keywords: [크림]\n")
    assert cfg.product("a").social_queries() == ["우리제품"]


def test_youtube_client_parses_search_and_stats(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    session = FakeSession([FakeResponse(200, YT_SEARCH), FakeResponse(200, YT_VIDEOS)])
    client = YouTubeClient("KEY", YouTubeBudget(store, 9000), session=session)
    posts = client.search("모공각화증", now_kst() - timedelta(days=7), 25)
    assert len(posts) == 1
    p = posts[0]
    assert p.post_id == "yt:abc123" and p.url == "https://www.youtube.com/watch?v=abc123"
    assert p.title == "모공각화증 & 닭살 피부 관리법"
    assert p.body == "팔 오돌토돌 모공각화증 관리 전체 설명"  # videos.list 의 전체 설명으로 교체
    assert (p.views, p.likes, p.comments) == (15000, 300, 3)
    assert p.published_at.isoformat().startswith("2026-09-29T10:00:00")
    params = session.calls[0][1]
    assert params["order"] == "relevance" and params["type"] == "video" and params["key"] == "KEY"
    assert YouTubeBudget(store, 9000).used() == 101


def test_youtube_errors_and_budget(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    quota = {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}
    client = YouTubeClient("KEY", session=FakeSession([FakeResponse(403, quota)]))
    with pytest.raises(SocialError) as e:
        client.search("x", now_kst(), 5)
    assert e.value.fatal and "할당량" in str(e.value)
    bad = {"error": {"message": "API key not valid. Please pass a valid API key.", "errors": [{"reason": "badRequest"}]}}
    client = YouTubeClient("KEY", session=FakeSession([FakeResponse(400, bad)]))
    with pytest.raises(SocialError) as e:
        client.search("x", now_kst(), 5)
    assert e.value.fatal and "키가 올바르지" in str(e.value)
    # 상한에 닿으면 요청하지 않는다
    budget = YouTubeBudget(store, 150)
    budget.record(100)
    session = FakeSession([])
    with pytest.raises(SocialError) as e:
        YouTubeClient("KEY", budget, session=session).search("x", now_kst(), 5)
    assert e.value.fatal and not session.calls


def test_threads_client_parses_and_explains_permission():
    data = {"data": [{"id": "999", "text": "닥사렌 써본 사람?\n팔 닭살 때문에 고민", "permalink": "https://www.threads.net/@a/post/X",
                      "timestamp": "2026-09-29T10:00:00+0000", "username": "a"}]}
    session = FakeSession([FakeResponse(200, data)])
    posts = ThreadsClient("TOKEN", session=session).search("닥사렌", now_kst() - timedelta(days=7), 50)
    assert posts[0].post_id == "th:999" and posts[0].author == "a"
    assert posts[0].published_at.utcoffset() == timedelta(0)
    assert session.calls[0][1]["search_type"] == "RECENT"
    denied = {"error": {"message": "Application does not have permission for this action", "code": 10}}
    with pytest.raises(SocialError) as e:
        ThreadsClient("T", session=FakeSession([FakeResponse(400, denied)])).search("x", now_kst(), 5)
    assert e.value.fatal and "threads_keyword_search" in str(e.value)


def test_threads_token_refresh_every_week(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    saved = []
    client = ThreadsClient("OLD", session=FakeSession([FakeResponse(200, {"access_token": "NEW", "expires_in": 5184000})]))
    maybe_refresh_threads_token(client, store, saved.append, lambda m: None)
    assert client.token == "NEW" and saved == ["NEW"]
    maybe_refresh_threads_token(client, store, saved.append, lambda m: None)  # 7일 안에는 다시 안 함
    assert saved == ["NEW"] and not client.session.responses


class FakeClient:
    def __init__(self, posts_by_query, error=None):
        self.posts_by_query = posts_by_query
        self.error = error
        self.queries = []

    def search(self, query, since, limit):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.posts_by_query.get(query, [])


def _post(pid, platform="youtube", title="", body="", **kw):
    kw.setdefault("url", f"https://example.com/{pid}")
    return SocialPost(platform=platform, post_id=pid, title=title, body=body,
                      published_at=now_kst() - timedelta(hours=2), **kw)


def test_collect_social_saves_classifies_and_dedupes(tmp_path, example_cfg):
    store = Store(tmp_path / "db.sqlite")
    yt = FakeClient({
        "닥사렌": [_post("yt:1", title="닥사렌 모각크림 한 달 후기", views=500, comments=2)],
        "모공각화증": [_post("yt:1", title="닥사렌 모각크림 한 달 후기", views=800), _post("yt:2", title="오늘의 브이로그")],
    })
    th = FakeClient({"신의소리": [_post("th:9", "threads", body="신의소리에서 재회운 봤는데\n소름")]})
    s = collect_social(example_cfg, store, clients={"youtube": yt, "threads": th}, log=lambda m: None)
    assert s.new_total == 3 and s.new_relevant == 2 and not s.errors
    assert yt.queries == example_cfg.social_queries()
    p1 = store.get_social("yt:1")
    assert p1["product"] == "daksaren" and p1["views"] == 800 and p1["queries"] == ["닥사렌", "모공각화증"]
    assert store.get_social("yt:2")["product"] is None  # 검색엔 걸렸지만 제목/설명에 키워드가 없음
    th9 = store.get_social("th:9")
    assert th9["product"] == "sinui" and "재회운" in th9["categories"]  # 브랜드명(social.keywords)도 제품 키워드로
    assert [r["post_id"] for r in store.list_social("youtube")] == ["yt:1"]
    assert {r["post_id"] for r in store.list_social("youtube", include_low=True)} == {"yt:1", "yt:2"}
    counts = store.social_counts()
    assert counts["youtube"]["daksaren"]["total"] == 1 and counts["threads"]["sinui"]["new"] == 1
    run = store.recent_runs(1, mode="social")[0]
    assert run["new_total"] == 3
    assert store.recent_runs(1, exclude_mode=("exposure", "social")) == []


def test_collect_social_stops_platform_on_fatal_error(tmp_path, example_cfg):
    store = Store(tmp_path / "db.sqlite")
    yt = FakeClient({}, error=SocialError("할당량 소진", fatal=True))
    s = collect_social(example_cfg, store, clients={"youtube": yt}, log=lambda m: None)
    assert yt.queries == [example_cfg.social_queries()[0]]
    assert len(s.errors) == 1 and s.errors[0].startswith("유튜브 ")


def test_social_matcher_keeps_exclude_rules(example_cfg):
    m = social_matcher(example_cfg)
    assert m.best(m.classify("음파쑥쑥 써봤어요", "")).product_id == "eumpa"
    assert m.best(m.classify("주식 성장판", "")) is None  # 음파쑥쑥의 !주식 제외어


def test_social_prompt_uses_comment_style(example_cfg):
    post = {"platform": "youtube", "title": "모공각화증 관리", "body": "설명", "author": "피부 채널", "categories": ["모공각화증"]}
    system, user = build_social_prompt(example_cfg, example_cfg.product("daksaren"), post)
    assert "유튜브 영상에 달 댓글" in system and "치료" in system  # 제품 가이드의 금지 표현도 들어감
    assert "[제목]\n모공각화증 관리" in user and "[작성자] 피부 채널" in user


def test_migration_adds_social_blocks_once():
    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    old = _SOCIAL_RE.sub("", example)  # social 이 없던 예전 설정
    assert "    social:" not in old
    new = _social(old, example)
    assert new is not None
    cfg = parse_config(new)
    assert cfg.product("sinui").social_queries() == ["신의소리", "신점 후기", "전화 타로"]
    assert _social(new, example) is None


@pytest.fixture
def web(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env")
    state.store.upsert_social(_post("yt:1", title="닥사렌 후기"), "닥사렌")
    state.store.classify_social(social_matcher(state.cfg))
    return state, create_app(state).test_client()


def test_social_api_status_counts_as_answer(web):
    state, client = web
    items = client.get("/api/social?platform=youtube").get_json()["items"]
    assert [i["post_id"] for i in items] == ["yt:1"]
    assert client.get("/api/social?platform=tiktok").status_code == 400
    meta = client.get("/api/meta").get_json()
    assert meta["social"]["counts"]["youtube"]["daksaren"]["total"] == 1
    assert meta["social"]["platforms"] == {"youtube": False, "threads": False}
    assert client.post("/api/social/yt:1/status", json={"status": "answered"}).status_code == 403
    r = client.post("/api/social/yt:1/status", json={"status": "answered", "draft": "좋은 영상 감사합니다"}, headers=H)
    assert r.get_json() == {"ok": True}
    post = state.store.get_social("yt:1")
    assert post["status"] == "answered" and post["draft"] == "좋은 영상 감사합니다"
    assert state.store.answer_stats()[""]["today"] == 1
    assert client.get("/api/social?platform=youtube").get_json()["items"] == []
    assert client.post("/api/social/yt:404/status", json={"status": "answered"}, headers=H).status_code == 404


def test_social_collect_and_draft_need_keys(web):
    _, client = web
    r = client.post("/api/social/collect", json={}, headers=H)
    assert r.status_code == 400 and "유튜브 API 키" in r.get_json()["error"]
    r = client.post("/api/social/yt:1/draft", json={}, headers=H)
    assert r.status_code == 400
    page = client.get("/settings").get_data(as_text=True)
    assert "YOUTUBE_API_KEY" in page and "THREADS_ACCESS_TOKEN" in page


def test_youtube_lists_most_viewed_first_and_keeps_older_videos(web):
    state, client = web
    old = _post("yt:2", title="닥사렌 두 달 사용기", views=90000)
    old.published_at = now_kst() - timedelta(days=60)  # 쓰레드 기준(14일)보다 오래됐지만 유튜브는 보여줌
    state.store.upsert_social(old, "닥사렌")
    state.store.upsert_social(_post("yt:3", title="닥사렌 언박싱", views=40), "닥사렌")
    state.store.classify_social(social_matcher(state.cfg))
    items = client.get("/api/social?platform=youtube").get_json()["items"]
    assert [i["post_id"] for i in items][:2] == ["yt:2", "yt:3"]  # 조회수 많은 순이 기본
    latest = client.get("/api/social?platform=youtube&sort=latest").get_json()["items"]
    assert latest[-1]["post_id"] == "yt:2"
    assert client.get("/api/meta").get_json()["social"]["counts"]["youtube"]["daksaren"]["total"] == 3


def test_collect_social_uses_platform_age_and_youtube_order(tmp_path, example_cfg):
    store = Store(tmp_path / "db.sqlite")
    seen = {}

    class Spy(FakeClient):
        def search(self, query, since, limit):
            seen[self.name] = since
            return []

    yt, th = Spy({}), Spy({})
    yt.name, th.name = "youtube", "threads"
    collect_social(example_cfg, store, clients={"youtube": yt, "threads": th}, log=lambda m: None)
    s = example_cfg.settings
    assert round((now_kst() - seen["youtube"]).days) in (s.youtube_max_age_days - 1, s.youtube_max_age_days)
    assert round((now_kst() - seen["threads"]).days) in (s.social_max_age_days - 1, s.social_max_age_days)
    assert YouTubeClient("K", order="viewCount").order == "viewCount"


def test_parse_duration():
    assert parse_duration("PT45S") == 45
    assert parse_duration("PT1M5S") == 65
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("P0D") == 0
    assert parse_duration("") is None and parse_duration(None) is None and parse_duration("P") is None


def test_youtube_detects_shorts(tmp_path):
    search = {"items": [
        {"id": {"videoId": v}, "snippet": {"publishedAt": "2026-09-29T10:00:00Z", "title": v}} for v in ("s1", "s2", "long", "unk")
    ]}
    videos = {"items": [
        {"id": "s1", "contentDetails": {"duration": "PT40S"}, "statistics": {}},
        {"id": "s2", "contentDetails": {"duration": "PT2M"}, "statistics": {}},     # 2분이지만 일반 영상
        {"id": "long", "contentDetails": {"duration": "PT12M"}, "statistics": {}},
        {"id": "unk", "contentDetails": {"duration": "PT50S"}, "statistics": {}},   # 확인 실패 → 길이로 판단
    ]}
    shorts = "https://www.youtube.com/shorts/{}"
    session = FakeSession([FakeResponse(200, search), FakeResponse(200, videos)],
                          heads={shorts.format("s1"): 200, shorts.format("s2"): 303, shorts.format("unk"): 500})
    posts = {p.post_id: p for p in YouTubeClient("KEY", session=session).search("x", now_kst(), 5)}
    assert [(posts[k].is_short, posts[k].duration) for k in ("yt:s1", "yt:s2", "yt:long", "yt:unk")] == [
        (True, 40), (False, 120), (False, 720), (True, 50)]
    assert posts["yt:s1"].url == shorts.format("s1") and "watch?v=long" in posts["yt:long"].url
    assert "contentDetails" in session.calls[1][1]["part"]
    assert not any(url == shorts.format("long") for url, _ in session.calls)  # 긴 영상은 확인하지 않음


def test_shorts_filter_and_update(web):
    state, client = web
    state.store.upsert_social(_post("yt:9", title="닥사렌 숏츠", url="https://www.youtube.com/shorts/9",
                                    duration=30, is_short=True), "닥사렌")
    state.store.classify_social(social_matcher(state.cfg))
    ids = lambda qs: [i["post_id"] for i in client.get("/api/social?platform=youtube" + qs).get_json()["items"]]
    assert set(ids("")) == {"yt:1", "yt:9"}
    assert ids("&shorts=only") == ["yt:9"]
    assert ids("&shorts=exclude") == ["yt:1"]
    item = next(i for i in client.get("/api/social?platform=youtube&shorts=only").get_json()["items"])
    assert item["is_short"] == 1 and item["duration"] == 30
    # 예전에 저장된 영상도 다시 찾으면 숏츠 정보와 숏츠 주소가 채워짐
    state.store.upsert_social(_post("yt:1", title="닥사렌 후기", url="https://www.youtube.com/shorts/1", duration=20, is_short=True), "닥사렌")
    post = state.store.get_social("yt:1")
    assert post["is_short"] == 1 and post["url"].endswith("/shorts/1")


def test_old_db_gets_shorts_columns(tmp_path):
    import sqlite3
    db = tmp_path / "db.sqlite"
    Store(db)
    with sqlite3.connect(db) as c:  # 숏츠 열이 없던 예전 DB 흉내
        c.execute("ALTER TABLE social_posts DROP COLUMN is_short")
        c.execute("ALTER TABLE social_posts DROP COLUMN duration")
    store = Store(db)
    store.upsert_social(_post("yt:1", title="x", duration=10, is_short=True), "q")
    assert store.get_social("yt:1")["is_short"] == 1
