"""상위노출 확인 1회 실행.

검색어(예: '인천 건선')를 네이버에서 검색했을 때 지금 상위에 노출되는 지식iN 글을 찾는다.
- pc     : 네이버 통합검색(PC) 결과에 나온 지식iN 글 순서
- mobile : 네이버 통합검색(모바일) 결과에 나온 지식iN 글 순서
- kin    : 지식iN 탭 정확도순 상위 글
노출 중인 글은 조회수가 계속 늘어나므로, 옛날 글이라도 답변을 달면 효과가 크다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import AppConfig, Product, naver_credentials
from .matcher import Matcher
from .naver import NaverClient, NaverError
from .searchad import compact
from .storage import Store


def all_targets(cfg: AppConfig, store: Store | None) -> list[tuple[Product, str]]:
    """확인할 (제품, 검색어): 설정 파일의 검색어 + 메인 키워드로 자동 생성해 켜 둔 검색어."""
    out: list[tuple[Product, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(p: Product, kw: str) -> None:
        key = (p.id, compact(kw))
        if key not in seen:
            seen.add(key)
            out.append((p, kw))

    for p, kw in cfg.exposure_targets():
        add(p, kw)
    if store is not None:
        for row in store.auto_keywords(enabled_only=True):
            p = cfg.product(row["product"])
            if p:
                add(p, row["keyword"])
    return out

MAX_FAILS = 3              # 연속 실패하면 이번 확인은 중단 (인터넷 끊김, 차단 등)
MAX_DETAILS_PER_CHECK = 150


@dataclass
class ExposureSummary:
    keywords: int = 0
    checks: int = 0
    posts: int = 0
    new_posts: int = 0
    details: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def text(self) -> str:
        s = (
            f"[상위노출] 검색어 {self.keywords}개, 확인 {self.checks}회, 노출 글 {self.posts}개 "
            f"(처음 본 글 {self.new_posts}개), 조회수 갱신 {self.details}건, {self.seconds:.0f}초"
        )
        if self.errors:
            s += f", 오류 {len(self.errors)}건"
        return s


def check_exposure(
    cfg: AppConfig,
    store: Store,
    client: NaverClient | None = None,
    log: Callable[[str], None] = print,
) -> ExposureSummary:
    started = time.monotonic()
    s = cfg.settings
    summary = ExposureSummary()
    targets = all_targets(cfg, store)
    summary.keywords = len(targets)
    if not targets:
        return summary
    if client is None:
        client = NaverClient(credentials=naver_credentials(), delay_seconds=s.request_delay_seconds)
    run_id = store.start_run("exposure")
    exposed: list[str] = []  # 순위가 높은 글부터 (상세 갱신 우선순위)
    fails = 0
    try:
        for product, keyword in targets:
            if fails >= MAX_FAILS:
                break
            for source in s.exposure_sources:
                try:
                    if source == "kin":
                        items = client.search_kin_ranked(keyword, s.exposure_top_n)
                    else:
                        items = client.search_integrated(keyword, source)
                except NaverError as e:
                    store.record_exposure(product.id, keyword, source, [], error=str(e))
                    summary.errors.append(f"'{keyword}' ({source}): {e}")
                    log(f"  ! '{keyword}' {source} 확인 실패: {e}")
                    fails += 1
                    if e.fatal or fails >= MAX_FAILS:
                        summary.errors.append(f"연속 {fails}번 실패해 이번 상위노출 확인을 중단합니다.")
                        fails = MAX_FAILS
                        break
                    continue
                fails = 0
                items = items[: s.exposure_top_n]
                for rq in items:
                    if store.upsert_raw(rq, f"노출:{keyword}", feed=False):
                        summary.new_posts += 1
                store.record_exposure(product.id, keyword, source, [rq.doc_id for rq in items])
                summary.checks += 1
                exposed.extend(rq.doc_id for rq in items)

        unique = list(dict.fromkeys(exposed))
        summary.posts = len(unique)
        matcher = Matcher(cfg.products)
        store.classify(matcher, unique)

        # 조회수·답변 수 갱신 (지난 확인의 절반 주기보다 오래된 것만)
        refresh_hours = max(1.0, s.exposure_interval_hours / 2)
        detail_fails = 0
        for row in store.stale_details(unique, refresh_hours, MAX_DETAILS_PER_CHECK):
            try:
                detail = client.fetch_detail(row["url"])
            except NaverError as e:
                store.mark_detail_failed(row["doc_id"], str(e))
                detail_fails += 1
                if e.fatal or detail_fails >= MAX_FAILS:
                    summary.errors.append(f"상세 페이지 확인 중단: {e}")
                    break
                continue
            detail_fails = 0
            store.update_detail(row["doc_id"], detail)
            summary.details += 1
        store.classify(matcher, unique)
    finally:
        summary.seconds = time.monotonic() - started
        store.finish_run(
            run_id,
            queries=summary.checks,
            fetched=summary.posts,
            new_total=summary.new_posts,
            details=summary.details,
            errors=summary.errors[:50],
        )
    return summary
