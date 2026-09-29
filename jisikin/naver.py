"""네이버 지식iN 에서 질문을 가져온다.

- search_api   : 네이버 검색 API (공식, 권장). developers.naver.com 에서 무료 발급.
- search_web   : 지식iN 검색 결과 페이지를 직접 읽는다. (API 키가 없을 때의 대안)
- fetch_list   : 지식iN 분야 목록 등, 질문 링크가 있는 아무 페이지에서 질문을 뽑는다.
- fetch_detail : 질문 상세 페이지에서 본문·답변 수·작성일·내공·조회수를 읽는다.
- search_integrated : 네이버 통합검색(PC/모바일) 결과에서 지식iN 글이 노출된 순서를 읽는다.
- search_kin_ranked : 지식iN 탭 정확도순 상위 글.

HTML 구조는 네이버가 예고 없이 바꿀 수 있으므로, 특정 class 이름에만 의존하지 않고
'질문 링크(/qna/detail.naver?...docId=...)' 를 기준으로 최대한 방어적으로 파싱한다.
"""
from __future__ import annotations

import html as html_lib
import json
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
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
# 네이버 검색 API 는 두 곳에서 발급된다. 같은 Client ID/Secret 칸에 넣으면 어느 쪽 키인지 자동으로 알아낸다.
#  - hub        : 네이버클라우드 NAVER API HUB (2026년부터 신규 발급은 여기서만 가능)
#  - developers : 기존 네이버 개발자센터 (예전에 발급받은 키)
HUB_API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/kin"
API_URL = "https://openapi.naver.com/v1/search/kin.json"
API_PROVIDERS = ("hub", "developers")
API_PROVIDER_NAMES = {"hub": "NAVER API HUB", "developers": "네이버 개발자센터"}
_provider_cache: dict[str, str] = {}  # Client ID → 성공한 발급처 (매번 두 곳을 시도하지 않도록)
WEB_SEARCH_URL = "https://kin.naver.com/search/list.naver"
DETAIL_URL = "https://kin.naver.com/qna/detail.naver"
INTEGRATED_PC_URL = "https://search.naver.com/search.naver"
INTEGRATED_MOBILE_URL = "https://m.search.naver.com/search.naver"
AUTOCOMPLETE_URL = "https://ac.search.naver.com/nx/ac"

# 질문 제목이 아닌 링크 문구 (같은 질문으로 가는 '답변하기' 버튼 등)
_GENERIC_LINK_TEXT = {"답변하기", "답변", "질문", "더보기", "원문보기", "바로가기", "댓글", "공유", "신고"}


class NaverError(Exception):
    """사용자에게 보여줄 수 있는 네이버 요청 오류."""

    def __init__(self, message: str, fatal: bool = False, auth: bool = False):
        super().__init__(message)
        self.fatal = fatal  # True 면 같은 방식의 남은 요청도 실패할 것 (예: 인증 오류)
        self.auth = auth    # 키가 맞지 않아 실패 (다른 발급처 방식으로 다시 시도해 볼 만함)


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
    views: int | None = None


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


_BLIND_TEXT_RE = re.compile(r"\s*새\s*창\s*(?:으로\s*)?(?:열림|열기)\s*")
_SOURCE_LABEL_RE = re.compile(r"^(?:네이버\s*)?지식\s*iN(?:\s*(?:질문|답변|Q&A))?$", re.I)
UNKNOWN_TITLE = "(제목 확인 중)"


def extract_questions(html: str, base_url: str = "https://kin.naver.com/") -> list[RawQuestion]:
    """질문 링크가 들어있는 아무 지식iN 페이지(검색 결과, 분야 목록)에서 질문 목록을 뽑는다.

    같은 질문으로 가는 링크가 여러 개면(출처 표시·제목·답변 미리보기 등) 제목다운 링크를 고른다.
    순서는 그 질문 링크가 페이지에 처음 나온 순서 (= 노출 순위).
    """
    soup = _soup(html)
    order: list[str] = []
    urls: dict[str, str] = {}
    cands: dict[str, list[tuple[float, int, Tag, str]]] = {}
    for idx, a in enumerate(soup.find_all("a", href=True)):
        parsed = parse_kin_url(urljoin(base_url, a["href"]))
        if not parsed:
            continue
        doc_id, url = parsed
        if doc_id not in urls:
            order.append(doc_id)
            urls[doc_id] = url
        text = re.sub(r"\s+", " ", _BLIND_TEXT_RE.sub(" ", _text(a))).strip()
        if len(text) < 2 or text in _GENERIC_LINK_TEXT or _SOURCE_LABEL_RE.match(text):
            continue  # '답변하기', '네이버 지식iN' 같은 버튼·출처 표시
        cands.setdefault(doc_id, []).append((_title_score(a), idx, a, text))

    out: list[RawQuestion] = []
    for doc_id in order:
        options = cands.get(doc_id)
        if not options:  # 제목 링크는 없지만 순위 계산을 위해 남긴다 (제목은 상세 페이지에서 채움)
            out.append(RawQuestion(doc_id=doc_id, url=urls[doc_id], title=UNKNOWN_TITLE))
            continue
        _, _, a, title = max(options, key=lambda o: (o[0], -o[1]))
        rq = RawQuestion(doc_id=doc_id, url=urls[doc_id], title=title)
        ctx = _text(_row_container(a))
        if ctx:
            rest = _BLIND_TEXT_RE.sub(" ", ctx).replace(title, " ", 1)
            m = _ANSWER_COUNT_IN_TEXT.search(rest)
            if m:
                rq.answer_count = int(m.group(1))
            md = _DATE_IN_TEXT.search(rest)
            if md:
                rq.asked_at = parse_korean_datetime(md.group(0))
            rest = _ANSWER_COUNT_IN_TEXT.sub(" ", rest)
            rest = _DATE_IN_TEXT.sub(" ", rest)
            rest = re.sub(r"조회\s*수?\s*\d+|추천\s*수?\s*\d+|내공\s*\d+|(?:네이버\s*)?지식\s*iN", " ", rest)
            rq.snippet = re.sub(r"\s+", " ", rest).strip()[:300]
        out.append(rq)
    return out


def _title_score(a: Tag) -> float:
    """제목 링크일수록 높은 점수. (제목 class·제목 태그 +, 답변 미리보기·#answer 링크 -)"""
    score = 0.0
    if "#" in a.get("href", ""):
        score -= 3
    nodes = [a, *[p for p in list(a.parents)[:3] if isinstance(p, Tag)]]
    if any(n.name in ("dt", "h2", "h3", "h4", "strong") for n in nodes):
        score += 2
    classes = " ".join(" ".join(n.get("class") or []) for n in nodes).lower()
    if re.search(r"(^|[\s_-])(tit|title|headline|subject|question)", classes):
        score += 3
    if re.search(r"answer|desc|dsc|snippet|preview", classes):
        score -= 2
    return score


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
    d.asked_at = _parse_asked_at(soup, area_text or page_text, now) or _parse_asked_at_fallback(html, area_text, page_text, now)

    m = re.search(r"내공\s*(\d+)", area_text) if area_text else None
    if m:
        d.reward = int(m.group(1))
    d.views = _parse_views(soup, area_text)
    return d


_VIEWS_RE = re.compile(r"조회\s*수?\s*[:：]?\s*(\d[\d,]*)")


def _parse_views(soup: BeautifulSoup, area_text: str) -> int | None:
    m = _VIEWS_RE.search(area_text) if area_text else None
    if not m:
        for el in soup.find_all(class_=re.compile(r"userinfo|info", re.I)):
            m = _VIEWS_RE.search(_text(el))
            if m:
                break
    return int(m.group(1).replace(",", "")) if m else None


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
        return _parse_iso_or_korean(published, now)
    return None


_JSON_DATE_KEYS = re.compile(
    r'["\']?(?:regDate|registDate|registerDate|writeDate|createDate|createdAt|createdDate|regDt|docRegDate|questionDate)["\']?'
    r'\s*[:=]\s*["\']([^"\']{8,32})["\']',
    re.I,
)


def _parse_iso_or_korean(value: str, now: datetime | None) -> datetime | None:
    value = (value or "").strip()
    if re.fullmatch(r"\d{12,13}", value):  # 밀리초 타임스탬프
        return datetime.fromtimestamp(int(value) / 1000, KST)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
        return dt if dt.tzinfo else dt.replace(tzinfo=KST)
    except ValueError:
        return parse_korean_datetime(value, now)


def _parse_asked_at_fallback(raw_html: str, area_text: str, page_text: str, now: datetime | None) -> datetime | None:
    """화면 구조가 예상과 다를 때: 질문 머리글 안의 날짜 → 스크립트 데이터 → '작성/등록' 근처 날짜."""
    if area_text:
        m = _DATE_IN_TEXT.search(area_text)
        if m:
            dt = parse_korean_datetime(m.group(0), now)
            if dt:
                return dt
    for m in _JSON_DATE_KEYS.finditer(raw_html or ""):
        dt = _parse_iso_or_korean(m.group(1), now)
        if dt:
            return dt
    m = re.search(r"(?:작성|등록)\S{0,3}\s*[:：]?\s*(" + _DATE_IN_TEXT.pattern + ")", page_text or "")
    if m:
        return parse_korean_datetime(m.group(1), now)
    return None


_KIN_URL_RAW = re.compile(
    r"(?:https?:)?(?:\\?/\\?/)(?:m\.)?kin\.naver\.com(?:\\?/)(?:mobile(?:\\?/))?qna(?:\\?/)detail\.naver\?[^\"'<>\s]{0,300}?docId(?:=|\\u003[dD])\d+"
)


def extract_kin_links_raw(html: str) -> list[RawQuestion]:
    """링크(<a>)로 못 찾을 때: 페이지 원문(스크립트 데이터 포함)에서 지식iN 질문 주소를 나온 순서대로 뽑는다.

    제목은 모르므로 비워 두고, 상세 페이지를 읽을 때 채운다.
    """
    out: list[RawQuestion] = []
    seen: set[str] = set()
    for m in _KIN_URL_RAW.finditer(html or ""):
        url = m.group(0)
        url = url.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=").replace("\\u003D", "=").replace("&amp;", "&")
        if url.startswith("//"):
            url = "https:" + url
        parsed = parse_kin_url(url)
        if parsed and parsed[0] not in seen:
            seen.add(parsed[0])
            out.append(RawQuestion(doc_id=parsed[0], url=parsed[1], title=UNKNOWN_TITLE))
    return out


def debug_snippets(html: str, pattern: str, limit: int = 3, width: int = 70) -> list[str]:
    """[연결 점검]용: 원문에서 pattern 주변 글자를 짧게 보여준다 (화면 구조 파악용)."""
    out = []
    for m in re.finditer(pattern, html or ""):
        a, b = max(0, m.start() - width), min(len(html), m.end() + width)
        s = re.sub(r"\s+", " ", html[a:b]).strip()
        out.append(s)
        if len(out) >= limit:
            break
    return out


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
        self.last_status: int | None = None
        self.last_html = ""

    # --- 공식 검색 API
    @property
    def api_provider(self) -> str | None:
        """이 키가 어느 발급처 키인지 (한 번 성공한 뒤에만 알 수 있음)."""
        return _provider_cache.get(self.credentials[0]) if self.credentials else None

    def search_api(self, query: str, count: int = 50, sort: str = "date") -> list[RawQuestion]:
        """sort: date(최신순) / sim(정확도순)"""
        if not self.credentials:
            raise NaverError("네이버 API 키가 없습니다. [설정] > API 키에 Client ID / Secret 을 넣어주세요.", fatal=True)
        known = self.api_provider
        providers = [known] if known else list(API_PROVIDERS)
        auth_errors: list[NaverError] = []
        for provider in providers:
            try:
                items = self._search_api_once(provider, query, count, sort)
            except NaverError as e:
                if e.auth and len(providers) > 1:
                    auth_errors.append(e)
                    continue
                raise
            _provider_cache[self.credentials[0]] = provider
            return items
        raise NaverError(
            "네이버 API 인증 실패 — NAVER API HUB(네이버클라우드)에서 발급한 Client ID / Client Secret 인지, "
            "Application 에 '검색' API 를 선택했는지 확인하세요. "
            f"(자세히: {' / '.join(str(e) for e in auth_errors)})",
            fatal=True,
            auth=True,
        )

    def _search_api_once(self, provider: str, query: str, count: int, sort: str) -> list[RawQuestion]:
        cid, secret = self.credentials
        name = API_PROVIDER_NAMES[provider]
        params = {"query": query, "display": max(1, min(count, 100)), "start": 1, "sort": sort}
        if provider == "hub":
            url = HUB_API_URL
            params["format"] = "json"
            headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret}
        else:
            url = API_URL
            headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise NaverError(f"네이버 API 연결 실패: {e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise NaverError(f"{name} {r.status_code} {_api_error_detail(r)}".strip(), fatal=True, auth=True)
        if r.status_code == 429:
            raise NaverError("네이버 API 하루 호출 한도를 초과했습니다.", fatal=True)
        if r.status_code != 200:
            raise NaverError(f"네이버 API 오류 {r.status_code} {_api_error_detail(r)}".strip())
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

    # --- 상위노출 확인
    def search_integrated(self, query: str, platform: str = "pc") -> list[RawQuestion]:
        """네이버 통합검색 결과에 노출된 지식iN 글을 화면에 나온 순서대로 돌려준다."""
        if platform == "mobile":
            base = INTEGRATED_MOBILE_URL
            html = self._get_html(
                base,
                params={"where": "m", "sm": "mtp_hty", "query": query},
                headers={"User-Agent": MOBILE_USER_AGENT},
                site="네이버 모바일 검색",
            )
        else:
            base = INTEGRATED_PC_URL
            html = self._get_html(
                base,
                params={"where": "nexearch", "sm": "top_hty", "query": query},
                site="네이버 통합검색",
            )
        # 보통은 <a> 링크로 찾고, 결과를 스크립트 데이터로만 그리는 화면이면 원문에서 주소를 찾는다
        return extract_questions(html, base) or extract_kin_links_raw(html)

    def autocomplete(self, query: str) -> list[str]:
        """네이버 검색창 자동완성 목록 (사람들이 실제로 많이 치는 검색어)."""
        params = {
            "q": query, "con": 1, "frm": "nv", "ans": 2, "r_format": "json", "r_enc": "UTF-8",
            "r_unicode": 0, "t_koreng": 1, "run": 2, "rev": 4, "q_enc": "UTF-8", "st": 100,
        }
        text = self._get_html(AUTOCOMPLETE_URL, params=params, site="네이버 자동완성")
        return parse_autocomplete(text)

    def search_kin_ranked(self, query: str, count: int = 10) -> list[RawQuestion]:
        """지식iN 탭 정확도순 상위 글. (API 키가 있으면 API, 없으면 지식iN 검색 화면)"""
        if self.credentials:
            return self.search_api(query, count, sort="sim")
        html = self._get_html(WEB_SEARCH_URL, params={"query": query, "section": "kin"})
        return extract_questions(html, WEB_SEARCH_URL)[:count]

    def _get_html(self, url: str, params: dict | None = None, headers: dict | None = None, site: str = "지식iN") -> str:
        with self._lock:
            wait = self._last_web_request + self.delay_seconds - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                raise NaverError(f"{site} 연결 실패: {e.__class__.__name__}") from e
            finally:
                self._last_web_request = time.monotonic()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        # [연결 점검]에서 실제로 받은 화면을 살펴볼 수 있게 마지막 응답을 남겨 둔다
        self.last_status = r.status_code
        self.last_html = r.text or ""
        if r.status_code in (403, 429):
            raise NaverError(f"{site} 이(가) 요청을 거부했습니다({r.status_code}). 수집 간격을 늘려주세요.", fatal=True)
        if r.status_code != 200:
            raise NaverError(f"{site} 응답 오류 {r.status_code}")
        return r.text


def _api_error_detail(r) -> str:
    """오류 응답에서 사람이 읽을 메시지만 뽑는다. (개발자센터: errorMessage / 네이버클라우드: error.message)"""
    try:
        data = r.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return " ".join(str(err.get(k) or "") for k in ("message", "details")).strip()
    return str(data.get("errorMessage") or data.get("message") or "")


def parse_autocomplete(text: str) -> list[str]:
    """자동완성 응답(JSON, 콜백 감싼 형태 포함)에서 검색어만 뽑는다. 형식이 조금 바뀌어도 버티도록 방어적으로."""
    text = (text or "").strip()
    m = re.match(r"^[\w$.]+\((.*)\)\s*;?\s*$", text, re.S)  # _jsonp_0({...}) 형태
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[str] = []

    def add(s):
        s = clean_text(str(s))
        if s and s not in out and not s.isdigit():
            out.append(s)

    items = data.get("items") if isinstance(data, dict) else data
    for group in items or []:
        if not isinstance(group, list):
            continue
        for entry in group:
            if isinstance(entry, list) and entry and isinstance(entry[0], str):
                add(entry[0])
            elif isinstance(entry, dict):
                add(entry.get("keyword") or entry.get("value") or "")
            elif isinstance(entry, str):
                add(entry)
    return out


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
