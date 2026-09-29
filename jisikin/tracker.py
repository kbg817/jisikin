"""작업 결과: 답변완료·댓글완료 한 글에 내 답변/댓글이 아직 보이는지 하루 한 번 확인한다.

어떤 답변/댓글이 '내 것'인지는 [완료]를 누를 때 저장된 최종 글(초안 칸 내용)로 찾는다.
계정 이름이 바뀌어도 상관없고, 올리면서 조금 고친 정도는 비슷한 글로 인정한다.

- 지식iN: 질문 페이지를 열어 내 답변 문장이 페이지에 있는지 + 채택 여부
- 유튜브: 공개 댓글(인기순 100개) 중 내 댓글이 있는지 + 몇 위인지·좋아요 수.
          API 키로는 남에게 보이는 댓글만 나오므로, 없으면 삭제·스팸 처리로 숨겨진 것.
- 쓰레드: Meta 검수 전에는 남의 글 답글을 볼 수 없어 확인하지 않음
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable

from bs4 import BeautifulSoup

from .collector import make_client
from .config import AppConfig, youtube_key
from .naver import NaverClient, NaverError, _parse_answer_count
from .social import SocialError, YouTubeBudget, YouTubeClient
from .storage import Store, now_kst

# 확인 결과
VISIBLE = "visible"            # 보임
MISSING = "missing"            # 안 보임 (삭제·숨김 의심)
GONE = "gone"                  # 질문·영상 자체가 삭제됨
COMMENTS_OFF = "comments_off"  # 영상 댓글이 막힘
NO_TEXT = "no_text"            # 저장된 답변/댓글 내용이 없어 확인 불가
ERROR = "error"                # 이번에는 확인 실패 (다음에 다시)

_GONE_RE = re.compile(r"삭제되었거나\s*존재하지\s*않|존재하지\s*않는\s*(질문|게시물|페이지)|삭제된\s*(질문|게시물)|비공개\s*처리된")
_ADOPTED_RE = re.compile(r"질문자\s*채택|질문자가\s*채택|채택된\s*답변|지식인\s*채택|채택\s*답변")
_ANSWER_ITEM_SELECTORS = ["[class*='answer-content__item']", "._answer", "._answerComponent", "[class*='answerDetail']"]


@dataclass
class TrackSummary:
    items: int = 0
    checked: int = 0
    visible: int = 0
    missing: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def text(self) -> str:
        s = f"{self.checked}건 확인 — 보임 {self.visible} · 안 보임 {self.missing}"
        if self.skipped:
            s += f" · 건너뜀 {self.skipped}"
        if self.errors:
            s += f" · 오류 {len(self.errors)}"
        return s + f" ({self.seconds:.0f}초)"


# ---------------------------------------------------------------- 글 비교

def normalize(text: str) -> str:
    """띄어쓰기·문장부호·이모지를 빼고 비교 (올리면서 줄바꿈이 바뀌어도 같은 글로 보게)."""
    return re.sub(r"[^0-9a-z가-힣]", "", (text or "").lower())


def _pieces(text: str) -> list[str]:
    parts = [normalize(p) for p in re.split(r"[.!?。\n~…]+", text or "")]
    parts = [p for p in parts if len(p) >= 8]
    if not parts:
        whole = normalize(text)
        return [whole] if len(whole) >= 4 else []
    return parts


def text_found(mine: str, haystack: str, haystack_normalized: bool = False) -> bool:
    """내 글의 문장 절반 이상이 haystack 에 그대로 들어 있으면 True."""
    pieces = _pieces(mine)
    if not pieces:
        return False
    hay = haystack if haystack_normalized else normalize(haystack)
    hits = sum(1 for p in pieces if p in hay)
    return hits * 2 >= len(pieces)


def same_text(mine: str, other: str) -> bool:
    """댓글 하나와 비교: 문장 절반 이상이 같거나, 전체가 60% 이상 비슷하면 같은 글."""
    a, b = normalize(mine), normalize(other)
    if not a or not b:
        return False
    if text_found(mine, b, haystack_normalized=True):
        return True
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.6


def search_phrase(mine: str) -> str:
    """유튜브 댓글 검색어: 내 글에서 가장 긴 문장 앞부분 (띄어쓰기는 유지)."""
    parts = [p.strip() for p in re.split(r"[.!?。\n~…]+", mine or "") if p.strip()]
    best = max(parts, key=len) if parts else (mine or "").strip()
    return best[:40]


# ---------------------------------------------------------------- 지식iN

def check_kin_page(html: str, mine: str) -> tuple[str, bool | None]:
    """질문 페이지 HTML → (결과, 채택 여부)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    page = soup.get_text(" ", strip=True)
    if _GONE_RE.search(page) and not text_found(mine, page):
        return GONE, None
    if not text_found(mine, page):
        # 답변 칸도, 답변 수도 못 읽었다면 화면 구조가 바뀐 것일 수 있음 → '안 보임'으로 단정하지 않음
        if not any(_safe_select(soup, sel) for sel in _ANSWER_ITEM_SELECTORS) and _parse_answer_count(soup) is None:
            return ERROR, None
        return MISSING, None
    # 내 답변이 들어 있는 답변 칸에서 채택 표시 찾기
    for sel in _ANSWER_ITEM_SELECTORS:
        for el in _safe_select(soup, sel):
            text = el.get_text(" ", strip=True)
            if text_found(mine, text):
                return VISIBLE, bool(_ADOPTED_RE.search(text))
    return VISIBLE, None  # 답변 칸을 못 찾았지만 페이지에는 있음


def _safe_select(soup: BeautifulSoup, sel: str) -> list:
    try:
        return soup.select(sel)
    except Exception:
        return []


def check_kin(client: NaverClient, item: dict) -> dict:
    try:
        html = client._get_html(item["url"])
    except NaverError as e:
        if "404" in str(e):
            return {"state": GONE}
        raise
    state, adopted = check_kin_page(html, item["draft"])
    if state == ERROR:
        raise NaverError("질문 페이지에서 답변을 읽지 못했습니다 (화면 구조가 바뀌었을 수 있음)")
    return {"state": state, "adopted": adopted}


# ---------------------------------------------------------------- 유튜브

def check_youtube(client: YouTubeClient, item: dict) -> dict:
    vid = item["item_id"][3:]
    mine = item["draft"]
    try:
        comments = client.top_comments(vid)
        for i, cm in enumerate(comments, 1):
            if same_text(mine, cm["text"]):
                return {"state": VISIBLE, "rank": i, "likes": cm["likes"], "replies": cm["replies"]}
        if len(comments) >= 100 or not comments:
            # 인기 댓글 100개 밖일 수 있으니 내 글로 한 번 더 찾아본다
            for cm in client.top_comments(vid, search=search_phrase(mine)):
                if same_text(mine, cm["text"]):
                    return {"state": VISIBLE, "likes": cm["likes"], "replies": cm["replies"], "note": "인기 댓글 100위 밖"}
    except SocialError as e:
        if e.reason == "commentsDisabled":
            return {"state": COMMENTS_OFF}
        if e.reason in ("videoNotFound", "notFound"):
            return {"state": GONE}
        raise
    return {"state": MISSING}


# ---------------------------------------------------------------- 실행

def check_answers(
    cfg: AppConfig,
    store: Store,
    naver: NaverClient | None = None,
    youtube: YouTubeClient | None = None,
    log: Callable[[str], None] = print,
) -> TrackSummary:
    started = time.monotonic()
    summary = TrackSummary()
    run_id = store.start_run("track")
    if naver is None:
        naver = make_client(cfg, store)
    if youtube is None and youtube_key():
        youtube = YouTubeClient(youtube_key(), YouTubeBudget(store, cfg.settings.youtube_daily_units))
    try:
        items = [i for i in store.answered_items() if i["platform"] in ("kin", "youtube")]
        summary.items = len(items)
        last = store.latest_answer_checks()
        # 오래 확인 못 한 글부터 (할당량이 모자라면 나머지는 다음 날)
        items.sort(key=lambda i: (last.get(i["item_id"]) or {}).get("checked_at") or "")
        stop = {"kin": False, "youtube": youtube is None}
        for item in items:
            platform = item["platform"]
            if stop[platform]:
                summary.skipped += 1
                continue
            if not (item.get("draft") or "").strip():
                store.add_answer_check(item["item_id"], NO_TEXT)
                summary.skipped += 1
                continue
            try:
                res = check_kin(naver, item) if platform == "kin" else check_youtube(youtube, item)
            except (NaverError, SocialError) as e:
                name = "지식iN" if platform == "kin" else "유튜브"
                summary.errors.append(f"{name} '{item['title'][:30]}': {e}")
                log(f"  ! {name} 확인 실패: {e}")
                if getattr(e, "fatal", False):
                    stop[platform] = True  # 할당량 소진·차단 등 — 남은 글은 다음 확인 때
                continue
            store.add_answer_check(item["item_id"], **res)
            summary.checked += 1
            summary.visible += res["state"] == VISIBLE
            summary.missing += res["state"] == MISSING
        store.purge_answer_checks()
    finally:
        summary.seconds = time.monotonic() - started
        store.finish_run(
            run_id, queries=summary.items, fetched=summary.checked, new_total=summary.visible,
            new_relevant=summary.missing, errors=summary.errors[:50],
        )
    return summary
