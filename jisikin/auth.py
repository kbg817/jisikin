"""로그인 — 서버에 올려 여러 명이 쓸 때만 켜진다.

JISIKIN_ADMIN_PASSWORD 환경변수가 있으면 로그인이 필요해진다. (PC 에서 혼자 쓸 때는 필요 없음)
- 관리자: 아이디 JISIKIN_ADMIN_USER (기본 admin) / 비밀번호 JISIKIN_ADMIN_PASSWORD
- 직원: 관리자가 [설정] → 직원 계정에서 만든다 (DB 에 비밀번호 해시로 저장)
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

LOCAL_USER = {"username": "", "name": "", "role": "admin"}
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,19}$")
MIN_PASSWORD = 6


def auth_enabled() -> bool:
    return bool(os.environ.get("JISIKIN_ADMIN_PASSWORD", "").strip())


def admin_username() -> str:
    return os.environ.get("JISIKIN_ADMIN_USER", "").strip().lower() or "admin"


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def authenticate(store, username: str, password: str) -> dict | None:
    username = (username or "").strip().lower()
    password = password or ""
    if not username or not password:
        return None
    if username == admin_username():
        expected = os.environ.get("JISIKIN_ADMIN_PASSWORD", "").strip()
        if expected and secrets.compare_digest(password.encode(), expected.encode()):
            return {"username": username, "name": "관리자", "role": "admin"}
        return None
    user = store.get_user(username)
    if user and user["active"] and check_password_hash(user["password_hash"], password):
        return {"username": user["username"], "name": user["name"], "role": "staff"}
    return None


def secret_key(data_dir: Path) -> str:
    """세션 쿠키 서명 키. 환경변수가 없으면 데이터 폴더에 한 번 만들어 계속 쓴다."""
    env = os.environ.get("JISIKIN_SECRET_KEY", "").strip()
    if env:
        return env
    path = Path(data_dir) / "secret_key"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(32)
        path.write_text(key, encoding="utf-8")
        return key
    except OSError:
        return secrets.token_urlsafe(32)


class LoginLimiter:
    """같은 곳(IP)에서 비밀번호를 여러 번 틀리면 잠시 막는다."""

    def __init__(self, max_failures: int = 8, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window = window_seconds
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        items = [t for t in self._fails.get(key, []) if now - t < self.window]
        self._fails[key] = items
        return items

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
