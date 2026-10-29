"""한자 검색어 — 인명용 한자의 '뜻 + 음'(예: 이슬 빛날 현)으로 네이버 통합검색 상위 지식iN 글을 찾는다.

아기 이름 한자를 고르는 사람들은 '이슬빛날 현'처럼 한자 뜻으로 검색하고, 통합검색에 뜨는 지식iN 글을 읽는다.
음(현)만 넣으면 명연당 계산 서버의 인명용 한자 사전으로 뜻을 붙여 검색어를 만든다 (사전은 이 저장소에 넣지 않는다).
검색어가 수백 개라 상위노출 확인 때마다 일부(settings.hanja_per_run)만 돌아가며 통합검색(모바일·PC)에서 확인한다.
"""
from __future__ import annotations

import re
from typing import Callable

SOURCES = ("mobile", "pc")  # 통합검색 화면만 (사람들이 한자 뜻을 치는 곳)
MAX_EUM_PER_REQUEST = 40    # 한 번에 펼칠 음 수 (명연당 계산 서버는 1분에 120회까지)
MAX_PHRASE_LENGTH = 30

_SYLLABLE = re.compile(r"^[가-힣]$")
_PHRASE = re.compile(r"^[가-힣 ]+$")


def parse_input(text: str) -> tuple[list[str], list[dict], list[str]]:
    """'현, 서' / '이슬 빛날 현' → (펼칠 음, 그대로 넣을 검색어 [{keyword, eum, hanja}], 알아듣지 못한 것)."""
    eums: list[str] = []
    phrases: list[dict] = []
    bad: list[str] = []
    for raw in re.split(r"[,\n]", text or ""):
        item = " ".join(raw.split())
        if not item:
            continue
        if _SYLLABLE.match(item):
            eums.append(item)
            continue
        head, _, last = item.rpartition(" ")
        if head and _SYLLABLE.match(last) and _PHRASE.match(item) and len(item) <= MAX_PHRASE_LENGTH:
            phrases.append({"keyword": item, "eum": last, "hanja": ""})
        else:
            bad.append(item)
    return list(dict.fromkeys(eums)), phrases, bad


def expand(eums: list[str], lookup: Callable[[str], list[dict]] | None = None) -> tuple[list[dict], list[str]]:
    """음마다 인명용 한자를 찾아 '뜻 음' 검색어로. 연결 문제(CalcUnavailable)는 그대로 올린다."""
    if lookup is None:
        from .calc import hanja_candidates as lookup
    rows: list[dict] = []
    errors: list[str] = []
    for eum in eums:
        try:
            found = lookup(eum)
        except ValueError as e:
            errors.append(f"'{eum}': {e}")
            continue
        if not found:
            errors.append(f"'{eum}': 이름에 쓰는 한자를 찾지 못했어요")
            continue
        rows.extend({"keyword": f"{h['meaning']} {eum}", "eum": eum, "hanja": h.get("hanja") or ""} for h in found)
    return rows, errors
