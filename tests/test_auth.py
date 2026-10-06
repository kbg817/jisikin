import shutil

import pytest

from jisikin.auth import LoginLimiter
from jisikin.config import EXAMPLE_CONFIG_PATH
from jisikin.naver import RawQuestion

H = {"X-Jisikin": "1"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("JISIKIN_ADMIN_PASSWORD", "보스비번!123")
    monkeypatch.setenv("JISIKIN_ADMIN_USER", "boss")
    from jisikin.matcher import Matcher
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env", data_dir=tmp_path)
    for doc_id, title in [("1", "타로 재회운 봐주세요"), ("2", "두피 건선 크림 추천")]:
        state.store.upsert_raw(RawQuestion(doc_id=doc_id, url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title), "검색:t")
    state.store.classify(Matcher(state.cfg.products))
    app = create_app(state, behind_proxy=True)
    return state, app


def csrf_of(client, path="/login"):
    page = client.get(path).get_data(as_text=True)
    return page.split('name="csrf" value="')[1].split('"')[0]


def login(client, username, password):
    return client.post("/login", data={"csrf": csrf_of(client), "username": username, "password": password})


def test_requires_login(server):
    _, app = server
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert c.get("/api/meta").status_code == 401
    assert c.get("/healthz").get_json() == {"ok": True}
    assert "로그인" in c.get("/login").get_data(as_text=True)


def test_admin_login_and_staff_management(server):
    state, app = server
    admin = app.test_client()
    assert login(admin, "boss", "틀린비번").status_code == 200
    r = login(admin, "BOSS", "보스비번!123")  # 아이디 대소문자 무시, 한글 비밀번호도 됨
    assert r.status_code == 302
    meta = admin.get("/api/meta").get_json()
    assert meta["user"]["role"] == "admin" and meta["auth"] is True
    assert "검색어 관리" in admin.get("/").get_data(as_text=True)

    tok = csrf_of(admin, "/settings")
    r = admin.post("/settings/users", data={"csrf": tok, "action": "add", "username": "kim", "name": "김직원", "password": "pw1234"})
    assert "users_saved" in r.headers["Location"]
    r = admin.post("/settings/users", data={"csrf": tok, "action": "add", "username": "kim", "name": "중복", "password": "pw1234"})
    assert "user_error" in r.headers["Location"]
    r = admin.post("/settings/users", data={"csrf": tok, "action": "add", "username": "lee", "name": "이", "password": "123"})
    assert "user_error" in r.headers["Location"]  # 비밀번호 너무 짧음
    assert [u["username"] for u in state.store.list_users()] == ["kim"]

    # 직원 로그인 → 대시보드는 되지만 설정/검색어 관리는 안 됨
    staff = app.test_client()
    assert login(staff, "kim", "pw1234").status_code == 302
    assert staff.get("/").status_code == 200
    assert "검색어 관리" not in staff.get("/").get_data(as_text=True)
    assert staff.get("/settings").status_code == 403
    assert staff.get("/keywords").status_code == 403
    assert staff.get("/api/keywords").status_code == 403
    assert staff.post("/api/diagnose", json={}, headers=H).status_code == 403
    assert staff.get("/examples").status_code == 403
    assert staff.get("/api/examples").status_code == 403
    assert staff.post("/api/examples/add", json={"product": "sinui", "answer": "x" * 30}, headers=H).status_code == 403
    assert "답변 예시" not in staff.get("/").get_data(as_text=True)

    # 직원이 초안을 고쳐서 답변완료 → 누가 했는지 기록 + 실적 + 답변 예시 후보
    state.store.set_draft("1", "AI 가 쓴 초안입니다.")
    final = "직원이 다듬은 최종 답변입니다. 재회운은 서로의 마음이 중요해요."
    assert staff.post("/api/questions/1/draft/save", json={"draft": final}, headers=H).get_json() == {"ok": True}
    assert staff.post("/api/questions/1/status", json={"status": "answered"}, headers=H).get_json() == {"ok": True}
    assert state.store.get("1")["status_by"] == "kim"
    ex = state.store.list_examples("sinui")
    assert [(e["answer"], e["edited"], e["created_by"], e["starred"]) for e in ex] == [(final, 1, "kim", 0)]
    admin_items = admin.get("/api/examples").get_json()["items"]
    assert admin_items[0]["created_by_name"] == "김직원" and admin_items[0]["url"].endswith("docId=1")
    meta = staff.get("/api/meta").get_json()
    assert meta["people"]["kim"] == "김직원"
    assert meta["answer_stats"] == [{"username": "kim", "name": "김직원", "today": 1, "week": 1, "total": 1}]
    items = admin.get("/api/questions?status=answered").get_json()["items"]
    assert items[0]["status_by"] == "kim"

    # 직원 정지 → 바로 로그아웃됨
    admin.post("/settings/users", data={"csrf": tok, "action": "toggle", "username": "kim"})
    assert staff.get("/api/meta").status_code == 401
    assert login(app.test_client(), "kim", "pw1234").status_code == 200  # 로그인 실패 (폼 다시 보여줌)

    # 비밀번호 변경 후 다시 사용
    admin.post("/settings/users", data={"csrf": tok, "action": "toggle", "username": "kim"})
    admin.post("/settings/users", data={"csrf": tok, "action": "password", "username": "kim", "password": "newpw99"})
    assert login(app.test_client(), "kim", "newpw99").status_code == 302

    admin.post("/settings/users", data={"csrf": tok, "action": "delete", "username": "kim"})
    assert state.store.list_users() == []


def test_answer_counted_once_even_if_clicked_twice(server):
    state, _ = server
    state.store.set_status("2", "answered", by="kim")
    state.store.set_status("2", "opened", by="kim")
    state.store.set_status("2", "answered", by="lee")
    stats = state.store.answer_stats()
    assert stats == {"lee": {"today": 1, "week": 1, "total": 1}}


def test_logout_and_open_redirect(server):
    _, app = server
    c = app.test_client()
    tok = csrf_of(c)
    r = c.post("/login?next=//evil.example.com", data={"csrf": tok, "username": "boss", "password": "보스비번!123"})
    assert r.headers["Location"] == "/"
    tok = csrf_of(c, "/settings")
    assert c.post("/logout", data={"csrf": tok}).status_code == 302
    assert c.get("/api/meta").status_code == 401


def test_login_limiter():
    lim = LoginLimiter(max_failures=3, window_seconds=60)
    for _ in range(3):
        assert not lim.blocked("ip")
        lim.fail("ip")
    assert lim.blocked("ip")
    lim.reset("ip")
    assert not lim.blocked("ip")


def test_login_requires_csrf(server):
    _, app = server
    c = app.test_client()
    assert c.post("/login", data={"username": "boss", "password": "보스비번!123"}).status_code == 403


def test_local_mode_has_no_login(tmp_path, monkeypatch):
    monkeypatch.delenv("JISIKIN_ADMIN_PASSWORD", raising=False)
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    app = create_app(AppState(cfg_path, tmp_path / "db.sqlite", data_dir=tmp_path))
    c = app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/settings").status_code == 200
    assert c.get("/login").status_code == 302
    assert c.get("/api/meta").get_json()["auth"] is False


def test_public_serve_requires_password(monkeypatch, capsys):
    monkeypatch.delenv("JISIKIN_ADMIN_PASSWORD", raising=False)
    from jisikin.__main__ import main

    assert main(["serve", "--host", "0.0.0.0", "--no-browser"]) == 1
    assert "JISIKIN_ADMIN_PASSWORD" in capsys.readouterr().err


def test_daily_goal_per_staff(server):
    """상단에 직원마다 '이름 오늘완료/목표' (0건이어도, 꺼진 계정은 빼고)."""
    from jisikin.auth import hash_password

    state, app = server
    state.store.save_user("kim", "정소연", hash_password("pw1234"))
    state.store.save_user("lee", "염영주", hash_password("pw1234"))
    state.store.save_user("old", "퇴사자", hash_password("pw1234"), active=False)
    staff = app.test_client()
    login(staff, "kim", "pw1234")
    assert staff.post("/api/questions/1/status", json={"status": "answered"}, headers=H).get_json() == {"ok": True}
    goals = staff.get("/api/meta").get_json()["goals"]
    assert [(g["name"], g["today"], g["goal"]) for g in goals] == [("정소연", 1, 30), ("염영주", 0, 30)]
