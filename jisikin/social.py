"""유튜브 · 쓰레드에서 제품 관련 영상과 글을 찾는다.

- 유튜브: YouTube Data API v3 (search.list 로 최신 영상 → videos.list 로 조회수·좋아요·댓글 수)
- 쓰레드: Threads API keyword_search (threads_keyword_search 권한이 있는 토큰 필요)

찾은 글은 지식iN 질문과 같은 규칙(제품 키워드·카테고리 점수)으로 분류하고,
직원이 댓글을 단 뒤 [✓ 댓글완료] 를 누르는 방식으로 관리한다. 댓글을 자동으로 올리지는 않는다.
"""
from __future__ import annotations

import html
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from .config import AppConfig, threads_token, youtube_key
from .matcher import Matcher
from .storage import Store, iso, now_kst

PLATFORMS = {"youtube": "유튜브", "threads": "쓰레드"}

YT_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YT_SEARCH_UNITS = 100      # search.list 1회 사용량
YT_VIDEOS_UNITS = 1        # videos.list 1회 사용량
# 유튜브 할당량은 태평양 시간 자정에 초기화된다 (서머타임은 무시하고 1시간 일찍 넘어가도 상한 안이면 문제없음)
YT_QUOTA_TZ = timezone(timedelta(hours=-8))

TH_SEARCH_URL = "https://graph.threads.net/v1.0/keyword_search"
TH_REFRESH_URL = "https://graph.threads.net/refresh_access_token"
TH_FIELDS = "id,text,media_type,permalink,timestamp,username"
TH_REFRESH_DAYS = 7        # 장기 토큰(60일)을 이 주기로 연장해 만료되지 않게 한다

MAX_FAILS = 3              # 한 곳에서 검색이 연속으로 이만큼 실패하면 이번 수집에선 그만


class SocialError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # True 면 남은 검색도 같은 이유로 실패할 것 (키 오류, 할당량 소진 등)


@dataclass
class SocialPost:
    platform: str               # youtube / threads
    post_id: str                # yt:<영상 id> / th:<글 id>
    url: str
    title: str = ""
    body: str = ""
    author: str = ""
    author_url: str = ""
    thumbnail: str = ""
    published_at: datetime | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None


@dataclass
class SocialSummary:
    queries: int = 0
    fetched: int = 0
    new_total: int = 0
    new_relevant: int = 0
    per_platform: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def text(self) -> str:
        per = ", ".join(f"{PLATFORMS[k]} {v}건" for k, v in self.per_platform.items())
        s = (
            f"[유튜브·쓰레드] 검색어 {self.queries}개, 가져온 글 {self.fetched}건 ({per or '-'}), "
            f"새 글 {self.new_total}건 (관련 {self.new_relevant}건), {self.seconds:.0f}초"
        )
        if self.errors:
            s += f", 오류 {len(self.errors)}건"
        return s


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # 유튜브 2026-09-29T10:00:00Z / 쓰레드 2026-09-29T10:00:00+0000
        v = value.replace("Z", "+00:00")
        if len(v) >= 5 and v[-5] in "+-" and v[-3] != ":":
            v = v[:-2] + ":" + v[-2:]
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _error_body(res: requests.Response) -> dict:
    try:
        return (res.json() or {}).get("error") or {}
    except ValueError:
        return {}


# ---------------------------------------------------------------- 유튜브

class YouTubeBudget:
    """유튜브 API 하루 사용량(유닛)을 세고, 상한에 닿으면 멈춘다. (무료 10,000 유닛/일)"""

    def __init__(self, store: Store, daily_limit: int):
        self.store = store
        self.daily_limit = daily_limit

    def _key(self) -> str:
        return f"yt_units:{datetime.now(YT_QUOTA_TZ):%Y-%m-%d}"

    def used(self) -> int:
        return int(self.store.kv_get(self._key(), 0) or 0)

    def check(self, units: int) -> None:
        if self.daily_limit > 0 and self.used() + units > self.daily_limit:
            raise SocialError(
                f"오늘 유튜브 API 사용량 상한({self.daily_limit:,})에 도달해 유튜브 검색을 멈췄습니다. "
                "오후 5시(한국 시간)쯤 다시 시작합니다.",
                fatal=True,
            )

    def record(self, units: int) -> None:
        self.store.kv_incr(self._key(), units)


class YouTubeClient:
    def __init__(self, api_key: str, budget: YouTubeBudget | None = None, session: requests.Session | None = None):
        self.api_key = api_key
        self.budget = budget
        self.session = session or requests.Session()

    def _get(self, url: str, params: dict, units: int) -> dict:
        if self.budget:
            self.budget.check(units)
        try:
            res = self.session.get(url, params={**params, "key": self.api_key}, timeout=20)
        except requests.RequestException as e:
            raise SocialError(f"유튜브에 연결할 수 없습니다: {e.__class__.__name__}") from e
        if self.budget:
            self.budget.record(units)  # 실패한 요청도 할당량을 쓴다
        if res.status_code == 200:
            return res.json()
        err = _error_body(res)
        reason = ((err.get("errors") or [{}])[0] or {}).get("reason", "")
        msg = err.get("message") or res.reason
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            raise SocialError("유튜브 API 오늘 할당량(10,000)을 모두 썼습니다. 오후 5시(한국 시간)쯤 풀립니다.", fatal=True)
        if reason == "keyInvalid" or (reason == "badRequest" and "api key" in msg.lower()):
            raise SocialError("유튜브 API 키가 올바르지 않습니다. [설정] > API 키를 확인하세요.", fatal=True)
        if reason in ("accessNotConfigured", "forbidden", "ipRefererBlocked", "keyExpired"):
            raise SocialError(
                f"유튜브 API 를 쓸 수 없습니다 ({reason}). Google Cloud 콘솔에서 'YouTube Data API v3' 를 사용 설정했는지, "
                "키에 API 제한을 걸었다면 이 API 가 허용되어 있는지 확인하세요.",
                fatal=True,
            )
        raise SocialError(f"유튜브 API 오류 ({res.status_code}): {msg}")

    def search(self, query: str, since: datetime, limit: int) -> list[SocialPost]:
        data = self._get(
            YT_SEARCH_URL,
            {
                "part": "snippet", "q": query, "type": "video", "order": "date",
                "maxResults": max(1, min(limit, 50)),
                "publishedAfter": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "regionCode": "KR", "relevanceLanguage": "ko",
            },
            YT_SEARCH_UNITS,
        )
        posts: list[SocialPost] = []
        for it in data.get("items") or []:
            vid = (it.get("id") or {}).get("videoId")
            if not vid:
                continue
            sn = it.get("snippet") or {}
            thumbs = sn.get("thumbnails") or {}
            posts.append(
                SocialPost(
                    platform="youtube",
                    post_id=f"yt:{vid}",
                    url=f"https://www.youtube.com/watch?v={vid}",
                    title=html.unescape(sn.get("title") or ""),
                    body=html.unescape(sn.get("description") or ""),
                    author=html.unescape(sn.get("channelTitle") or ""),
                    author_url=f"https://www.youtube.com/channel/{sn['channelId']}" if sn.get("channelId") else "",
                    thumbnail=(thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
                    published_at=_parse_time(sn.get("publishedAt")),
                )
            )
        if posts:
            self._fill_stats(posts)
        return posts

    def _fill_stats(self, posts: list[SocialPost]) -> None:
        """조회수·좋아요·댓글 수와 전체 설명(검색 결과의 설명은 잘려 있음)."""
        by_id = {p.post_id[3:]: p for p in posts}
        data = self._get(YT_VIDEOS_URL, {"part": "statistics,snippet", "id": ",".join(by_id)}, YT_VIDEOS_UNITS)
        for v in data.get("items") or []:
            p = by_id.get(v.get("id"))
            if not p:
                continue
            st = v.get("statistics") or {}
            p.views, p.likes, p.comments = _int(st.get("viewCount")), _int(st.get("likeCount")), _int(st.get("commentCount"))
            desc = (v.get("snippet") or {}).get("description")
            if desc:
                p.body = desc


# ---------------------------------------------------------------- 쓰레드

class ThreadsClient:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.token = token
        self.session = session or requests.Session()

    def search(self, query: str, since: datetime, limit: int) -> list[SocialPost]:
        params = {
            "q": query, "search_type": "RECENT", "fields": TH_FIELDS,
            "since": int(since.timestamp()), "limit": max(1, min(limit, 100)), "access_token": self.token,
        }
        try:
            res = self.session.get(TH_SEARCH_URL, params=params, timeout=20)
        except requests.RequestException as e:
            raise SocialError(f"쓰레드에 연결할 수 없습니다: {e.__class__.__name__}") from e
        if res.status_code != 200:
            err = _error_body(res)
            code, msg = err.get("code"), err.get("message") or res.reason
            if code == 190 or res.status_code == 401:
                raise SocialError("쓰레드 토큰이 만료되었거나 올바르지 않습니다. [설정] > API 키에 새 토큰을 넣어 주세요.", fatal=True)
            if code in (10, 200) or "permission" in msg.lower():
                raise SocialError(
                    "쓰레드 토큰에 키워드 검색 권한(threads_keyword_search)이 없습니다. "
                    "Meta 개발자 앱에서 권한을 추가한 뒤 토큰을 다시 발급해 주세요.",
                    fatal=True,
                )
            if code in (4, 17, 32, 613) or res.status_code == 429:
                raise SocialError("쓰레드 검색 한도(24시간에 2,200회)에 걸렸습니다. 잠시 후 다시 시도합니다.", fatal=True)
            raise SocialError(f"쓰레드 API 오류 ({res.status_code}): {msg}")
        posts = []
        for p in (res.json() or {}).get("data") or []:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            user = p.get("username") or ""
            posts.append(
                SocialPost(
                    platform="threads",
                    post_id=f"th:{pid}",
                    url=p.get("permalink") or (f"https://www.threads.net/@{user}" if user else "https://www.threads.net/"),
                    body=p.get("text") or "",
                    author=user,
                    author_url=f"https://www.threads.net/@{user}" if user else "",
                    published_at=_parse_time(p.get("timestamp")),
                )
            )
        return posts

    def refresh(self) -> str | None:
        """장기 토큰 연장 (60일 → 새 60일). 실패하면 None."""
        try:
            res = self.session.get(
                TH_REFRESH_URL, params={"grant_type": "th_refresh_token", "access_token": self.token}, timeout=20
            )
            if res.status_code == 200:
                return (res.json() or {}).get("access_token") or None
        except (requests.RequestException, ValueError):
            pass
        return None


# ---------------------------------------------------------------- 분류 · 수집

def social_matcher(cfg: AppConfig) -> Matcher:
    """지식iN 분류 규칙 + 제품별 social.keywords(브랜드명 등)도 제품 키워드로 친다."""
    products = []
    for p in cfg.products:
        extra = [k for k in p.social_queries() if k not in p.keywords]
        products.append(replace(p, keywords=list(p.keywords) + extra))
    return Matcher(products)


def social_status() -> dict[str, bool]:
    return {"youtube": youtube_key() is not None, "threads": threads_token() is not None}


def make_clients(cfg: AppConfig, store: Store) -> dict[str, object]:
    clients: dict[str, object] = {}
    key, token = youtube_key(), threads_token()
    if key:
        clients["youtube"] = YouTubeClient(key, YouTubeBudget(store, cfg.settings.youtube_daily_units))
    if token:
        clients["threads"] = ThreadsClient(token)
    return clients


def maybe_refresh_threads_token(
    client: ThreadsClient, store: Store, save_token: Callable[[str], None] | None, log: Callable[[str], None]
) -> None:
    last = store.kv_get("threads_token_refreshed_at")
    if last and (now_kst() - datetime.fromisoformat(last)) < timedelta(days=TH_REFRESH_DAYS):
        return
    new = client.refresh()
    store.kv_set("threads_token_refreshed_at", iso(now_kst()))  # 실패해도 매번 다시 시도하지 않게
    if new and new != client.token:
        client.token = new
        if save_token:
            save_token(new)
        log("쓰레드 토큰을 연장했습니다 (앞으로 60일)")


def collect_social(
    cfg: AppConfig,
    store: Store,
    clients: dict[str, object] | None = None,
    log: Callable[[str], None] = print,
    save_threads_token: Callable[[str], None] | None = None,
) -> SocialSummary:
    started = time.monotonic()
    s = cfg.settings
    summary = SocialSummary()
    if clients is None:
        clients = make_clients(cfg, store)
    run_id = store.start_run("social")
    touched: list[str] = []
    new_ids: set[str] = set()
    since = now_kst() - timedelta(days=s.social_max_age_days)
    try:
        queries = cfg.social_queries()
        summary.queries = len(queries)
        if not clients:
            summary.errors.append("유튜브 API 키나 쓰레드 토큰이 없습니다. [설정] > API 키에 넣어 주세요.")
        if isinstance(clients.get("threads"), ThreadsClient):
            maybe_refresh_threads_token(clients["threads"], store, save_threads_token, log)
        for platform, client in clients.items():
            limit = s.youtube_results if platform == "youtube" else s.threads_results
            fails = 0
            for q in queries:
                try:
                    posts = client.search(q, since, limit)
                except SocialError as e:
                    summary.errors.append(f"{PLATFORMS[platform]} '{q}': {e}")
                    log(f"  ! {PLATFORMS[platform]} '{q}' 검색 실패: {e}")
                    fails += 1
                    if e.fatal or fails >= MAX_FAILS:
                        break
                    continue
                fails = 0
                for post in posts:
                    summary.fetched += 1
                    summary.per_platform[platform] = summary.per_platform.get(platform, 0) + 1
                    touched.append(post.post_id)
                    if store.upsert_social(post, q):
                        new_ids.add(post.post_id)
        store.classify_social(social_matcher(cfg), touched)
        summary.new_total = len(new_ids)
        summary.new_relevant = sum(1 for pid in new_ids if (store.get_social(pid) or {}).get("product"))
        store.purge_social(s.keep_days)
    except Exception as e:
        summary.errors.append(f"내부 오류: {e!r}")
        raise
    finally:
        summary.seconds = time.monotonic() - started
        store.finish_run(
            run_id, queries=summary.queries, fetched=summary.fetched, new_total=summary.new_total,
            new_relevant=summary.new_relevant, errors=summary.errors[:50],
        )
    return summary
