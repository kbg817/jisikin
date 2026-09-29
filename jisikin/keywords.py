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
from functools import lru_cache
from typing import Callable

from .config import EXAMPLE_CONFIG_PATH, AppConfig, ConfigError, Product, load_config, naver_credentials, searchad_credentials
from .drafter import DraftError, ai_status, ask_claude
from .matcher import Matcher
from .naver import NaverClient, NaverError
from .searchad import SearchAdClient, compact, hint_key
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
DEFAULT_MIN_VOLUME = 50  # 월간 검색수(PC+모바일)가 이보다 적으면 켜지 않음 (검색광고 API 로 검색수를 알 때만)
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
    min_volume = data.get("min_volume")
    return {
        "seeds": list(data.get("seeds") or []),
        "max": int(data.get("max") or DEFAULT_MAX),
        "min_volume": DEFAULT_MIN_VOLUME if min_volume is None else int(min_volume),
        "generated_at": data.get("generated_at"),
        "last_error": data.get("last_error"),
        "used": list(data.get("used") or []),  # 마지막 생성 때 실제로 쓴 출처
        "searchad_tried": bool(data.get("searchad_tried")),
    }


@lru_cache(maxsize=1)
def _example_products() -> dict[str, Product]:
    try:
        return {p.id: p for p in load_config(EXAMPLE_CONFIG_PATH).products}
    except (ConfigError, OSError):
        return {}


def default_seeds(product: Product) -> list[str]:
    """설정의 exposure.seeds. 예전에 복사한 config.yaml 에는 없으므로, 같은 제품이면 예시 설정 값을 쓴다."""
    if product.exposure.seeds:
        return list(product.exposure.seeds)
    ex = _example_products().get(product.id)
    return list(ex.exposure.seeds) if ex and ex.name == product.name else []


def ensure_default_seeds(cfg: AppConfig, store: Store) -> list[str]:
    """메인 키워드를 한 번도 저장한 적 없는 제품에 기본 메인 키워드를 넣는다. (화면에서 비우면 그대로 둠)"""
    added = []
    for p in cfg.products:
        seeds = default_seeds(p)
        if seeds and store.kv_get(f"seeds:{p.id}") is None:
            save_seed_settings(store, p.id, seeds, DEFAULT_MAX)
            added.append(p.name)
    return added


def save_seed_settings(store: Store, product_id: str, seeds: list[str], max_n: int, min_volume: int | None = None) -> dict:
    cur = seed_settings(store, product_id)
    clean = []
    for s in seeds:
        s = re.sub(r"\s+", " ", s).strip()
        if s and compact(s) not in {compact(c) for c in clean}:
            clean.append(s)
    cur.update(seeds=clean[:10], max=max(1, min(int(max_n), 60)))
    if min_volume is not None:
        cur["min_volume"] = max(0, int(min_volume))
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
        ad_errors: list[str] = []

        def ad_stats(hints: list[str]) -> list[dict] | None:
            try:
                rows = ad_client.keyword_stats(hints)
                used.add("searchad")
                return rows
            except NaverError as e:
                if str(e) not in ad_errors:
                    ad_errors.append(str(e))
                    log(f"  ! 검색광고 API 실패: {e}")
                if e.fatal:
                    raise
                return None

        try:
            for i in range(0, len(seeds), 5):
                rows = ad_stats(seeds[i : i + 5]) or []
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
                rows = ad_stats([c.keyword for c in batch])
                if rows is None:
                    continue  # 이 묶음만 실패 — 검색수 모름으로 둠
                stats = {hint_key(r["keyword"]): r for r in rows}
                for c in batch:
                    r = stats.get(hint_key(c.keyword))
                    c.pc, c.mobile = (r["pc"], r["mobile"]) if r else (0, 0)
        except NaverError:
            pass  # 인증 실패 등 — 더 부르지 않음 (오류는 위에서 기록)
        res.errors.extend(f"검색광고 API: {e}" for e in ad_errors[:2])

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
            settings["min_volume"],
        )
    settings.update(
        generated_at=iso(now_kst()),
        last_error="; ".join(res.errors[:3]) or None,
        used=res.used,
        searchad_tried=ad_client is not None,
    )
    store.kv_set(f"seeds:{product_id}", settings)
    return res


def due_products(cfg: AppConfig, store: Store, now=None) -> list[str]:
    """다시 만들어야 하는 제품: 메인 키워드가 있는데
    - 한 번도 생성 안 했거나 REFRESH_DAYS 가 지났거나
    - 검색광고 키가 생겼는데 마지막 생성 때는 검색광고 API 를 쓰지 않은 경우 (키 저장 전에 만든 목록)
    """
    from .storage import from_iso

    now = now or now_kst()
    has_ad = searchad_credentials() is not None
    out = []
    for p in cfg.products:
        s = seed_settings(store, p.id)
        if not s["seeds"]:
            continue
        last = from_iso(s["generated_at"])
        if last is None or (now - last).days >= REFRESH_DAYS or (has_ad and not s["searchad_tried"]):
            out.append(p.id)
    return out
