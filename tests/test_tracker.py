import shutil
from datetime import timedelta

import pytest

from jisikin.config import EXAMPLE_CONFIG_PATH
from jisikin.naver import NaverError
from jisikin.social import SocialError, SocialPost
from jisikin.storage import Store, iso, now_kst
from jisikin.tracker import (
    ERROR,
    GONE,
    MISSING,
    NO_TEXT,
    VISIBLE,
    check_answers,
    check_kin_page,
    same_text,
    search_phrase,
    text_found,
)
from jisikin.web import AppState, create_app

MINE = "모공각화증은 보습이 가장 중요해요. 샤워 후 3분 안에 보습제를 발라 주세요!\n저는 닥사렌 판매자인데, 요소 성분 크림도 도움이 됩니다."
H = {"X-Jisikin": "1"}


def test_text_matching_tolerates_small_edits():
    edited = "모공각화증은 보습이 제일 중요해요 ㅎㅎ 샤워 후 3분 안에 보습제를 발라 주세요~ 저는 닥사렌 판매자인데 요소 성분 크림도 도움이 됩니다"
    assert same_text(MINE, edited)
    assert text_found(MINE, "<div>" + MINE.replace("\n", " ") + "</div>")
    assert not same_text(MINE, "건선에는 햇빛이 좋아요. 병원 가보세요.")
    assert not text_found("", "아무 글")
    assert search_phrase(MINE).startswith("저는 닥사렌 판매자인데")


def kin_page(answers, extra=""):
    items = "".join(
        f'<div class="answer-content__item">{"<span>질문자 채택</span>" if adopted else ""}<div>{text}</div></div>'
        for text, adopted in answers
    )
    return f"<html><body><div class='c-heading'>질문</div>{extra}<div>{items}</div></body></html>"


def test_check_kin_page():
    assert check_kin_page(kin_page([("다른 답변입니다 병원에 가보세요", False), (MINE, True)]), MINE) == (VISIBLE, True)
    assert check_kin_page(kin_page([(MINE, False)]), MINE) == (VISIBLE, False)
    assert check_kin_page(kin_page([("다른 사람 답변만 남았어요 병원에 가보세요", False)]), MINE) == (MISSING, None)
    assert check_kin_page("<html><body>삭제되었거나 존재하지 않는 질문입니다.</body></html>", MINE)[0] == GONE
    # 답변 칸도 답변 수도 못 읽으면 '안 보임'으로 단정하지 않음
    assert check_kin_page("<html><body><div id='app'></div></body></html>", MINE)[0] == ERROR


class FakeNaver:
    def __init__(self, pages):
        self.pages = pages

    def _get_html(self, url):
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


class FakeYouTube:
    def __init__(self, comments, search=None, error=None):
        self.comments, self.search, self.error = comments, search or [], error
        self.calls = []

    def top_comments(self, vid, search=""):
        self.calls.append((vid, search))
        if self.error:
            raise self.error
        return self.search if search else self.comments.get(vid, [])


def _cm(text, likes=0):
    return {"text": text, "author": "누군가", "likes": likes, "replies": 0}


@pytest.fixture
def store_with_work(tmp_path):
    from jisikin.naver import RawQuestion

    store = Store(tmp_path / "db.sqlite")
    for doc in ("1", "2", "3"):
        store.upsert_raw(RawQuestion(doc_id=doc, url=f"https://kin/{doc}", title=f"질문 {doc}"), "모공각화증")
    for pid in ("yt:a", "yt:b", "yt:c", "th:x"):
        store.upsert_social(SocialPost(platform=pid[:2] == "yt" and "youtube" or "threads", post_id=pid,
                                       url=f"https://y/{pid}", title=pid), "q")
    for doc in ("1", "2", "3"):
        store.set_status(doc, "answered", by="kim")
    for pid in ("yt:a", "yt:b", "yt:c", "th:x"):
        store.set_social_status(pid, "answered", by="lee")
    for item in ("1", "2"):
        store.save_draft_edit(item, MINE)
    for item in ("yt:a", "yt:b", "yt:c", "th:x"):
        store.save_social_draft_edit(item, MINE)
    return store


def test_check_answers(store_with_work, tmp_path):
    store = store_with_work
    shutil.copyfile(EXAMPLE_CONFIG_PATH, tmp_path / "config.yaml")
    from jisikin.config import load_config

    cfg = load_config(tmp_path / "config.yaml")
    naver = FakeNaver({"https://kin/1": kin_page([(MINE, True)]), "https://kin/2": kin_page([("다른 답변 병원에 가보세요", False)])})
    yt = FakeYouTube(
        {"a": [_cm("첫 댓글 좋아요 영상"), _cm(MINE, likes=7)], "b": [_cm("다른 댓글이에요 반가워요")] * 100},
        search=[_cm(MINE, likes=1)],
    )
    yt.comments["c"] = []  # 영상 c: 검색으로도 없음 → 아래에서 오류로 바꿔 봄
    s = check_answers(cfg, store, naver=naver, youtube=yt, log=lambda m: None)
    checks = store.latest_answer_checks()
    assert checks["1"]["state"] == VISIBLE and checks["1"]["adopted"] == 1
    assert checks["2"]["state"] == MISSING
    assert checks["3"]["state"] == NO_TEXT  # 저장된 답변 내용이 없음
    assert (checks["yt:a"]["state"], checks["yt:a"]["rank"], checks["yt:a"]["likes"]) == (VISIBLE, 2, 7)
    assert checks["yt:b"]["state"] == VISIBLE and checks["yt:b"]["rank"] is None and "100위 밖" in checks["yt:b"]["note"]
    assert "th:x" not in checks  # 쓰레드는 확인하지 않음
    assert s.checked == 5 and s.visible == 4 and s.missing == 1

    # 다음 날: 영상이 지워지고 댓글이 막히면
    yt2 = FakeYouTube({}, error=SocialError("x", reason="commentsDisabled"))
    naver2 = FakeNaver({"https://kin/1": NaverError("응답 오류 404"), "https://kin/2": NaverError("연결 실패")})
    s2 = check_answers(cfg, store, naver=naver2, youtube=yt2, log=lambda m: None)
    checks = store.latest_answer_checks()
    assert checks["1"]["state"] == GONE and checks["1"]["prev_state"] == VISIBLE
    assert checks["2"]["state"] == MISSING  # 확인 실패는 기록하지 않고 이전 결과 유지
    assert checks["yt:a"]["state"] == "comments_off"
    assert checks["yt:a"]["visible_days"] == 1
    assert len(s2.errors) == 1


def test_youtube_quota_stops_remaining(store_with_work, example_cfg):
    yt = FakeYouTube({}, error=SocialError("할당량", fatal=True))
    s = check_answers(example_cfg, store_with_work, naver=FakeNaver({"https://kin/1": kin_page([(MINE, False)]),
                                                                       "https://kin/2": kin_page([(MINE, False)])}),
                      youtube=yt, log=lambda m: None)
    assert len(yt.calls) == 1 and s.skipped >= 2
    assert store_with_work.recent_runs(1, mode="track")[0]["fetched"] == s.checked


def test_next_track_time(tmp_path):
    shutil.copyfile(EXAMPLE_CONFIG_PATH, tmp_path / "config.yaml")
    state = AppState(tmp_path / "config.yaml", tmp_path / "db.sqlite", env_path=tmp_path / ".env")
    now = now_kst().replace(hour=5, minute=0, second=0, microsecond=0)
    assert state.next_track_time(now) == now.replace(hour=6)
    later = now.replace(hour=9)
    assert state.next_track_time(later) == later  # 6시가 지났는데 오늘 아직 안 함 → 지금
    run = state.store.start_run("track")
    with state.store._conn() as c:  # 오늘 7시에 확인했다고 치면 다음은 내일 6시
        c.execute("UPDATE runs SET started_at=? WHERE id=?", (iso(now.replace(hour=7)), run))
    assert state.next_track_time(later) == now.replace(hour=6) + timedelta(days=1)
    state.cfg.settings.track_check_hour = -1
    assert state.next_track_time(later) is None


def test_results_api(store_with_work, tmp_path):
    shutil.copyfile(EXAMPLE_CONFIG_PATH, tmp_path / "config.yaml")
    state = AppState(tmp_path / "config.yaml", tmp_path / "db.sqlite", env_path=tmp_path / ".env")
    state.store.add_answer_check("yt:a", VISIBLE, rank=3, likes=5)
    state.run_track = lambda: None  # 테스트에서 실제 확인(네트워크)은 하지 않음
    client = create_app(state).test_client()
    items = {i["item_id"]: i for i in client.get("/api/results").get_json()["items"]}
    assert set(items) == {"1", "2", "3", "yt:a", "yt:b", "yt:c", "th:x"}
    assert items["yt:a"]["check"]["rank"] == 3 and items["1"]["check"] is None
    assert items["1"]["platform"] == "kin" and items["th:x"]["platform"] == "threads"
    meta = client.get("/api/meta").get_json()
    assert meta["track"]["hour"] == 6 and meta["track"]["next_at"]
    assert client.post("/api/results/check", json={}).status_code == 403
    assert client.post("/api/results/check", json={}, headers=H).get_json()["started"] is True


def test_kin_answer_position_and_likes():
    from jisikin.tracker import kin_answer_position

    def item(text, likes):
        return (f'<div class="answer-content__item"><div>{text}</div>'
                f'<button class="_recommendBtn"><span class="_recommendCount">{likes}</span></button></div>')

    other = "다른 업체 답변입니다. 저희 철학관으로 오세요. 이름은 수리가 중요합니다."
    html = ("<html><body><div class='c-heading'>질문</div><div>"
            + item(other, 12) + item(other + " 두번째", 3) + item(MINE, 2) + "</div></body></html>")
    assert kin_answer_position(html, MINE) == {"rank": 3, "total": 3, "likes": 2, "top_likes": 12}
    # 좋아요를 '좋아요 5' 글자로만 보여주는 화면
    html2 = f"<html><body><div class='answer-content__item'><div>{MINE}</div><span>좋아요 5</span></div></body></html>"
    assert kin_answer_position(html2, MINE) == {"rank": 1, "total": 1, "likes": 5}
    assert kin_answer_position("<html><body><div>없음</div></body></html>", MINE) == {}


def test_prev_rank_shows_drop(tmp_path):
    from jisikin.storage import Store

    store = Store(tmp_path / "db.sqlite")
    store.add_answer_check("1", "visible", rank=1, likes=3, total=4, top_likes=2)
    store.add_answer_check("1", "visible", rank=3, likes=3, total=5, top_likes=9)
    c = store.latest_answer_checks()["1"]
    assert (c["rank"], c["prev_rank"], c["total"], c["top_likes"]) == (3, 1, 5, 9)
