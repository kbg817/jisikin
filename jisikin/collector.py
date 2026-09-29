"""한 번의 수집 실행: 검색 → 저장 → 분류 → 상세 정보 보강 → 정리."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import AppConfig, naver_credentials
from .matcher import Matcher
from .naver import NaverClient, NaverError, RawQuestion
from .storage import Store

WEB_MAX_PAGES = 3          # 웹 검색 모드에서 검색어당 최대 페이지 (1페이지 = 10건)
WEB_MIN_INTERVAL = 30      # 웹 검색 모드의 최소 자동 수집 간격(분) — 네이버에 부담을 주지 않도록
MAX_DETAIL_FAILS = 3       # 상세 페이지가 연속으로 이만큼 실패하면 이번 실행에선 중단
MAX_SEARCH_FAILS = 3       # 검색이 연속으로 이만큼 실패하면 (인터넷 끊김 등) 이번 실행에선 중단


@dataclass
class RunSummary:
    mode: str
    queries: int = 0
    fetched: int = 0
    new_total: int = 0
    new_relevant: int = 0
    details: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def text(self) -> str:
        s = (
            f"[{self.mode}] 검색어 {self.queries}개, 가져온 질문 {self.fetched}건, "
            f"새 질문 {self.new_total}건 (관련 {self.new_relevant}건), 상세 {self.details}건, "
            f"{self.seconds:.0f}초"
        )
        if self.errors:
            s += f", 오류 {len(self.errors)}건"
        return s


def resolve_mode(cfg: AppConfig) -> str:
    source = cfg.settings.source
    if source == "auto":
        return "api" if naver_credentials() else "web"
    return source


def effective_interval(cfg: AppConfig) -> int:
    minutes = cfg.settings.interval_minutes
    if minutes <= 0:
        return 0
    if resolve_mode(cfg) == "web":
        return max(minutes, WEB_MIN_INTERVAL)
    return minutes


def estimate_api_calls_per_day(cfg: AppConfig) -> int:
    interval = effective_interval(cfg)
    if interval <= 0:
        return 0
    return len(cfg.all_search_queries()) * (24 * 60 // interval)


def collect(
    cfg: AppConfig,
    store: Store,
    client: NaverClient | None = None,
    log: Callable[[str], None] = print,
) -> RunSummary:
    started = time.monotonic()
    mode = resolve_mode(cfg)
    s = cfg.settings
    summary = RunSummary(mode=mode)
    if client is None:
        client = NaverClient(credentials=naver_credentials(), delay_seconds=s.request_delay_seconds)
    run_id = store.start_run(mode)
    touched: list[str] = []
    new_ids: set[str] = set()

    def save(items: list[RawQuestion], source: str) -> int:
        new_here = 0
        for rq in items:
            summary.fetched += 1
            touched.append(rq.doc_id)
            if store.upsert_raw(rq, source):
                new_ids.add(rq.doc_id)
                new_here += 1
        return new_here

    try:
        # 1) 키워드 검색
        queries = cfg.all_search_queries()
        summary.queries = len(queries)
        if mode == "api" and not client.credentials:
            summary.errors.append(
                "source: api 로 설정되어 있지만 네이버 API 키가 없습니다. [설정] > API 키를 확인하세요."
            )
            queries = []
        search_fails = 0
        for q in queries:
            try:
                if mode == "api":
                    save(client.search_api(q, s.results_per_keyword), f"검색:{q}")
                else:
                    pages = min(WEB_MAX_PAGES, max(1, -(-s.results_per_keyword // 10)))
                    for page in range(1, pages + 1):
                        items = client.search_web(q, page)
                        new_here = save(items, f"검색:{q}")
                        # 최신순이므로, 이 페이지에 새 질문이 하나도 없으면 다음 페이지도 이미 본 것
                        if not items or new_here == 0:
                            break
            except NaverError as e:
                summary.errors.append(f"'{q}': {e}")
                log(f"  ! '{q}' 검색 실패: {e}")
                search_fails += 1
                if e.fatal or search_fails >= MAX_SEARCH_FAILS:
                    if not e.fatal:
                        summary.errors.append(f"검색이 {search_fails}번 연속 실패해 이번 수집의 나머지 검색을 건너뜁니다.")
                    break
            else:
                search_fails = 0

        # 2) 분야 목록 페이지 (watch_urls)
        for p in cfg.products:
            for url in p.watch_urls:
                try:
                    save(client.fetch_list(url), f"목록:{p.name}")
                except NaverError as e:
                    summary.errors.append(f"목록 {url}: {e}")
                    log(f"  ! 목록 페이지 실패: {e}")

        # 3) 분류
        matcher = Matcher(cfg.products)
        store.classify(matcher, touched)

        # 4) 상세 정보 (답변 수, 작성일, 본문)
        if s.fetch_details and s.max_details_per_run > 0:
            fails = 0
            for row in store.pending_details(s.max_details_per_run, s.keep_days):
                try:
                    detail = client.fetch_detail(row["url"])
                except NaverError as e:
                    store.mark_detail_failed(row["doc_id"], str(e))
                    fails += 1
                    if e.fatal or fails >= MAX_DETAIL_FAILS:
                        summary.errors.append(f"상세 페이지 수집 중단: {e}")
                        break
                    continue
                fails = 0
                store.update_detail(row["doc_id"], detail)
                store.classify(matcher, [row["doc_id"]])
                summary.details += 1

        summary.new_total = len(new_ids)
        if new_ids:
            summary.new_relevant = sum(
                1 for q in (store.get(d) for d in new_ids) if q and q.get("product")
            )
        store.purge(s.keep_days)
    except Exception as e:  # 예기치 못한 오류도 기록은 남긴다
        summary.errors.append(f"내부 오류: {e!r}")
        raise
    finally:
        summary.seconds = time.monotonic() - started
        store.finish_run(
            run_id,
            queries=summary.queries,
            fetched=summary.fetched,
            new_total=summary.new_total,
            new_relevant=summary.new_relevant,
            details=summary.details,
            errors=summary.errors[:50],
        )
    return summary
