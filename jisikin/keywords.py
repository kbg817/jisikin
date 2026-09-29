"""메인 키워드(예: '건선') → 상위노출 확인용 세부 검색어 자동 생성.

후보를 모으는 곳 (있는 것만 사용):
  1) 네이버 자동완성      — 사람들이 실제로 많이 치는 검색어 (2단계까지 파고듦)
  2) 조합                — 메인 키워드 × 의도(추천/원인/병원…), 지역 × 메인 키워드
  3) AI 추천 (선택)       — Claude 가 제품 설명을 보고 질문형·고민형 검색어를 추측
  4) 검색광고 API (선택)  — 연관 키워드 + 실제 월간 검색수(PC/모바일)로 순위 계산
제품과 관련 없는 검색어는 걸러내고, 월간 검색수(없으면 추정 점수) 순으로 상위 N개를 켠다.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import AppConfig, Product, naver_credentials, searchad_credentials
from .drafter import DraftError, ai_status, ask_claude
from .matcher import Matcher
from .naver import NaverClient, NaverError
from .searchad import SearchAdClient, compact
from .storage import Store, iso, now_kst

SOURCE_LABELS = {
    "seed": "메인",
    "autocomplete": "자동완성",
    "combo": "조합",
    "ai": "AI 추천",
    "searchad": "검색광고",
    "manual": "직접",
}
MODIFIERS = ["추천", "원인", "증상", "방법", "병원", "잘하는 곳", "후기", "비용"]
DEFAULT_MAX = 20
REFRESH_DAYS = 7
MAX_VOLUME_LOOKUPS = 60  # 검색광고 API 로 검색수를 조회할 후보 수 (5개씩 묶어서 조회)

AI_SYSTEM = """당신은 네이버 검색 마케팅을 잘 아는 도우미입니다.
사람들이 네이버 검색창에 실제로 입력할 법한 짧은 검색어를 만듭니다."""

AI_USER = """제품/서비스: {name}
설명: {guide}
메인 키워드: {seed}

이 주제로 사람들이 네이버에 실제로 검색할 만한 검색어를 30개 만들어 주세요.
- 지식iN 글이 검색 결과에 잘 뜨는 질문형·고민형·추천형 검색어 위주 (예: "{seed} 원인", "{seed} 잘하는 곳", "{seed} 추천")
- 증상·상황·대상(아이, 여성 등)·방법·제품 종류를 다양하게
- 지역명 조합은 인구가 많은 지역으로 몇 개만
- 한 줄에 하나씩, 번호·설명·따옴표 없이 검색어만"""


@dataclass
class Candidate:
    keyword: str
    seed: str
    sources: list[str]
    base: float  # 검색수를 모를 때 쓰는 추정 점수
    pc: int | None = None
    mobile: int | None = None

    @property
    def volume(self) -> int | None:
        if self.pc is None and self.mobile is None:
            return None
        return (self.pc or 0) + (self.mobile or 0)


@dataclass
class ExpandResult:
    product: str
    candidates: list[Candidate] = field(default_factory=list)
    used: list[str] = field(default_factory=list)  # 실제로 사용한 출처
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def text(self) -> str:
        used = ", ".join(SOURCE_LABELS.get(u, u) for u in self.used) or "-"
        s = f"[검색어 생성] 후보 {len(self.candidates)}개 (사용: {used}), {self.seconds:.0f}초"
        if self.errors:
            s += f", 오류 {len(self.errors)}건"
        return s


def seed_settings(store: Store, product_id: str) -> dict:
    data = store.kv_get(f"seeds:{product_id}", {}) or {}
    return {
        "seeds": list(data.get("seeds") or []),
        "max": int(data.get("max") or DEFAULT_MAX),
        "generated_at": data.get("generated_at"),
        "last_error": data.get("last_error"),
    }


def save_seed_settings(store: Store, product_id: str, seeds: list[str], max_n: int) -> dict:
    cur = seed_settings(store, product_id)
    clean = []
    for s in seeds:
        s = re.sub(r"\s+", " ", s).strip()
        if s and compact(s) not in {compact(c) for c in clean}:
            clean.append(s)
    cur.update(seeds=clean[:10], max=max(1, min(int(max_n), 60)))
    store.kv_set(f"seeds:{product_id}", cur)
    return cur


def parse_ai_lines(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•·]|\d+\s*[.)]|\d+\s*-)\s*", "", line).strip().strip("\"'“”‘’`")
        if 1 < len(line) <= 30 and line not in out:
            out.append(line)
    return out


def expand(
    cfg: AppConfig,
    product: Product,
    seeds: list[str],
    client: NaverClient,
    ad_client: SearchAdClient | None = None,
    use_ai: bool = True,
    log: Callable[[str], None] = print,
) -> ExpandResult:
    started = time.monotonic()
    res = ExpandResult(product=product.id)
    pool: dict[str, Candidate] = {}
    used: set[str] = set()

    def add(kw: str, seed: str, source: str, base: float) -> Candidate | None:
        kw = re.sub(r"\s+", " ", kw or "").strip()
        if not kw or len(kw) > 40:
            return None
        key = compact(kw)
        c = pool.get(key)
        if c:
            if source not in c.sources:
                c.sources.append(source)
                c.base = max(c.base, base) + 5  # 여러 곳에서 나오면 가산
            return c
        c = pool[key] = Candidate(keyword=kw, seed=seed, sources=[source], base=base)
        return c

    for seed in seeds:
        add(seed, seed, "seed", 120)

        # 1) 자동완성 (메인 → 상위 제안어 → 그 제안어의 제안어)
        try:
            first = client.autocomplete(seed)
            used.add("autocomplete")
            for i, s in enumerate(first[:10]):
                add(s, seed, "autocomplete", 100 - i * 4)
            for s in [x for x in first if compact(x) != compact(seed)][:4]:
                for i, s2 in enumerate(client.autocomplete(s)[:8]):
                    add(s2, seed, "autocomplete", 70 - i * 3)
        except NaverError as e:
            res.errors.append(f"'{seed}' 자동완성: {e}")
            log(f"  ! '{seed}' 자동완성 실패: {e}")

        # 2) 조합
        for m in MODIFIERS:
            add(f"{seed} {m}", seed, "combo", 25)
        for r in product.exposure.regions:
            add(f"{r} {seed}", seed, "combo", 35)
        used.add("combo")

        # 3) AI 추천
        if use_ai and ai_status()[0]:
            try:
                text = ask_claude(
                    cfg,
                    AI_SYSTEM,
                    AI_USER.format(name=product.name, guide=product.answer_guide or product.name, seed=seed),
                    effort="low",
                )
                used.add("ai")
                for i, s in enumerate(parse_ai_lines(text)[:40]):
                    add(s, seed, "ai", 50 - i * 0.5)
            except DraftError as e:
                res.errors.append(f"'{seed}' AI 추천: {e}")

    # 관련 없는 검색어 거르기: 메인 키워드를 포함하거나, 제품 키워드/카테고리에 걸리면 통과
    matcher = Matcher([product])
    seed_keys = [compact(s) for s in seeds]

    def relevant(kw: str) -> bool:
        k = compact(kw)
        return any(s in k for s in seed_keys) or matcher.best(matcher.classify(kw)) is not None

    # 4) 검색광고 API: 연관 키워드 + 월간 검색수
    if ad_client:
        try:
            for i in range(0, len(seeds), 5):
                rows = ad_client.keyword_stats(seeds[i : i + 5])
                used.add("searchad")
                rows = [r for r in rows if relevant(r["keyword"])]
                rows.sort(key=lambda r: (r["pc"] or 0) + (r["mobile"] or 0), reverse=True)
                for r in rows[:50]:
                    seed = next((s for s in seeds if compact(s) in compact(r["keyword"])), seeds[i])
                    c = add(r["keyword"], seed, "searchad", 30)
                    if c:
                        c.pc, c.mobile = r["pc"], r["mobile"]
            # 검색수를 아직 모르는 후보도 조회 (추정 점수 높은 것부터)
            unknown = sorted((c for c in pool.values() if c.volume is None and relevant(c.keyword)), key=lambda c: -c.base)
            unknown = unknown[:MAX_VOLUME_LOOKUPS]
            for i in range(0, len(unknown), 5):
                batch = unknown[i : i + 5]
                stats = {compact(r["keyword"]): r for r in ad_client.keyword_stats([c.keyword for c in batch])}
                for c in batch:
                    r = stats.get(compact(c.keyword))
                    c.pc, c.mobile = (r["pc"], r["mobile"]) if r else (0, 0)
        except NaverError as e:
            res.errors.append(f"검색광고 API: {e}")
            log(f"  ! 검색광고 API 실패: {e}")

    cands = [c for c in pool.values() if relevant(c.keyword)]
    if any(c.volume is not None for c in cands):
        cands.sort(key=lambda c: (c.volume if c.volume is not None else -1, c.base), reverse=True)
    else:
        cands.sort(key=lambda c: c.base, reverse=True)
    res.candidates = cands
    res.used = [u for u in ("autocomplete", "combo", "ai", "searchad") if u in used]
    res.seconds = time.monotonic() - started
    return res


def generate_for_product(
    cfg: AppConfig,
    store: Store,
    product_id: str,
    client: NaverClient | None = None,
    ad_client: SearchAdClient | None = None,
    log: Callable[[str], None] = print,
) -> ExpandResult:
    product = cfg.product(product_id)
    settings = seed_settings(store, product_id)
    if product is None or not settings["seeds"]:
        return ExpandResult(product=product_id)
    if client is None:
        client = NaverClient(credentials=naver_credentials(), delay_seconds=cfg.settings.request_delay_seconds)
    if ad_client is None and searchad_credentials():
        ad_client = SearchAdClient(*searchad_credentials())
    res = expand(cfg, product, settings["seeds"], client, ad_client, log=log)
    if res.candidates:
        store.replace_auto_keywords(
            product_id,
            [
                {
                    "keyword": c.keyword, "seed": c.seed, "sources": c.sources, "pc": c.pc, "mobile": c.mobile,
                    "score": c.volume if c.volume is not None else c.base,
                }
                for c in res.candidates
            ],
            settings["max"],
        )
    settings.update(generated_at=iso(now_kst()), last_error="; ".join(res.errors[:3]) or None)
    store.kv_set(f"seeds:{product_id}", settings)
    return res


def due_products(cfg: AppConfig, store: Store, now=None) -> list[str]:
    """메인 키워드가 있는데 한 번도 생성 안 했거나 REFRESH_DAYS 가 지난 제품."""
    from .storage import from_iso

    now = now or now_kst()
    out = []
    for p in cfg.products:
        s = seed_settings(store, p.id)
        if not s["seeds"]:
            continue
        last = from_iso(s["generated_at"])
        if last is None or (now - last).days >= REFRESH_DAYS:
            out.append(p.id)
    return out
