import shutil

import pytest

from jisikin.config import EXAMPLE_CONFIG_PATH
from jisikin.naver import RawQuestion
from jisikin.web import AppState, create_app

H = {"X-Jisikin": "1"}


@pytest.fixture
def app_state(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite")
    for doc_id, title in [("101", "사주 재회운 봐주세요"), ("102", "두피 건선 크림 추천"), ("103", "노트북 추천")]:
        state.store.upsert_raw(RawQuestion(doc_id=doc_id, url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title), "검색:test")
    from jisikin.matcher import Matcher

    state.store.classify(Matcher(state.cfg.products))
    return state


@pytest.fixture
def client(app_state):
    return create_app(app_state).test_client()


def test_pages_render(client):
    assert "지식iN 질문 수집기" in client.get("/").get_data(as_text=True)
    page = client.get("/settings").get_data(as_text=True)
    assert "config.yaml" in page and "신의소리" in page


def test_meta_and_questions(client):
    meta = client.get("/api/meta").get_json()
    assert [p["name"] for p in meta["products"]] == ["신의소리", "음파쑥쑥", "닥사렌 모각크림"]
    assert meta["counts"]["products"]["sinui"]["total"] == 1
    assert meta["mode"] == "web" and meta["ai"]["enabled"] is False
    items = client.get("/api/questions").get_json()["items"]
    assert {i["doc_id"] for i in items} == {"101", "102"}
    low = client.get("/api/questions?include_low=1").get_json()["items"]
    assert {i["doc_id"] for i in low} == {"101", "102"}  # 노트북은 어떤 제품과도 점수 0
    only = client.get("/api/questions?product=sinui&category=재회운").get_json()["items"]
    assert [i["doc_id"] for i in only] == ["101"]


def test_status_requires_header(client, app_state):
    assert client.post("/api/questions/101/status", json={"status": "answered"}).status_code == 403
    r = client.post("/api/questions/101/status", json={"status": "answered"}, headers=H)
    assert r.get_json() == {"ok": True}
    assert app_state.store.get("101")["status"] == "answered"
    assert client.post("/api/questions/101/status", json={"status": "bogus"}, headers=H).status_code == 400
    assert client.post("/api/questions/999/status", json={"status": "answered"}, headers=H).status_code == 404


def test_draft_without_key_explains(client):
    r = client.post("/api/questions/101/draft", json={}, headers=H)
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.get_json()["error"]


def test_classify_test_endpoint(client):
    r = client.post("/api/classify-test", json={"title": "팔 오돌토돌 모공각화증"}, headers=H).get_json()
    assert r["matches"][0]["product_name"] == "닥사렌 모각크림"


def _csrf(client):
    page = client.get("/settings").get_data(as_text=True)
    return page.split('name="csrf" value="')[1].split('"')[0]


def test_settings_save_rejects_invalid_and_keeps_file(client, app_state):
    before = app_state.config_path.read_text(encoding="utf-8")
    r = client.post("/settings", data={"csrf": _csrf(client), "config": "products: [\n  - name"})
    assert "저장하지 못했습니다" in r.get_data(as_text=True)
    assert app_state.config_path.read_text(encoding="utf-8") == before


def test_settings_save_reclassifies(client, app_state):
    text = app_state.config_path.read_text(encoding="utf-8").replace(
        "keywords: [신점, 타로, 사주,", "keywords: [신점, 타로, 노트북, 사주,"
    )
    r = client.post("/settings", data={"csrf": _csrf(client), "config": text})
    assert r.status_code == 302
    assert app_state.store.get("103")["product"] == "sinui"


def test_settings_requires_csrf(client):
    assert client.post("/settings", data={"config": "x"}).status_code == 403


def test_collect_trigger(client, app_state, monkeypatch):
    calls = []
    monkeypatch.setattr(app_state, "run_collection", lambda: calls.append(1))
    r = client.post("/api/collect", json={}, headers=H).get_json()
    assert r["started"] is True
