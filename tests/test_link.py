"""업무 데스크 연동 (link.py): 요약 숫자, 1회용 입장권 로그인, 직원 사용 중지."""
import base64
import hashlib
import hmac
import json
import shutil
import time

import pytest

from jisikin.config import EXAMPLE_CONFIG_PATH
from jisikin.naver import RawQuestion

SECRET = "업무데스크-연동-비밀값-테스트용-0123456789abcdef"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def ticket(secret=SECRET, **claims) -> str:
    """업무 데스크(jose SignJWT HS256)와 같은 모양의 입장권."""
    body = {"sub": "kim", "name": "김직원", "role": "staff", "aud": "tool-link", "jti": f"j{time.time_ns()}", "exp": int(time.time()) + 60}
    body.update(claims)
    head = b64(json.dumps({"alg": "HS256"}).encode())
    payload = b64(json.dumps(body, ensure_ascii=False).encode())
    sig = hmac.new(secret.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    return f"{head}.{payload}.{b64(sig)}"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("JISIKIN_ADMIN_PASSWORD", "보스비번!123")
    monkeypatch.setenv("JISIKIN_ADMIN_USER", "boss")
    monkeypatch.setenv("JISIKIN_LINK_SECRET", SECRET)
    from jisikin.matcher import Matcher
    from jisikin.web import AppState, create_app

    cfg_path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
    state = AppState(cfg_path, tmp_path / "db.sqlite", env_path=tmp_path / ".env", data_dir=tmp_path)
    for doc_id, title in [("1", "타로 재회운 봐주세요"), ("2", "두피 건선 크림 추천")]:
        state.store.upsert_raw(RawQuestion(doc_id=doc_id, url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title), "검색:t")
    state.store.classify(Matcher(state.cfg.products))
    return state, create_app(state, behind_proxy=True)


AUTH = {"Authorization": f"Bearer {SECRET}"}


def test_summary_needs_secret(server):
    state, app = server
    c = app.test_client()
    assert c.get("/api/link/summary").status_code == 404
    assert c.get("/api/link/summary", headers={"Authorization": "Bearer 틀린값"}).status_code == 404
    state.store.set_status("1", "answered", by="kim")
    data = c.get("/api/link/summary", headers=AUTH).get_json()
    counts = {x["label"]: x["value"] for x in data["counts"]}
    assert counts["답변할 지식iN 질문"] == 1 and counts["오늘 답변·댓글 완료"] == 1
    assert data["staff"] == [{"login": "kim", "name": "kim", "today": 1, "week": 1, "role": "staff"}]


def test_sso_logs_staff_in_and_creates_account(server):
    state, app = server
    c = app.test_client()
    r = c.get("/sso", query_string={"ticket": ticket(), "next": "/"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    meta = c.get("/api/meta").get_json()
    assert meta["user"] == {"username": "kim", "name": "김직원", "role": "staff"}
    assert state.store.get_user("kim")["active"] == 1


def test_sso_admin_maps_to_admin(server):
    _, app = server
    c = app.test_client()
    c.get("/sso", query_string={"ticket": ticket(sub="daepyo", role="admin")})
    assert c.get("/api/meta").get_json()["user"]["role"] == "admin"


def test_sso_rejects_bad_tickets(server):
    _, app = server
    c = app.test_client()
    used = ticket()
    assert c.get("/sso", query_string={"ticket": used}).status_code == 302
    c2 = app.test_client()
    assert c2.get("/sso", query_string={"ticket": used}).status_code == 403  # 한 번만 쓸 수 있음
    assert c2.get("/sso", query_string={"ticket": ticket(exp=int(time.time()) - 5)}).status_code == 403
    assert c2.get("/sso", query_string={"ticket": ticket(exp=int(time.time()) + 3600)}).status_code == 403
    assert c2.get("/sso", query_string={"ticket": ticket(secret="다른-비밀값-0123456789abcdef-0123456789")}).status_code == 403
    assert c2.get("/sso", query_string={"ticket": ticket(aud="other")}).status_code == 403
    assert c2.get("/sso", query_string={"ticket": ticket(sub="boss")}).status_code == 403  # 관리자 아이디로 직원 로그인 불가
    assert c2.get("/sso", query_string={"ticket": "abc"}).status_code == 403
    assert c2.get("/api/meta").status_code == 401


def test_sso_blocks_open_redirect(server):
    _, app = server
    r = app.test_client().get("/sso", query_string={"ticket": ticket(), "next": "//evil.example"})
    assert r.headers["Location"].endswith("/") and "evil" not in r.headers["Location"]


def test_deactivate_from_desk(server):
    state, app = server
    staff = app.test_client()
    staff.get("/sso", query_string={"ticket": ticket()})
    assert staff.get("/api/meta").status_code == 200
    c = app.test_client()
    assert c.post("/api/link/deactivate", json={"login": "kim"}).status_code == 404
    assert c.post("/api/link/deactivate", json={"login": "kim"}, headers=AUTH).get_json() == {"ok": True, "found": True}
    assert staff.get("/api/meta").status_code == 401
    # 업무 데스크에서 다시 열면(= 다시 사용 중) 켜진다
    staff.get("/sso", query_string={"ticket": ticket()})
    assert staff.get("/api/meta").status_code == 200


def test_link_off_without_secret(server, monkeypatch):
    _, app = server
    monkeypatch.delenv("JISIKIN_LINK_SECRET")
    c = app.test_client()
    assert c.get("/api/link/summary", headers=AUTH).status_code == 404
    assert c.get("/sso", query_string={"ticket": ticket()}).status_code == 403
