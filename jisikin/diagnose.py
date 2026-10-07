"""설정 · API 키 · 네이버 연결 점검. (CLI 의 check 명령과 대시보드의 [연결 점검] 버튼이 함께 사용)"""
from __future__ import annotations

import platform
import re
import sys

from . import __version__
from . import config as config_mod
from .collector import effective_interval, estimate_api_calls_per_day, make_client, resolve_mode
from .config import AppConfig
from .calc import calc_status, check_connection
from .drafter import ai_status
from .exposure import all_targets
from .naver import NaverClient, NaverError, debug_snippets
from .searchad import SearchAdClient
from .storage import ApiBudget, Store


def run_diagnostics(
    cfg: AppConfig,
    client: NaverClient | None = None,
    store: Store | None = None,
    ad_client: SearchAdClient | None = None,
    calc_session=None,
) -> tuple[bool, list[str]]:
    ok = True
    lines = [f"지식iN 수집기 {__version__} / Python {platform.python_version()} / {platform.system()} {platform.release()}"]
    for p in cfg.products:
        lines.append(f"[OK] {p.name}: 키워드 {len(p.keywords)}개, 카테고리 {len(p.categories)}개, 검색어 {len(p.search_queries())}개")

    creds = config_mod.naver_credentials()
    mode = resolve_mode(cfg)
    lines.append(f"[{'OK' if creds else '--'}] 네이버 API 키: {'있음' if creds else '없음 (웹 검색 모드로 동작)'}")
    interval = effective_interval(cfg)
    stretched = f" (검색어가 많아 {cfg.settings.interval_minutes}분 → 자동 조정)" if interval > cfg.settings.interval_minutes > 0 else ""
    lines.append(f"     수집 방식: {mode}, 자동 수집 간격: {interval}분{stretched}")
    if mode == "api":
        limit = cfg.settings.api_daily_limit
        used = ApiBudget(store, limit).used() if store is not None else None
        today = f", 오늘 사용 {used:,}회" if used is not None else ""
        cap = f" / 하루 상한 {limit:,}회 (네이버 무료 25,000회)" if limit > 0 else " (상한 없음 — 25,000회를 넘으면 네이버가 거부하거나 과금될 수 있음)"
        lines.append(f"     API 호출: 새 질문 수집에 하루 약 {estimate_api_calls_per_day(cfg):,}회 예상{today}{cap}")

    client = client or make_client(cfg, store)
    query = cfg.all_search_queries()[0]
    items = []
    try:
        items = client.search_api(query, 5) if mode == "api" else client.search_web(query)
        where = mode
        if mode == "api" and getattr(client, "api_provider", None):
            from .naver import API_PROVIDER_NAMES

            where = f"api · {API_PROVIDER_NAMES[client.api_provider]}"
        lines.append(f"[{'OK' if items else '??'}] '{query}' 검색 ({where}): {len(items)}건")
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
            views = f"{d.views:,}" if d.views is not None else "모름"
            lines.append(
                f"[{'OK' if good else '??'}] 상세 페이지: 제목={'O' if d.title else 'X'} "
                f"본문={'O' if d.body else 'X'} 답변수={answers} 작성일={asked} 조회수={views} 내공={d.reward or '-'}"
            )
            if not good or d.views is None:
                lines += _page_debug(client, "상세", {
                    "날짜": r"20\d{2}\s?[.\-/]\s?\d{1,2}\s?[.\-/]\s?\d{1,2}|\d+\s*(?:분|시간|일)\s*전|작성일|등록일",
                    "조회": r"조회",
                    "답변수": r"answer_?[Cc]ount|답변\s*\d",
                })
        except NaverError as e:
            ok = False
            lines.append(f"[오류] 상세 페이지: {e}")

    # 상위노출: 제품이 다른 검색어 2개까지 확인 (한 검색어는 원래 지식iN 이 안 뜰 수도 있으므로)
    targets = all_targets(cfg, store)
    samples: list[str] = []
    seen_products: set[str] = set()
    for product, keyword in targets:
        if product.id not in seen_products and len(samples) < 2:
            seen_products.add(product.id)
            samples.append(keyword)
    exposure_zero = True
    for i, keyword in enumerate(samples):
        for source, label in (("pc", "통합검색 PC"), ("mobile", "통합검색 모바일")):
            if source not in cfg.settings.exposure_sources:
                continue
            try:
                found = client.search_integrated(keyword, source)
                lines.append(f"[{'OK' if found else '??'}] 상위노출 '{keyword}' {label}: 지식iN 글 {len(found)}개")
                for n, it in enumerate(found[:3], start=1):
                    lines.append(f"     {n}. {it.title}  ({it.url})")
                if found:
                    exposure_zero = False
                elif i == 0:  # 첫 검색어에서만 화면 일부를 보여줌 (결과가 너무 길어지지 않게)
                    lines += _page_debug(client, label, {"지식iN 링크": r"kin\.naver\.com", "지식iN 글자": r"지식iN"})
            except NaverError as e:
                ok = False
                lines.append(f"[오류] 상위노출 '{keyword}' {label}: {e}")
    if samples and "views" in cfg.settings.exposure_sources:
        try:
            found = client.search_kin_by_views(samples[0], 5)
            lines.append(f"[{'OK' if found else '??'}] 상위노출 '{samples[0]}' 지식iN 조회수순: 지식iN 글 {len(found)}개")
            for n, it in enumerate(found[:3], start=1):
                lines.append(f"     {n}. {it.title}  ({it.url})")
            if not found:
                lines += _page_debug(client, "지식iN 조회수순", {"지식iN 링크": r"docId=", "조회수": r"조회"})
        except NaverError as e:
            ok = False
            lines.append(f"[오류] 상위노출 '{samples[0]}' 지식iN 조회수순: {e}")
    if samples and exposure_zero:
        ok = False
        lines.append("     통합검색에서 지식iN 글을 하나도 못 찾았습니다. 위 '진단' 줄을 Claude 에게 보내주세요.")

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
    calc_products = [p.name for p in cfg.products if p.calc_tools]
    if calc_products:
        if calc_status()[0]:
            calc_ok, calc_msg = check_connection(session=calc_session)
            if not calc_ok:
                ok = False
            lines.append(f"[{'OK' if calc_ok else '오류'}] 명연당 계산 ({', '.join(calc_products)} 초안): {calc_msg}")
        else:
            lines.append(f"[--] 명연당 계산 ({', '.join(calc_products)} 초안): {calc_status()[1]}")
    lines += _kin_rank_check(client, store)
    return ok, lines


def _kin_rank_check(client, store) -> list[str]:
    """[작업 결과] 답변 순위: 답변완료한 지식iN 글 하나를 열어 답변 칸·좋아요를 읽을 수 있는지 (실패해도 전체 점검은 OK)."""
    from bs4 import BeautifulSoup

    from .tracker import _answer_items, _likes, kin_answer_position

    item = next((i for i in (store.answered_items() if store else []) if i["platform"] == "kin" and (i.get("draft") or "").strip()), None)
    if item is None:
        return ["[--] 지식iN 답변 순위: 답변완료한 지식iN 글이 없어 확인하지 않음"]
    try:
        html = client._get_html(item["url"])
    except NaverError as e:
        return [f"[??] 지식iN 답변 순위: 질문 페이지를 못 열었습니다 ({e})"]
    soup = BeautifulSoup(html, "html.parser")
    items = _answer_items(soup)
    likes = [_likes(e) for e in items]
    pos = kin_answer_position(html, item["draft"])
    good = bool(pos.get("rank")) and any(n is not None for n in likes)
    out = [
        f"[{'OK' if good else '??'}] 지식iN 답변 순위 '{item['title'][:25]}': 답변 칸 {len(items)}개, "
        f"좋아요 읽음 {sum(n is not None for n in likes)}개 {likes[:8]}, "
        f"내 답변 {str(pos.get('rank')) + '번째' if pos.get('rank') else '못 찾음'}"
    ]
    if not good:
        out += _page_debug(client, "답변 순위", {
            "답변 칸": r"answer-content__item|_answer\b|answerDetail",
            "좋아요": r"좋아요|추천|공감|recommend|like|sympathy",
        })
    return out


# 'captcha' 같은 영어 단어는 정상 검색 화면의 스크립트에도 들어 있어서, 차단 화면에만 나오는 문구로 판단한다
_BLOCK_WORDS = ("자동입력 방지", "비정상적인 접근", "보안 절차", "접근이 제한", "일시적으로 제한")


def _page_debug(client, label: str, patterns: dict[str, str]) -> list[str]:
    """실제로 받은 화면의 크기·제목·차단 여부와 주요 글자 주변을 짧게 보여준다 (Claude 가 구조를 파악하도록)."""
    html = getattr(client, "last_html", "") or ""
    if not html:
        return []
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:40] if m else "-"
    blocked = [w for w in _BLOCK_WORDS if w.lower() in html.lower()]
    blocked_text = "/".join(blocked) if blocked else "없음"
    out = [
        f"     진단({label}): HTTP {getattr(client, 'last_status', '?')}, {len(html) // 1024}KB, "
        f"제목={title}, 차단/캡차 문구={blocked_text}"
    ]
    for name, pat in patterns.items():
        count = len(re.findall(pat, html))
        out.append(f"       '{name}' {count}회")
        for s in debug_snippets(html, pat, limit=2, width=80):
            out.append(f"         … {s[:200]}")
    return out


def main_check() -> int:
    cfg = config_mod.load_config()
    print(f"[OK] 설정 파일: {config_mod.CONFIG_PATH}")
    ok, lines = run_diagnostics(cfg)
    print("\n".join(lines))
    sys.stdout.flush()
    return 0 if ok else 1
