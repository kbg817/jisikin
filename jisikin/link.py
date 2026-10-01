"""업무 데스크 연동 — 사내 업무 대시보드(업무 데스크)와 숫자·로그인을 이어준다.

JISIKIN_LINK_SECRET 환경변수가 있을 때만 켜진다. 업무 데스크의 TOOL_LINK_SECRET 과 같은 값을 넣는다.
- 업무 데스크 홈에 할 일 수·직원별 답변 실적을 보여준다 (/api/link/summary)
- 업무 데스크에서 [열기]를 누르면 따로 로그인하지 않고 들어온다 (/sso, 1분짜리 1회용 입장권)
- 업무 데스크에서 직원을 사용 중지하면 여기서도 막힌다 (/api/link/deactivate)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

MAX_TICKET_SECONDS = 120  # 입장권 유효시간 상한 (업무 데스크는 60초로 만든다)
TICKET_AUDIENCE = "tool-link"


def link_secret() -> str:
    return os.environ.get("JISIKIN_LINK_SECRET", "").strip()


def link_enabled() -> bool:
    return len(link_secret()) >= 32


def bearer_ok(header: str | None) -> bool:
    """업무 데스크가 보낸 'Authorization: Bearer <비밀값>' 확인."""
    secret = link_secret()
    if len(secret) < 32 or not header or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header[7:].strip().encode(), secret.encode())


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class TicketError(ValueError):
    pass


class TicketBook:
    """한 번 쓴 입장권은 다시 못 쓰게 기억한다 (유효시간이 지나면 잊는다)."""

    def __init__(self):
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def use(self, jti: str, exp: float) -> bool:
        now = time.time()
        with self._lock:
            self._used = {k: v for k, v in self._used.items() if v > now}
            if jti in self._used:
                return False
            self._used[jti] = exp
            return True


def verify_ticket(token: str, book: TicketBook, now: float | None = None) -> dict:
    """업무 데스크가 HS256 으로 서명한 입장권을 확인하고 내용(sub, name, role)을 돌려준다."""
    secret = link_secret()
    if len(secret) < 32:
        raise TicketError("연동이 꺼져 있습니다")
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_b64decode(head_b64))
        claims = json.loads(_b64decode(body_b64))
        sig = _b64decode(sig_b64)
    except (ValueError, json.JSONDecodeError) as e:
        raise TicketError("입장권 형식이 맞지 않습니다") from e
    if header.get("alg") != "HS256":
        raise TicketError("입장권 형식이 맞지 않습니다")
    expected = hmac.new(secret.encode(), f"{head_b64}.{body_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise TicketError("입장권 서명이 맞지 않습니다")
    now = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp < now or exp - now > MAX_TICKET_SECONDS:
        raise TicketError("입장권 시간이 지났습니다. 업무 데스크에서 다시 열어 주세요.")
    if claims.get("aud") != TICKET_AUDIENCE or not claims.get("sub") or not claims.get("jti"):
        raise TicketError("입장권 내용이 맞지 않습니다")
    if claims.get("role") not in ("admin", "staff"):
        raise TicketError("입장권 내용이 맞지 않습니다")
    if not book.use(str(claims["jti"]), float(exp)):
        raise TicketError("이미 사용한 입장권입니다. 업무 데스크에서 다시 열어 주세요.")
    return claims
