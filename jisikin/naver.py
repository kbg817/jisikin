"""네이버 지식iN 에서 질문을 가져온다.

- search_api   : 네이버 검색 API (공식, 권장). developers.naver.com 에서 무료 발급.
- search_web   : 지식iN 검색 결과 페이지를 직접 읽는다. (API 키가 없을 때의 대안)
- fetch_list   : 지식iN 분야 목록 등, 질문 링크가 있는 아무 페이지에서 질문을 뽑는다.
- fetch_detail : 질문 상세 페이지에서 본문·답변 수·작성일·내공을 읽는다.

HTML 구조는 네이버가 예고 없이 바꿀 수 있으므로, 특정 class 이름에만 의존하지 않고
'질문 링크(/qna/detail.naver?...docId=...)' 를 기준으로 최대한 방어적으로 파싱한다.
"""
from __future__ import annotations

import html as html_lib
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

KST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
API_URL = "https://openapi.naver.com/v1/search/kin.json"
WEB_SEARCH_URL = "https://kin.naver.com/search/list.naver"
DETAIL_URL = "https://kin.naver.com/qna/detail.naver"

# 질문 제목이 아닌 링크 문구 (같은 질문으로 가는 '답변하기' 버튼 등)
_GENERIC_LINK_TEXT = {"답변하기", "답변", "질문", "더보기", "원문보기", "바로가기", "댓글", "공유", "신고"}


class NaverError(Exception):
    """사용자에게 보여줄 수 있는 네이버 요청 오류."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # True 면 같은 방식의 남은 요청도 실패할 것 (예: 인증 오류)


@dataclass
class RawQuestion:
    doc_id: str
    url: str
    title: str
    snippet: str = ""
    answer_count: int | None = None
    asked_at: datetime | None = None
    dir_name: str = ""


@dataclass
class QuestionDetail:
    title: str = ""
    body: str = ""
    answer_count: int | None = None
    asked_at: datetime | None = None
    reward: int | None = None


# ---------------------------------------------------------------- 유틸


def parse_kin_url(url: str) -> tuple[str, str] | None:
    """지식iN 질문 주소 → (docId, 정리된 PC 주소). 질문 주소가 아니면 None."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if not u.netloc.endswith("kin.naver.com") or not u.path.endswith("/qna/detail.naver"):
        return None
    qs = parse_qs(u.query)
    doc_id = (qs.get("docId") or [""])[0]
    if not doc_id.isdigit():
        return None
    params = {}
    for key in ("d1id", "dirId"):
        v = (qs.get(key) or [""])[0]
        if v.isdigit():
            params[key] = v
    params["docId"] = doc_id
    return doc_id, f"{DETAIL_URL}?{urlencode(params)}"


_BLOCK_TAGS = (
    "p", "div", "br", "li", "ul", "ol", "dl", "dd", "dt", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer", "blockquote",
)
_BLOCK_TAG_RE = re.compile(r"</?(?:" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>", re.I)


def clean_text(value: str | None) -> str:
    """HTML 태그/엔티티 제거 + 공백 정리.

    <b>타로</b>로 → '타로로' 처럼 글자 사이 인라인 태그는 붙이고, 줄바꿈성 태그만 띄운다.
    """
    if not value:
        return ""
    value = _BLOCK_TAG_RE.sub(" ", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html_lib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _soup(html: str) -> BeautifulSoup:
    """스크립트를 지우고, 블록 요소 앞뒤에만 공백을 넣은 soup. (이후 get_text("") 로 읽는다)"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "template"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before(" ")
        tag.insert_after(" ")
    return soup


def _text(el: Tag | None) -> str:
    # get_text 는 이미 엔티티가 풀린 순수 텍스트이므로 공백만 정리한다
    return re.sub(r"\s+", " ", el.get_text("")).strip() if el else ""


_ABS_DATE = re.compile(
    r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*[.일]?"
    r"(?:\s*(오전|오후)?\s*(\d{1,2}):(\d{2}))?"
)


def parse_korean_datetime(text: str | None, now: datetime | None = None) -> datetime | None:
    """'2024.05.01.', '2024.05.01. 14:30', '3시간 전', '15분 전', '어제', '방금' 등을 해석."""
    if not text:
        return None
    now = now or datetime.now(KST)
    t = text.strip()
    m = _ABS_DATE.search(t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = 0, 0
        if m.group(5):
            hh, mm = int(m.group(5)), int(m.group(6))
            if m.group(4) == "오후" and hh < 12:
                hh += 12
            elif m.group(4) == "오전" and hh == 12:
                hh = 0
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=KST)
        except ValueError:
            return None
    if "방금" in t:
        return now
    m = re.search(r"(\d+)\s*초\s*전", t)
    if m:
        return now - timedelta(seconds=int(m.group(1)))
    m = re.search(r"(\d+)\s*분\s*전", t)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d+)\s*시간\s*전", t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*일\s*전", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    if "어제" in t:
        return now - timedelta(days=1)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return None


_DATE_IN_TEXT = re.compile(
    r"20\d{2}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?(?:\s*\d{1,2}:\d{2})?"
    r"|\d+\s*(?:초|분|시간|일)\s*전|방금|어제"
)
_ANSWER_COUNT_IN_TEXT = re.compile(r"답변\s*수?\s*[:：]?\s*(\d+)")


# ---------------------------------------------------------------- HTML 파싱


def extract_questions(html: str, base_url: str = "https://kin.naver.com/") -> list[RawQuestion]:
    """질문 링크가 들어있는 아무 지식iN 페이지(검색 결과, 분야 목록)에서 질문 목록을 뽑는다."""
    soup = _soup(html)
    found: dict[str, RawQuestion] = {}
    order: list[str] = []
    for a in soup.find_all("a", href=True):
        parsed = parse_kin_url(urljoin(base_url, a["href"]))
        if not parsed:
            continue
        doc_id, url = parsed
        title = _text(a)
        if len(title) < 2 or title in _GENERIC_LINK_TEXT:
            continue
        prev = found.get(doc_id)
        if prev and not _looks_like_title_anchor(a):
            continue  # 이미 제목 링크를 찾았음
        row = _row_container(a)
        ctx = _text(row)
        rq = RawQuestion(doc_id=doc_id, url=url, title=title)
        if ctx:
            rest = ctx.replace(title, " ", 1)
            m = _ANSWER_COUNT_IN_TEXT.search(rest)
            if m:
                rq.answer_count = int(m.group(1))
            md = _DATE_IN_TEXT.search(rest)
            if md:
                rq.asked_at = parse_korean_datetime(md.group(0))
            rest = _ANSWER_COUNT_IN_TEXT.sub(" ", rest)
            rest = _DATE_IN_TEXT.sub(" ", rest)
            rest = re.sub(r"조회\s*수?\s*\d+|추천\s*수?\s*\d+|내공\s*\d+", " ", rest)
            rq.snippet = re.sub(r"\s+", " ", rest).strip()[:300]
        if doc_id not in found:
            order.append(doc_id)
        found[doc_id] = rq
    return [found[d] for d in order]


def _looks_like_title_anchor(a: Tag) -> bool:
    for node in [a, *list(a.parents)[:3]]:
        if not isinstance(node, Tag):
            continue
        if node.name in ("dt", "h2", "h3", "h4", "strong"):
            return True
        classes = " ".join(node.get("class") or [])
        if "title" in classes or "tit" in classes.split():
            return True
    return False


def _row_container(a: Tag) -> Tag | None:
    """링크를 감싸는 '한 줄(결과 1건)' 요소를 찾는다."""
    node: Tag | None = a
    for _ in range(7):
        node = node.parent if node else None
        if node is None or node.name in ("body", "html", "ul", "ol", "table", "tbody"):
            return None
        if node.name in ("li", "tr", "dl", "article"):
            return node
    return None


def parse_detail(html: str, now: datetime | None = None) -> QuestionDetail:
    soup = _soup(html)
    d = QuestionDetail()

    heading = _first(soup, [".c-heading", ".question-content", ".endTitleSection", "#content .question"])

    title_el = _first(soup, [".c-heading__title .title", ".c-heading__title", ".endTitleSection .title", ".endTitleSection"])
    d.title = _text(title_el)
    if not d.title:
        d.title = _clean_og_title(_meta(soup, "og:title"))

    body_el = _first(soup, [".c-heading__content", ".questionDetail", ".question-content__detail"])
    d.body = _text(body_el)
    if not d.body:
        d.body = clean_text(_meta(soup, "og:description") or _meta(soup, "description"))
    d.body = d.body[:4000]

    d.answer_count = _parse_answer_count(soup)

    area_text = _text(heading)
    page_text = _text(soup)
    d.asked_at = _parse_asked_at(soup, area_text or page_text, now)

    m = re.search(r"내공\s*(\d+)", area_text) if area_text else None
    if m:
        d.reward = int(m.group(1))
    return d


def _first(soup: BeautifulSoup, selectors: list[str]) -> Tag | None:
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:  # 잘못된 선택자
            el = None
        if el and el.get_text(strip=True):
            return el
    return None


def _meta(soup: BeautifulSoup, name: str) -> str:
    el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    return (el.get("content") or "") if el else ""


def _clean_og_title(value: str) -> str:
    value = clean_text(value)
    return re.sub(r"\s*[:|\-]\s*(네이버\s*)?지식\s*iN\s*$", "", value, flags=re.I).strip()


def _parse_answer_count(soup: BeautifulSoup) -> int | None:
    # 1) 답변 수를 담은 요소 (class 이름에 answerCount 가 들어간 것)
    for el in soup.find_all(class_=re.compile(r"answer_?count", re.I)):
        m = re.search(r"\d+", _text(el))
        if m:
            return int(m.group(0))
    # 2) 답변 영역 머리글의 "답변 3" 같은 문구
    for el in soup.find_all(class_=re.compile(r"answer.*(title|header|head)", re.I)):
        m = re.search(r"답변\s*(\d+)", _text(el))
        if m:
            return int(m.group(1))
    # 3) 답변 항목 개수
    items = soup.select("[class*='answer-content__item']") or soup.select("._answer, ._answerComponent")
    if items:
        return len(items)
    # 4) '아직 답변이 없다' 류의 문구
    text = _text(soup)
    if re.search(r"(아직|등록된)\s*답변이\s*없|첫\s*번째?\s*답변을|답변을\s*기다리", text):
        return 0
    return None


def _parse_asked_at(soup: BeautifulSoup, text: str, now: datetime | None) -> datetime | None:
    m = re.search(r"작성일\s*[:：]?\s*(" + _DATE_IN_TEXT.pattern + ")", text)
    if m:
        return parse_korean_datetime(m.group(1), now)
    for el in soup.find_all(class_=re.compile(r"(^|_|-)date|userinfo__info", re.I)):
        dt = parse_korean_datetime(_text(el), now)
        if dt:
            return dt
    published = _meta(soup, "article:published_time")
    if published:
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=KST)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- 클라이언트


class NaverClient:
    def __init__(
        self,
        credentials: tuple[str, str] | None = None,
        delay_seconds: float = 1.0,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.credentials = credentials
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
            }
        )
        self._last_web_request = 0.0
        self._lock = threading.Lock()

    # --- 공식 검색 API
    def search_api(self, query: str, count: int = 50) -> list[RawQuestion]:
        if not self.credentials:
            raise NaverError("네이버 API 키가 없습니다. .env 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 을 넣어주세요.", fatal=True)
        cid, secret = self.credentials
        params = {"query": query, "display": max(1, min(count, 100)), "start": 1, "sort": "date"}
        try:
            r = self.session.get(
                API_URL,
                params=params,
                headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise NaverError(f"네이버 API 연결 실패: {e.__class__.__name__}") from e
        if r.status_code == 401:
            raise NaverError("네이버 API 인증 실패 — Client ID/Secret 을 확인하세요.", fatal=True)
        if r.status_code == 403:
            raise NaverError("네이버 API 권한 없음 — 애플리케이션에 '검색' API 가 추가되어 있는지 확인하세요.", fatal=True)
        if r.status_code == 429:
            raise NaverError("네이버 API 하루 호출 한도를 초과했습니다.", fatal=True)
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("errorMessage", "")
            except ValueError:
                pass
            raise NaverError(f"네이버 API 오류 {r.status_code} {detail}".strip())
        try:
            data = r.json()
        except ValueError as e:
            raise NaverError("네이버 API 응답을 읽을 수 없습니다.") from e
        return parse_api_items(data.get("items") or [])

    # --- 웹 검색 (API 키 없을 때)
    def search_web(self, query: str, page: int = 1) -> list[RawQuestion]:
        params = {"query": query, "sort": "date", "section": "kin", "page": page}
        html = self._get_html(WEB_SEARCH_URL, params=params)
        return extract_questions(html, WEB_SEARCH_URL)

    def fetch_list(self, url: str) -> list[RawQuestion]:
        html = self._get_html(url)
        return extract_questions(html, url)

    def fetch_detail(self, url: str) -> QuestionDetail:
        return parse_detail(self._get_html(url))

    def _get_html(self, url: str, params: dict | None = None) -> str:
        with self._lock:
            wait = self._last_web_request + self.delay_seconds - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                raise NaverError(f"지식iN 연결 실패: {e.__class__.__name__}") from e
            finally:
                self._last_web_request = time.monotonic()
        if r.status_code in (403, 429):
            raise NaverError(f"지식iN 이 요청을 거부했습니다({r.status_code}). 수집 간격을 늘려주세요.", fatal=True)
        if r.status_code != 200:
            raise NaverError(f"지식iN 응답 오류 {r.status_code}")
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return r.text


def parse_api_items(items: list[dict]) -> list[RawQuestion]:
    out = []
    for it in items:
        parsed = parse_kin_url(it.get("link") or "")
        if not parsed:
            continue
        doc_id, url = parsed
        title = clean_text(it.get("title"))
        if not title:
            continue
        out.append(RawQuestion(doc_id=doc_id, url=url, title=title, snippet=clean_text(it.get("description"))))
    return out
