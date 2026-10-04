"""네이버 카페 글 수집 (지식iN 과 같은 네이버 검색 API 키 사용).

제품별 cafe.keywords (예: 명운연구소 [작명소 추천, 개명 후기])로 카페 글을 최신순으로 찾아
[카페] 탭에 쌓는다. 직원이 그 카페에 가입해 직접 댓글을 단다. (자동 댓글 기능은 넣지 않음)
API 는 작성일을 주지 않으므로 처음 찾은 시각으로 새 글을 판단한다.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import AppConfig
from .naver import NaverClient, NaverError, clean_text
from .social import SocialPost, social_matcher
from .storage import Store

MAX_FAILS = 3
CAFE_API_HINT = (
    "네이버클라우드 콘솔 → NAVER API HUB → Application 에서 지식iN 키의 [수정]을 눌러 "
    "검색 API 중 '카페글'도 선택(체크)한 뒤 저장해 주세요."
)
_ARTICLE_RE = re.compile(r"cafe\.naver\.com/([A-Za-z0-9_\-]+)/(\d+)")


@dataclass
class CafeSummary:
    queries: int = 0
    fetched: int = 0
    new_total: int = 0
    new_relevant: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def text(self) -> str:
        s = f"[카페] 검색어 {self.queries}개, 글 {self.fetched}건, 새 글 {self.new_total}건 (관련 {self.new_relevant}건), {self.seconds:.0f}초"
        if self.errors:
            s += f", 오류 {len(self.errors)}건"
        return s


def to_post(item: dict) -> SocialPost | None:
    """API 결과 1건 → SocialPost. 카페 글 주소가 아니면 None."""
    link = (item.get("link") or "").strip()
    m = _ARTICLE_RE.search(link)
    if not m:
        return None
    cafe_id, article = m.group(1), m.group(2)
    return SocialPost(
        platform="cafe",
        post_id=f"cafe:{cafe_id}/{article}",
        url=f"https://cafe.naver.com/{cafe_id}/{article}",
        title=clean_text(item.get("title")),
        body=clean_text(item.get("description")),
        author=clean_text(item.get("cafename")),
        author_url=(item.get("cafeurl") or "").strip(),
    )


def collect_cafe(
    cfg: AppConfig,
    store: Store,
    client: NaverClient | None = None,
    log: Callable[[str], None] = print,
) -> CafeSummary:
    started = time.monotonic()
    summary = CafeSummary()
    targets = cfg.cafe_queries()
    summary.queries = len(targets)
    if not targets:
        return summary
    if client is None:
        from .collector import make_client  # 하루 API 호출 상한을 지키는 클라이언트

        client = make_client(cfg, store)
    run_id = store.start_run("cafe")
    touched: list[str] = []
    new_ids: set[str] = set()
    fails = 0
    try:
        for _product, q in targets:
            try:
                items = client.search_cafe(q, cfg.settings.cafe_results, sort="date")
            except NaverError as e:
                msg = str(e)
                if e.auth:  # 지식iN 은 되는데 카페만 막힌 경우: 키에 '카페글' 검색이 꺼져 있음
                    msg += f" → {CAFE_API_HINT}"
                summary.errors.append(f"카페 '{q}': {msg}")
                log(f"  ! 카페 '{q}' 검색 실패: {e}")
                fails += 1
                if e.fatal or fails >= MAX_FAILS:
                    break
                continue
            fails = 0
            for item in items:
                post = to_post(item)
                if post is None:
                    continue
                summary.fetched += 1
                touched.append(post.post_id)
                if store.upsert_social(post, q):
                    new_ids.add(post.post_id)
        store.classify_social(social_matcher(cfg), touched)
        summary.new_total = len(new_ids)
        summary.new_relevant = sum(1 for pid in new_ids if (store.get_social(pid) or {}).get("product"))
    finally:
        summary.seconds = time.monotonic() - started
        store.finish_run(
            run_id, queries=summary.queries, fetched=summary.fetched, new_total=summary.new_total,
            new_relevant=summary.new_relevant, errors=summary.errors[:50],
        )
    return summary
