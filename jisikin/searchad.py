"""네이버 검색광고 API — 키워드 도구 (월간 검색수 조회). 선택 기능.

searchad.naver.com 가입 → 도구 → API 사용 관리 에서 무료로 발급.
광고를 집행하지 않아도 키워드 월간 검색수 조회는 쓸 수 있다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import requests

from .naver import NaverError

BASE_URL = "https://api.naver.com"


def _count(value) -> int | None:
    """'< 10' 같은 값은 5 로 본다."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).replace(",", "").strip()
    if s.startswith("<"):
        return 5
    try:
        return int(float(s))
    except ValueError:
        return None


def compact(keyword: str) -> str:
    return "".join(keyword.split()).lower()


class SearchAdClient:
    def __init__(self, customer_id: str, access_license: str, secret_key: str,
                 session: requests.Session | None = None, timeout: float = 15.0, delay_seconds: float = 0.3):
        self.customer_id = str(customer_id)
        self.access_license = access_license
        self.secret_key = secret_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.delay_seconds = delay_seconds
        self._last = 0.0

    def _headers(self, method: str, uri: str) -> dict:
        ts = str(int(time.time() * 1000))
        msg = f"{ts}.{method}.{uri}".encode()
        sig = base64.b64encode(hmac.new(self.secret_key.encode(), msg, hashlib.sha256).digest()).decode()
        return {"X-Timestamp": ts, "X-API-KEY": self.access_license, "X-Customer": self.customer_id, "X-Signature": sig}

    def keyword_stats(self, hints: list[str]) -> list[dict]:
        """연관 키워드와 월간 검색수. hints 는 최대 5개 (띄어쓰기는 API 규칙상 제거됨).

        반환: [{keyword, pc, mobile}] — keyword 는 띄어쓰기 없는 형태로 돌아온다.
        """
        hints = [compact(h) for h in hints if compact(h)][:5]
        if not hints:
            return []
        wait = self._last + self.delay_seconds - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        uri = "/keywordstool"
        try:
            r = self.session.get(
                BASE_URL + uri,
                params={"hintKeywords": ",".join(hints), "showDetail": "1"},
                headers=self._headers("GET", uri),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise NaverError(f"검색광고 API 연결 실패: {e.__class__.__name__}") from e
        finally:
            self._last = time.monotonic()
        if r.status_code in (401, 403):
            raise NaverError("검색광고 API 인증 실패 — CUSTOMER_ID / 액세스라이선스 / 비밀키를 확인하세요.", fatal=True)
        if r.status_code == 429:
            raise NaverError("검색광고 API 호출이 너무 많습니다. 잠시 후 다시 시도하세요.", fatal=True)
        if r.status_code != 200:
            raise NaverError(f"검색광고 API 오류 {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise NaverError("검색광고 API 응답을 읽을 수 없습니다.") from e
        out = []
        for k in data.get("keywordList") or []:
            kw = str(k.get("relKeyword") or "").strip()
            if kw:
                out.append({"keyword": kw, "pc": _count(k.get("monthlyPcQcCnt")), "mobile": _count(k.get("monthlyMobileQcCnt"))})
        return out
