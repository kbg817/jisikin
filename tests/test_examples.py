import shutil

import pytest

from jisikin.config import EXAMPLE_CONFIG_PATH
from jisikin.naver import RawQuestion
from jisikin.storage import Store

H = {"X-Jisikin": "1"}


@pytest.fixture
def web(tmp_path):
    from jisikin.matcher import Matcher
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env", data_dir=tmp_path)
    for doc_id, title in [("11", "두피 건선 크림 추천해주세요"), ("12", "아기 이름 좀 지어주세요")]:
        state.store.upsert_raw(RawQuestion(doc_id=doc_id, url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title), "검색:t")
    state.store.classify(Matcher(state.cfg.products))
    return state, create_app(state).test_client()


def test_answered_draft_becomes_example_candidate(web):
    state, client = web
    state.store.set_draft("11", "AI 초안: 보습이 중요합니다.")
    # 초안을 그대로 올림 → '고침' 아님
    client.post("/api/questions/11/status", json={"status": "answered", "draft": "AI 초안: 보습이 중요합니다."}, headers=H)
    ex = state.store.list_examples("daksaren")
    assert len(ex) == 1 and ex[0]["edited"] == 0 and ex[0]["source"] == "final" and ex[0]["title"] == "두피 건선 크림 추천해주세요"

    # 할 일로 되돌리면 ⭐ 안 한 후보는 지워짐
    client.post("/api/questions/11/status", json={"status": "opened"}, headers=H)
    assert state.store.list_examples("daksaren") == []

    # 고친 내용을 답변완료와 함께 보내면 저장되고 '고침'으로 표시
    client.post("/api/questions/11/status", json={"status": "answered", "draft": "고친 최종 답변입니다."}, headers=H)
    ex = state.store.list_examples("daksaren")
    assert ex[0]["answer"] == "고친 최종 답변입니다." and ex[0]["edited"] == 1
    assert state.store.get("11")["draft"] == "고친 최종 답변입니다."

    # ⭐ 한 예시는 되돌려도 남음, 다시 답변완료해도 중복되지 않음
    state.store.set_example_star(ex[0]["id"], True)
    client.post("/api/questions/11/status", json={"status": "opened"}, headers=H)
    client.post("/api/questions/11/status", json={"status": "answered"}, headers=H)
    assert len(state.store.list_examples("daksaren")) == 1

    # 초안 없이 답변완료하면 아무것도 남기지 않음
    client.post("/api/questions/12/status", json={"status": "answered"}, headers=H)
    assert state.store.list_examples("myeongun") == []


def test_draft_save_endpoint(web):
    state, client = web
    assert client.post("/api/questions/11/draft/save", json={"draft": "수정본"}, headers=H).get_json() == {"ok": True}
    assert state.store.get("11")["draft"] == "수정본" and state.store.get("11")["draft_edited"] == 1
    assert client.post("/api/questions/nope/draft/save", json={"draft": "x"}, headers=H).status_code == 404
    assert client.post("/api/questions/11/draft/save", json={"draft": 3}, headers=H).status_code == 400
    assert client.post("/api/questions/11/draft/save", json={"draft": "x"}).status_code == 403  # 헤더 없으면 거부


def test_examples_api_star_limit_edit_delete(web):
    state, client = web
    assert client.get("/examples").status_code == 200
    ids = []
    for i in range(5):
        r = client.post("/api/examples/add", json={"product": "myeongun", "answer": f"작명 예시 답변 {i}번입니다. 발음과 뜻을 함께 봅니다."}, headers=H).get_json()
        assert r["starred"] is True
        ids.append(r["id"])
    r = client.post("/api/examples/add", json={"product": "myeongun", "title": "개명", "answer": "여섯 번째 답변은 후보로만 들어갑니다."}, headers=H).get_json()
    assert r["starred"] is False
    sixth = r["id"]
    r = client.post(f"/api/examples/{sixth}/star", json={"starred": True}, headers=H)
    assert r.status_code == 400 and "5개" in r.get_json()["error"]
    client.post(f"/api/examples/{ids[0]}/star", json={"starred": False}, headers=H)
    assert client.post(f"/api/examples/{sixth}/star", json={"starred": True}, headers=H).get_json() == {"ok": True}

    data = client.get("/api/examples").get_json()
    mg = next(p for p in data["products"] if p["id"] == "myeongun")
    assert mg["starred"] == {"kin": 5, "youtube": 0} and data["max"] == 5
    assert [p["name"] for p in data["products"]] == ["신의소리", "명연당", "명운연구소", "닥사렌 모각크림", "음파쑥쑥", "치디핏", "세이프맘 탄소매트"]

    assert client.post(f"/api/examples/{sixth}/update", json={"answer": "짧음"}, headers=H).status_code == 400
    assert client.post(f"/api/examples/{sixth}/update", json={"answer": "다듬은 여섯 번째 답변입니다. 법원 절차도 안내합니다."}, headers=H).get_json() == {"ok": True}
    assert state.store.get_example(sixth)["answer"].startswith("다듬은")
    assert client.post(f"/api/examples/{sixth}/delete", json={}, headers=H).get_json() == {"ok": True}
    assert client.post(f"/api/examples/{sixth}/delete", json={}, headers=H).status_code == 404
    assert client.post("/api/examples/add", json={"product": "nope", "answer": "x" * 30}, headers=H).status_code == 404
    assert client.post("/api/examples/add", json={"product": "sinui", "answer": "짧아요"}, headers=H).status_code == 400


def test_starred_examples_are_stable_and_capped():
    store = Store(":memory:")
    ids = [store.add_example("p", f"답변 {i}") for i in range(4)]
    assert [e["id"] for e in store.starred_examples("p", 3)] == ids[1:]  # 최근 ⭐ 3개, 오래된 순
    store.set_example_star(ids[2], False)
    assert [e["id"] for e in store.starred_examples("p", 3)] == [ids[0], ids[1], ids[3]]


def test_settings_page_shows_ai_usage(web):
    from datetime import timedelta
    from types import SimpleNamespace

    from jisikin.drafter import record_usage
    from jisikin.storage import now_kst

    state, client = web
    u = SimpleNamespace(input_tokens=2000, output_tokens=1000, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    for _ in range(3):
        record_usage(state.store, "draft", "claude-sonnet-5-5", u)
    record_usage(state.store, "draft", "claude-sonnet-5-5", u, now=now_kst().replace(day=1) - timedelta(days=1))
    page = client.get("/settings").get_data(as_text=True)
    assert "AI 사용량" in page and "claude-sonnet-5-5" in page and "생각 깊이 low" in page
    assert "$0.04" in page and "답변 초안 3회" in page  # 3 × ($0.004 + $0.01)
    assert "Claude 구독(Pro·Max)과는 별개" in page
    assert "$0.01" in page  # 지난달 1회


def test_youtube_examples_are_separate_from_kin(tmp_path):
    from jisikin.drafter import build_social_prompt
    from jisikin.social import SocialPost, social_matcher
    from jisikin.storage import Store

    store = Store(tmp_path / "db.sqlite")
    store.add_example("daksaren", "지식iN 답변 예시입니다. 모공각화증은 보습이 중요해요. 길게 설명합니다.")
    store.add_example("daksaren", "영상 잘 봤어요! 저도 보습 루틴 따라 해볼게요 🙂", title="모공각화증 루틴", channel="youtube")
    assert [e["channel"] for e in store.starred_examples("daksaren", 5)] == ["kin"]
    yt = store.starred_examples("daksaren", 5, channel="youtube")
    assert len(yt) == 1 and store.starred_count("daksaren", "youtube") == 1

    # 댓글완료한 유튜브 댓글은 유튜브 예시 후보로 쌓임
    store.upsert_social(SocialPost(platform="youtube", post_id="yt:v1", url="https://y/v1", title="닥사렌 후기",
                                   body="모공각화증 관리 영상"), "닥사렌")
    store.save_social_draft_edit("yt:v1", "좋은 정보 감사합니다. 저는 닥사렌 운영하는 사람인데요, 보습이 정말 중요해요.")
    ex_id = store.capture_final_comment("yt:v1", "daksaren", by="kim")
    ex = store.get_example(ex_id)
    assert ex["channel"] == "youtube" and ex["starred"] == 0 and ex["title"] == "닥사렌 후기"
    assert next(e for e in store.list_examples() if e["id"] == ex_id)["url"] == "https://y/v1"


def test_social_prompt_uses_youtube_examples(example_cfg):
    from jisikin.drafter import build_social_prompt

    product = example_cfg.product("daksaren")
    post = {"platform": "youtube", "title": "건선 관리", "body": "설명"}
    ex = [{"title": "모공각화증 루틴", "question": "", "answer": "영상 잘 봤어요! 보습 루틴 따라 해볼게요"}]
    system, _ = build_social_prompt(example_cfg, product, post, ex)
    assert "좋은 댓글 예시" in system and "보습 루틴 따라 해볼게요" in system and "[영상]" in system
    assert "좋은 댓글 예시" not in build_social_prompt(example_cfg, product, post)[0]
