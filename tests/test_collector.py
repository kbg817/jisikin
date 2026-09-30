from datetime import timedelta

from jisikin.collector import collect, effective_interval, estimate_api_calls_per_day
from jisikin.naver import NaverError, QuestionDetail, RawQuestion
from jisikin.storage import Store, now_kst, priority


def rq(doc_id, title, snippet=""):
    return RawQuestion(doc_id=str(doc_id), url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title, snippet=snippet)


class FakeClient:
    """네이버 대신 쓰는 가짜 클라이언트: 검색어 → 결과 목록."""

    def __init__(self, results, details=None, credentials=("id", "secret"), fail_queries=()):
        self.results = results
        self.details = details or {}
        self.credentials = credentials
        self.fail_queries = set(fail_queries)
        self.api_calls, self.web_calls, self.detail_calls = [], [], []

    def search_api(self, query, count=50):
        self.api_calls.append(query)
        if query in self.fail_queries:
            raise NaverError("테스트 오류")
        return self.results.get(query, [])

    def search_web(self, query, page=1):
        self.web_calls.append((query, page))
        items = self.results.get(query, [])
        return items[(page - 1) * 10 : page * 10]

    def fetch_list(self, url):
        return []

    def fetch_detail(self, url):
        doc_id = url.rsplit("=", 1)[1]
        self.detail_calls.append(doc_id)
        if doc_id not in self.details:
            raise NaverError("없음")
        return self.details[doc_id]


def test_collect_saves_classifies_and_enriches(example_cfg, monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    store = Store(":memory:")
    results = {
        "타로": [rq(1001, "타로 재회운 봐주세요"), rq(1000, "타로카드 게임 공략")],
        "재회 타로": [rq(1001, "타로 재회운 봐주세요"), rq(999, "전남친 연락 올까요")],
        "건선": [rq(998, "두피 건선 샴푸 추천")],
    }
    details = {
        "1001": QuestionDetail(body="헤어진 지 두 달이에요", answer_count=0, asked_at=now_kst() - timedelta(minutes=30), reward=30),
        "999": QuestionDetail(body="전남친한테 연락이 올까요?", answer_count=3),
    }
    client = FakeClient(results, details)
    s = collect(example_cfg, store, client=client, log=lambda m: None)

    assert s.mode == "api"
    assert s.queries == len(example_cfg.all_search_queries())
    assert s.new_total == 4
    assert s.new_relevant == 4  # 제목에 '타로' 가 있으면 관련 질문 (게임 질문은 제외어로 걸러야 함)
    assert store.get("999")["product"] == "sinui"  # '전남친 연락' 같은 재회 고민도 신의소리
    assert s.details == 2 and not s.errors
    q = store.get("1001")
    assert q["product"] == "sinui" and "재회운" in q["categories"]
    assert q["answer_count"] == 0 and q["reward"] == 30 and q["body"]
    assert len(q["sources"]) == 2  # 두 검색어에서 모두 발견
    assert store.get("998")["product"] == "daksaren"
    assert set(client.detail_calls) >= {"1001", "999", "998"}
    assert store.get("998")["detail_error"]  # 상세 실패도 기록 (다음에 재시도하지 않음)

    # 두 번째 실행: 새 질문 없음, 이미 가져온 상세는 다시 안 가져옴
    client.detail_calls.clear()
    s2 = collect(example_cfg, store, client=client, log=lambda m: None)
    assert s2.new_total == 0 and client.detail_calls == []
    runs = store.recent_runs()
    assert len(runs) == 2 and runs[0]["new_total"] == 0


def test_collect_records_errors_and_stops_on_fatal(example_cfg, monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    store = Store(":memory:")
    first = example_cfg.all_search_queries()[0]

    class FatalClient(FakeClient):
        def search_api(self, query, count=50):
            self.api_calls.append(query)
            raise NaverError("인증 실패", fatal=True)

    client = FatalClient({})
    s = collect(example_cfg, store, client=client, log=lambda m: None)
    assert client.api_calls == [first]
    assert s.errors and "인증 실패" in s.errors[0]
    assert store.recent_runs()[0]["errors"]


def test_web_mode_pages_until_no_new(example_cfg):
    example_cfg.settings.source = "web"
    store = Store(":memory:")
    many = [rq(5000 - i, f"타로 질문 {i}") for i in range(25)]
    client = FakeClient({"타로": many}, credentials=None)
    collect(example_cfg, store, client=client, log=lambda m: None)
    pages = [p for q, p in client.web_calls if q == "타로"]
    assert pages == [1, 2, 3]
    client.web_calls.clear()
    collect(example_cfg, store, client=client, log=lambda m: None)
    assert [p for q, p in client.web_calls if q == "타로"] == [1]  # 1페이지가 전부 본 것이면 멈춤


def test_web_mode_enforces_minimum_interval(example_cfg):
    example_cfg.settings.source = "web"
    example_cfg.settings.interval_minutes = 5
    assert effective_interval(example_cfg) == 30
    example_cfg.settings.interval_minutes = 0
    assert effective_interval(example_cfg) == 0


def test_api_call_estimate(example_cfg, monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    example_cfg.settings.interval_minutes = 10
    n = len(example_cfg.all_search_queries())
    assert estimate_api_calls_per_day(example_cfg) == n * (1440 // effective_interval(example_cfg))
    assert estimate_api_calls_per_day(example_cfg) < 25000


def test_list_filters_and_status(example_cfg, monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    example_cfg.settings.fetch_details = False
    store = Store(":memory:")
    client = FakeClient({
        "사주": [rq(10, "사주 궁합 봐주세요"), rq(11, "타로 재회운 궁금해요"), rq(12, "옷 사주세요")],
        "건선": [rq(13, "건선 크림 추천")],
    })
    collect(example_cfg, store, client=client, log=lambda m: None)

    ids = lambda rows: sorted(r["doc_id"] for r in rows)  # noqa: E731
    assert ids(store.list_questions()) == ["10", "11", "13"]
    assert ids(store.list_questions(product="myeongyeon")) == ["10"]
    assert ids(store.list_questions(product="sinui")) == ["11"]
    assert ids(store.list_questions(product="sinui", category="재회운")) == ["11"]
    # '옷 사주세요' 는 제외어로 가려져 점수 0 → 관련도 낮은 목록에도 안 보임
    assert store.get("12")["product"] is None
    assert "12" not in ids(store.list_questions(include_low=True))

    store.set_status("10", "answered")
    store.set_status("13", "skipped")
    assert ids(store.list_questions()) == ["11"]
    assert ids(store.list_questions(status="answered")) == ["10"]
    counts = store.todo_counts()
    assert counts["products"]["sinui"]["total"] == 1
    assert counts["products"]["sinui"]["categories"] == {"재회운": 1}
    assert counts["answered_today"] == 1


def test_priority_prefers_unanswered_and_recent():
    now = now_kst()
    base = {"score": 3, "first_seen": now.isoformat(), "asked_at": None, "reward": None}
    fresh_zero = priority({**base, "answer_count": 0, "asked_at": (now - timedelta(minutes=10)).isoformat()}, now)
    old_many = priority({**base, "answer_count": 6, "asked_at": (now - timedelta(days=9)).isoformat()}, now)
    assert fresh_zero > old_many
    unknown_date = priority({**base, "answer_count": 0}, now)
    assert fresh_zero > unknown_date  # 작성일이 확인된 최신 질문이 우선


def test_purge_keeps_answered(example_cfg):
    store = Store(":memory:")
    old = now_kst() - timedelta(days=100)
    store.upsert_raw(rq(1, "사주 질문"), "검색:사주", now=old)
    store.upsert_raw(rq(2, "사주 질문2"), "검색:사주", now=old)
    store.set_status("2", "answered")
    store.purge(60)
    assert store.get("1") is None and store.get("2") is not None


def test_collect_stops_after_repeated_connection_failures(example_cfg):
    example_cfg.settings.source = "web"
    store = Store(":memory:")

    class DownClient(FakeClient):
        def search_web(self, query, page=1):
            self.web_calls.append((query, page))
            raise NaverError("연결 실패")

    client = DownClient({}, credentials=None)
    s = collect(example_cfg, store, client=client, log=lambda m: None)
    assert len(client.web_calls) == 3
    assert "연속 실패" in s.errors[-1]
