"""설정 · API 키 · 네이버 연결 점검. (CLI 의 check 명령과 대시보드의 [연결 점검] 버튼이 함께 사용)"""
from __future__ import annotations

import platform
import sys

from . import __version__
from . import config as config_mod
from .collector import effective_interval, estimate_api_calls_per_day, resolve_mode
from .config import AppConfig
from .drafter import ai_status
from .exposure import all_targets
from .naver import NaverClient, NaverError
from .searchad import SearchAdClient
from .storage import Store


def run_diagnostics(
    cfg: AppConfig,
    client: NaverClient | None = None,
    store: Store | None = None,
    ad_client: SearchAdClient | None = None,
) -> tuple[bool, list[str]]:
    ok = True
    lines = [f"지식iN 수집기 {__version__} / Python {platform.python_version()} / {platform.system()} {platform.release()}"]
    for p in cfg.products:
        lines.append(f"[OK] {p.name}: 키워드 {len(p.keywords)}개, 카테고리 {len(p.categories)}개, 검색어 {len(p.search_queries())}개")

    creds = config_mod.naver_credentials()
    mode = resolve_mode(cfg)
    lines.append(f"[{'OK' if creds else '--'}] 네이버 API 키: {'있음' if creds else '없음 (웹 검색 모드로 동작)'}")
    lines.append(f"     수집 방식: {mode}, 자동 수집 간격: {effective_interval(cfg)}분")
    if mode == "api":
        calls = estimate_api_calls_per_day(cfg)
        warn = "  ← 하루 한도(25,000회)에 가까움. 간격을 늘리거나 검색어를 줄이세요." if calls > 20000 else ""
        lines.append(f"     예상 API 호출: 하루 약 {calls:,}회{warn}")

    client = client or NaverClient(credentials=creds, delay_seconds=cfg.settings.request_delay_seconds)
    query = cfg.all_search_queries()[0]
    items = []
    try:
        items = client.search_api(query, 5) if mode == "api" else client.search_web(query)
        lines.append(f"[{'OK' if items else '??'}] '{query}' 검색 ({mode}): {len(items)}건")
        for it in items[:3]:
            lines.append(f"     · {it.title}  ({it.url})")
            if mode == "web":
                ans = "?" if it.answer_count is None else it.answer_count
                asked = f"{it.asked_at:%Y-%m-%d}" if it.asked_at else "?"
                lines.append(f"       목록에서 읽은 값: 답변수={ans}, 작성일={asked}, 요약={'O' if it.snippet else 'X'}")
        if not items:
            ok = False
            lines.append("     결과가 0건입니다. 웹 모드라면 네이버 화면 구조가 바뀌었을 수 있습니다.")
    except NaverError as e:
        ok = False
        lines.append(f"[오류] 검색 실패: {e}")

    if items and cfg.settings.fetch_details:
        try:
            d = client.fetch_detail(items[0].url)
            answers = d.answer_count if d.answer_count is not None else "모름"
            asked = f"{d.asked_at:%Y-%m-%d %H:%M}" if d.asked_at else "모름"
            good = bool(d.title and d.body and d.answer_count is not None and d.asked_at)
            if not good:
                ok = False
            lines.append(
                f"[{'OK' if good else '??'}] 상세 페이지: 제목={'O' if d.title else 'X'} "
                f"본문={'O' if d.body else 'X'} 답변수={answers} 작성일={asked} 내공={d.reward or '-'}"
            )
        except NaverError as e:
            ok = False
            lines.append(f"[오류] 상세 페이지: {e}")

    targets = all_targets(cfg, store)
    if targets:
        product, keyword = targets[0]
        for source, label in (("pc", "통합검색 PC"), ("mobile", "통합검색 모바일")):
            if source not in cfg.settings.exposure_sources:
                continue
            try:
                found = client.search_integrated(keyword, source)
                lines.append(f"[{'OK' if found else '??'}] 상위노출 '{keyword}' {label}: 지식iN 글 {len(found)}개")
                for it in found[:3]:
                    lines.append(f"     {found.index(it) + 1}. {it.title}  ({it.url})")
                if not found:
                    lines.append("     0개입니다. 이 검색어에 지식iN 글이 안 뜨거나, 네이버 화면 구조가 바뀌었을 수 있습니다.")
            except NaverError as e:
                ok = False
                lines.append(f"[오류] 상위노출 '{keyword}' {label}: {e}")

    # 검색어 자동 생성에 쓰는 자동완성 / 검색광고 API
    sample = (cfg.products[0].keywords or [cfg.products[0].name])[0]
    try:
        sugg = client.autocomplete(sample)
        lines.append(f"[{'OK' if sugg else '??'}] 네이버 자동완성 '{sample}': {len(sugg)}개 {' / '.join(sugg[:5])}")
    except NaverError as e:
        ok = False
        lines.append(f"[오류] 네이버 자동완성: {e}")
    creds = config_mod.searchad_credentials()
    if ad_client is None and creds:
        ad_client = SearchAdClient(*creds)
    if ad_client:
        try:
            rows = ad_client.keyword_stats([sample])
            top = rows[0] if rows else None
            detail = f" (예: {top['keyword']} PC {top['pc']} / 모바일 {top['mobile']})" if top else ""
            lines.append(f"[OK] 검색광고 API: 연관 키워드 {len(rows)}개{detail}")
        except NaverError as e:
            ok = False
            lines.append(f"[오류] 검색광고 API: {e}")
    else:
        lines.append("[--] 검색광고 API: 키 없음 (없어도 동작, 있으면 월간 검색수로 검색어 순위를 매김)")

    ai_ok, reason = ai_status()
    lines.append(f"[{'OK' if ai_ok else '--'}] AI (답변 초안 · 검색어 추천): {reason}")
    return ok, lines


def main_check() -> int:
    cfg = config_mod.load_config()
    print(f"[OK] 설정 파일: {config_mod.CONFIG_PATH}")
    ok, lines = run_diagnostics(cfg)
    print("\n".join(lines))
    sys.stdout.flush()
    return 0 if ok else 1
