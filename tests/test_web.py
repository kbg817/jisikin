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
    for doc_id, title in [("101", "타로 재회운 봐주세요"), ("102", "두피 건선 크림 추천"), ("103", "노트북 추천")]:
        state.store.upsert_raw(RawQuestion(doc_id=doc_id, url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title), "검색:test")
    from jisikin.matcher import Matcher

    state.store.classify(Matcher(state.cfg.products))
    return state


@pytest.fixture
def client(app_state):
    return create_app(app_state).test_client()


def test_pages_render(client):
    assert "답변·댓글 센터" in client.get("/").get_data(as_text=True)
    page = client.get("/settings").get_data(as_text=True)
    assert "config.yaml" in page and "신의소리" in page


def test_meta_and_questions(client):
    meta = client.get("/api/meta").get_json()
    assert [p["name"] for p in meta["products"]] == ["신의소리", "명연당", "명운연구소", "닥사렌 모각크림", "음파쑥쑥", "치디핏"]
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
        "keywords: [신점, 타로, ", "keywords: [신점, 타로, 노트북, "
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


def test_save_keys_from_browser(client, app_state, tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    app_state.env_path = env_path
    monkeypatch.setattr(app_state, "trigger", lambda: True)
    r = client.post(
        "/settings/keys",
        data={"csrf": _csrf(client), "NAVER_CLIENT_ID": " abcID123 ", "NAVER_CLIENT_SECRET": "sec9876", "ANTHROPIC_API_KEY": ""},
    )
    assert r.status_code == 302 and "keys_saved=1" in r.headers["Location"]
    text = env_path.read_text(encoding="utf-8")
    assert "NAVER_CLIENT_ID=abcID123" in text and "NAVER_CLIENT_SECRET=sec9876" in text
    assert "# 네이버 검색 API" in text  # .env.example 의 안내 주석 유지
    import os

    assert os.environ["NAVER_CLIENT_ID"] == "abcID123"
    page = client.get("/settings").get_data(as_text=True)
    assert "abcID123" not in page and "저장됨 ••••D123" in page
    assert client.get("/api/meta").get_json()["mode"] == "api"  # 재시작 없이 바로 API 모드

    # 빈 칸은 유지, '삭제' 는 지움
    client.post("/settings/keys", data={"csrf": _csrf(client), "NAVER_CLIENT_SECRET": "삭제"})
    text = env_path.read_text(encoding="utf-8")
    assert "NAVER_CLIENT_ID=abcID123" in text and "NAVER_CLIENT_SECRET=\n" in text
    assert "NAVER_CLIENT_SECRET" not in os.environ


def test_save_keys_rejects_spaces_and_csrf(client, app_state, tmp_path):
    app_state.env_path = tmp_path / ".env"
    assert client.post("/settings/keys", data={"NAVER_CLIENT_ID": "x"}).status_code == 403
    r = client.post("/settings/keys", data={"csrf": _csrf(client), "NAVER_CLIENT_ID": "abc def"})
    assert "key_error" in r.headers["Location"]
    assert not (tmp_path / ".env").exists()


def test_diagnose_endpoint(client, monkeypatch):
    import jisikin.web as web

    monkeypatch.setattr(web, "run_diagnostics", lambda cfg, **kw: (False, ["[오류] 검색 실패: 테스트"]))
    assert client.post("/api/diagnose", json={}).status_code == 403
    r = client.post("/api/diagnose", json={}, headers=H).get_json()
    assert r == {"ok": False, "lines": ["[오류] 검색 실패: 테스트"]}


def test_exposure_check_runs_right_after_new_keywords(tmp_path, monkeypatch):
    import shutil
    from datetime import timedelta

    from jisikin.config import EXAMPLE_CONFIG_PATH
    from jisikin.keywords import Candidate, ExpandResult
    from jisikin.storage import now_kst
    from jisikin.web import AppState

    path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, path)
    state = AppState(path, tmp_path / "db.sqlite", data_dir=tmp_path)
    ran = []
    monkeypatch.setattr(state, "run_collection", lambda: ran.append("collect"))
    monkeypatch.setattr(state, "run_exposure", lambda: ran.append("exposure"))
    monkeypatch.setattr(
        state, "run_keywords",
        lambda pid: ran.append(f"keywords:{pid}") or ExpandResult(product=pid, candidates=[Candidate("건선", "건선", ["seed"], 1)]),
    )
    state.next_exposure_at = now_kst() + timedelta(hours=5)  # 원래라면 5시간 뒤
    state._tick()
    assert ran == ["collect", "keywords:sinui", "keywords:myeongyeon", "keywords:myeongun", "keywords:daksaren", "keywords:eumpa",
                   "keywords:chidifit", "exposure"]
