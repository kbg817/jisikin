"""이미 쓰고 있는 config.yaml 을 새 버전에 맞게 딱 한 번 고친다.

서버의 config.yaml 은 처음 배포할 때 config.example.yaml 을 복사한 것이라, 예시 파일에 새로 넣은
제품은 자동으로 들어가지 않는다. 여기서 사용자가 고친 내용은 건드리지 않고 필요한 부분만 덧붙인다.
- 제품 블록(`  - id: ...`)을 통째로 끼워 넣거나 순서만 바꾼다.
- 바꾸기 전 파일은 config.backup-<이름>.yaml 로 남긴다.
- 파일 모양이 예상과 다르면(직접 크게 고친 경우) 건드리지 않고 로그만 남긴다.
- 한 번 시도한 작업은 DB(kv)에 기록해 다시 하지 않는다. (사용자가 나중에 지운 제품을 되살리지 않게)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .config import EXAMPLE_CONFIG_PATH, ConfigError, parse_config
from .storage import Store, iso, now_kst

_BLOCK_RE = re.compile(r"^  - id: *['\"]?([A-Za-z0-9_-]+)['\"]? *(?:#.*)?$", re.M)
_TOP_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*\s*:", re.M)


def split_products(text: str) -> tuple[str, list[tuple[str, str]], str] | None:
    """(products 앞부분, [(id, 블록)], products 뒷부분). 모양이 예상과 다르면 None."""
    m = re.search(r"^products:[ \t]*(?:#.*)?\n", text, re.M)
    if not m:
        return None
    start = m.end()
    nxt = _TOP_KEY_RE.search(text, start)
    end = nxt.start() if nxt else len(text)
    section = text[start:end]
    heads = list(_BLOCK_RE.finditer(section))
    if not heads:
        return None
    blocks = []
    for i, h in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(section)
        blocks.append((h.group(1), section[h.start() : stop]))
    head = text[:start] + section[: heads[0].start()]
    try:
        ids = [p.id for p in parse_config(text).products]
    except ConfigError:
        return None
    if ids != [b[0] for b in blocks]:
        return None  # 제품이 다른 모양으로 적혀 있음 → 손대지 않음
    return head, blocks, text[end:]


def join_products(head: str, blocks: list[tuple[str, str]], tail: str) -> str:
    body = "\n".join(b.rstrip("\n") + "\n" for _, b in blocks)
    return head + body + ("\n" + tail if tail else "")


def add_product_in_order(text: str, product_id: str, order: list[str], example_text: str) -> str | None:
    """예시 설정의 product_id 블록이 없으면 넣고, order 에 있는 제품들을 그 순서로 맞춘다."""
    parts = split_products(text)
    ex = split_products(example_text)
    if parts is None or ex is None:
        return None
    head, blocks, tail = parts
    cfg = parse_config(text)
    example_block = dict(ex[1]).get(product_id)
    example_name = next((p.name for p in parse_config(example_text).products if p.id == product_id), None)
    ids = [b[0] for b in blocks]
    if product_id not in ids and example_block and example_name not in {p.name for p in cfg.products}:
        blocks.append((product_id, example_block))
    rank = {pid: i for i, pid in enumerate(order)}
    ordered = sorted(blocks, key=lambda b: (b[0] not in rank, rank.get(b[0], 0)))  # 목록에 없는 제품은 원래 순서대로 뒤에
    new = join_products(head, ordered, tail)
    return None if new == text else new


def _myeongun(text: str, example_text: str) -> str | None:
    return add_product_in_order(text, "myeongun", ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit"], example_text)


def _ai_sonnet(text: str, example_text: str) -> str | None:
    """AI 초안 기본값 변경: Opus 5.5 / medium → Sonnet 5.5 / low. 직접 다른 값으로 바꿔 둔 경우는 그대로 둔다."""
    m = re.search(r"^ai:[ \t]*(?:#.*)?\n", text, re.M)
    if not m:
        return None
    nxt = _TOP_KEY_RE.search(text, m.end())
    end = nxt.start() if nxt else len(text)
    section = text[m.end() : end]
    new = re.sub(r"^(  model:[ \t]*)(['\"]?)claude-opus-5-5\2(?=[ \t]|$)", r"\1claude-sonnet-5-5", section, count=1, flags=re.M)
    new = re.sub(r"^(  effort:[ \t]*)(['\"]?)medium\2(?=[ \t]|$)", r"\1low", new, count=1, flags=re.M)
    return None if new == section else text[: m.end()] + new + text[end:]


_SOCIAL_RE = re.compile(r"^    social:[ \t]*(?:#.*)?\n(?:      .*\n)*", re.M)


def _social(text: str, example_text: str) -> str | None:
    """제품마다 유튜브·쓰레드 검색어(social:)가 없으면 예시 설정의 것을 answer_guide 앞에 넣는다."""
    parts = split_products(text)
    ex = split_products(example_text)
    if parts is None or ex is None:
        return None
    head, blocks, tail = parts
    example_social = {pid: m.group(0) for pid, block in ex[1] if (m := _SOCIAL_RE.search(block))}
    out = []
    for pid, block in blocks:
        social = example_social.get(pid)
        if social and not _SOCIAL_RE.search(block):
            guide = re.search(r"^    answer_guide:", block, re.M)
            if guide:
                block = block[: guide.start()] + social + block[guide.start() :]
            else:
                block = block.rstrip("\n") + "\n" + social
        out.append((pid, block))
    new = join_products(head, out, tail)
    return None if new == text else new


# 제품별 금지 표현 문장 (예전 기본 가이드 그대로인 경우만 뺀다)
_PRODUCT_BANS = [
    ('      결과를 단정하거나 불안감을 조성하지 않습니다. ("반드시 ~된다", "굿을 해야 한다" 등 금지)\n',
     ''),
    ('      특정 이름이 "나쁘다", "불행해진다"처럼 불안을 조성하는 표현은 쓰지 않습니다.\n',
     ''),
    ('      화장품은 질병을 치료한다고 표현할 수 없으므로 "치료", "완치", "낫는다" 같은 표현은 쓰지 않고,\n      건선처럼 증상이 넓거나 심하면 피부과 진료를 먼저 권합니다.\n',
     '      건선처럼 증상이 넓거나 심하면 피부과 진료를 먼저 권합니다.\n'),
    ('      "키가 몇 cm 큰다" 같은 효능 단정은 하지 않고, 성조숙증 의심·성장호르몬 치료 등\n      의학적 판단이 필요한 경우 소아청소년과·성장클리닉 진료를 권합니다.\n',
     '      성조숙증 의심·성장호르몬 치료 등 의학적 판단이 필요한 경우 소아청소년과·성장클리닉 진료를 권합니다.\n'),
]


def _no_product_bans(text: str, example_text: str) -> str | None:
    """제품별 answer_guide 에서 금지 표현 문장을 뺀다. 직접 고친 문장은 그대로 둔다."""
    new = text
    for old, repl in _PRODUCT_BANS:
        new = new.replace(old, repl)
    return None if new == text else new


# 예전 기본 신의소리 키워드 — 이 그대로면 새 기본값(신점·타로만)으로 바꾼다. 직접 고쳤으면 건드리지 않음
_OLD_SINUI_KEYWORDS = ["신점", "타로", "사주", "운세", "궁합", "점집", "철학관", "역술", "무당", "신내림", "팔자", "신년 운세", "토정비결"]
_GUIDE_RE = re.compile(r"^    answer_guide:.*\n(?:(?:      .*)?\n)*", re.M)
_URL_RE = re.compile(r"^    url:.*$", re.M)


def _sinui_split(text: str, example_text: str) -> str | None:
    """신의소리는 신점·타로·연애·재회만, 사주는 새 서비스 명연당으로. 치디핏 추가. 비어 있는 사이트 주소 채우기.
    신의소리 블록은 예전 기본값 그대로일 때만 새 기본값으로 바꾸고, 답변 가이드·직접 넣은 사이트 주소는 서버 것을 유지한다."""
    parts, ex = split_products(text), split_products(example_text)
    if parts is None or ex is None:
        return None
    head, blocks, tail = parts
    cfg = parse_config(text)
    sinui = next((p for p in cfg.products if p.id == "sinui"), None)
    new_sinui = dict(ex[1]).get("sinui")
    if sinui and new_sinui and sinui.keywords == _OLD_SINUI_KEYWORDS:
        old_block = dict(blocks)["sinui"]
        block = new_sinui
        url, guide = _URL_RE.search(old_block), _GUIDE_RE.search(old_block)
        if url:
            block = _URL_RE.sub(lambda _: url.group(0), block, count=1)
        if guide:
            block = _GUIDE_RE.sub(lambda _: guide.group(0), block, count=1)
        blocks = [(pid, block if pid == "sinui" else b) for pid, b in blocks]
    # 사이트 주소가 비어 있는 제품은 예시 설정의 주소로 채운다
    example_urls = {pid: m.group(0) for pid, b in ex[1] if (m := _URL_RE.search(b))}
    blocks = [
        (pid, _URL_RE.sub(lambda _: example_urls[pid], b, count=1)
         if pid in example_urls and re.search(r'^    url:\s*(""|\'\')?\s*(#.*)?$', b, re.M) else b)
        for pid, b in blocks
    ]
    text2 = join_products(head, blocks, tail)
    order = ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit"]
    for pid in ("myeongyeon", "chidifit"):
        text2 = add_product_in_order(text2, pid, order, example_text) or text2
    return None if text2 == text else text2


def _sinui_seeds(store: Store) -> None:
    """[검색어 관리] 신의소리 메인 키워드가 예전 기본값이면 사주·재회운을 뺀다 (사주는 명연당으로)."""
    cur = store.kv_get("seeds:sinui")
    if isinstance(cur, dict) and cur.get("seeds") == ["신점", "타로", "사주", "재회운"]:
        store.kv_set("seeds:sinui", {**cur, "seeds": ["신점", "타로"]})


def _social_8h(text: str, example_text: str) -> str | None:
    """유튜브 검색어가 늘어 찾는 간격 6시간 → 8시간 (할당량 여유). 직접 다른 값으로 바꿔 둔 경우는 그대로."""
    new = re.sub(r"^(  social_interval_hours:[ \t]*)6(?=[ \t]|$)", r"\g<1>8", text, count=1, flags=re.M)
    return None if new == text else new


def _safemom(text: str, example_text: str) -> str | None:
    order = ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit", "safemom"]
    return add_product_in_order(text, "safemom", order, example_text)


_DISCLOSURE_LINES = [
    ('    - 운영자(판매자)로서 소개할 때는 그 관계를 짧게 밝힙니다.\n      (예: "참고로 저는 ○○를 운영하고 있어요.") — 공정위 추천·보증 심사지침\n',
     '    - "참고로 저는 ○○를 운영하고 있어요" 같은 운영자·판매자 소개 문장과 링크(URL)는 넣지 않습니다.\n'),
    ('    - 운영자(판매자)로서 소개할 때는 관계를 밝힙니다. (예: "○○ 운영하는 사람인데요,")\n',
     '    - "○○ 운영하는 사람인데요" 같은 운영자·판매자 소개 문장과 링크(URL)는 넣지 않습니다.\n'),
]


def _no_disclosure(text: str, example_text: str) -> str | None:
    """AI 초안 공통 원칙에서 '운영자라고 밝히기' 문장을 빼고 '운영자 소개·링크 넣지 않기'로 바꾼다 (예전 기본 문장일 때만)."""
    new = text
    for old, repl in _DISCLOSURE_LINES:
        new = new.replace(old, repl)
    return None if new == text else new


_VIEWS_LINES = [('      keywords: [신점 잘보는곳, 타로 잘보는곳, 재회 타로, 전화 신점 추천]\n      regions: [서울, 인천, 부산, 대구]\n      region_terms: [신점, 점집]\n', '      keywords: [신점 잘보는곳, 타로 잘보는곳, 재회 타로, 전화 신점 추천, 사주나루]   # 사주나루: 경쟁사 (조회수 높은 글이 많음)\n      regions: [서울, 강남, 홍대, 인천, 부산, 대구, 대전, 광주, 울산, 수원, 성남, 고양, 용인, 부천, 창원, 청주]\n      region_terms: [신점, 타로]\n'), ('      regions: [서울, 인천, 부산, 대구]\n      region_terms: [사주, 철학관]\n', '      regions: [서울, 강남, 홍대, 인천, 부산, 대구, 대전, 광주, 울산, 수원, 성남, 고양, 용인, 부천, 창원, 청주]\n      region_terms: [사주, 철학관]\n')]
_SOURCES_RE = re.compile(r"^  exposure_sources:[ \t]*\[pc, mobile, kin\][^\n]*\n", re.M)


def _exposure_views(text: str, example_text: str) -> str | None:
    """상위노출 확인에 '지식iN 조회수순' 추가, 신의소리·명연당 지역 확대(+ 지역×타로), 경쟁사 사주나루 검색어.
    예전 기본값 그대로인 줄만 바꾼다."""
    new = _SOURCES_RE.sub(
        "  exposure_sources: [pc, mobile, kin, views]  # pc: 통합검색(PC), mobile: 통합검색(모바일), kin: 지식iN탭 정확도순, views: 지식iN 조회수순\n"
        "  exposure_views_top_n: 10             # 지식iN 조회수순은 검색어마다 상위 몇 개까지 볼지 (예전 글이라도 많이 읽힌 글)\n",
        text, count=1,
    )
    for old, repl in _VIEWS_LINES:
        new = new.replace(old, repl, 1)
    return None if new == text else new


def _add_calc_tools(block: str, line: str) -> str:
    """calc_tools 가 없으면 name 줄 바로 아래에 넣는다 (블록 끝은 answer_guide 글 안이라 피한다)."""
    if re.search(r"^    calc_tools:", block, re.M):
        return block
    return re.sub(r"^(    name:.*\n)", lambda m: m.group(1) + line + "\n", block, count=1, flags=re.M)


_CALC_TOOLS = {"myeongyeon": "    calc_tools: [saju, name]", "myeongun": "    calc_tools: [name, hanja, saju]"}


def _calc_tools(text: str, example_text: str) -> str | None:
    """명연당(사주)·명운연구소(작명) AI 초안이 명연당 계산(만세력·이름 판정)을 쓰게 한다. 이미 calc_tools 가 있으면 그대로."""
    parts = split_products(text)
    if parts is None:
        return None
    head, blocks, tail = parts
    blocks = [(pid, _add_calc_tools(b, _CALC_TOOLS[pid]) if pid in _CALC_TOOLS else b) for pid, b in blocks]
    new = join_products(head, blocks, tail)
    return None if new == text else new


_MAX_BYTES_LINE = "    max_bytes: 500            # 지식iN 답변 최대 길이 (한글 1자=2byte → 약 240자)\n"


def _max_bytes(text: str, example_text: str) -> str | None:
    """신의소리·명연당·명운연구소 지식iN 답변을 500byte 이하로 (max_bytes 가 없을 때만 min_score 다음 줄에)."""
    parts = split_products(text)
    if parts is None:
        return None
    head, blocks, tail = parts
    out = []
    for pid, block in blocks:
        if pid in ("sinui", "myeongyeon", "myeongun") and not re.search(r"^    max_bytes:", block, re.M):
            m = re.search(r"^    min_score:.*\n", block, re.M) or re.search(r"^    name:.*\n", block, re.M)
            if m:
                block = block[: m.end()] + _MAX_BYTES_LINE + block[m.end() :]
        out.append((pid, block))
    new = join_products(head, out, tail)
    return None if new == text else new


_CAFE_BLOCK = '    cafe:\n      # [카페] 탭: 네이버 카페에 올라온 작명·개명 글 (지식iN 과 같은 네이버 API 키 사용)\n      keywords: [작명소 추천, 아기 작명, 신생아 작명, 작명 문의, 이름 짓기, 개명 후기, 개명 신청, 개명 작명소, 사주 작명]\n'


def _cafe(text: str, example_text: str) -> str | None:
    """명운연구소에 네이버 카페 검색어(cafe:) 추가 — social: 바로 뒤에, 이미 있으면 그대로."""
    parts = split_products(text)
    if parts is None:
        return None
    head, blocks, tail = parts
    out = []
    for pid, block in blocks:
        if pid == "myeongun" and not re.search(r"^    cafe:", block, re.M):
            m = _SOCIAL_RE.search(block)
            if m:
                block = block[: m.end()] + _CAFE_BLOCK + block[m.end() :]
            else:
                block = block.rstrip("\n") + "\n" + _CAFE_BLOCK
        out.append((pid, block))
    new = join_products(head, out, tail)
    return None if new == text else new


def _example_kin_block(example_text: str) -> re.Match | None:
    """예시 설정의 신의소리 지식iN 지침 묶음 (# kin_guide 설명 ~ kin_guide 글 ~ kin_banned 줄까지)."""
    return re.search(
        r"^    # kin_guide:.*\n    kin_guide: \|\n(?:      .*\n|\n)*?(?:    # kin_banned:.*\n    kin_banned:.*\n(?:      +\S.*\n)*)?(?=    \S)",
        example_text, re.M,
    )


def _sinui_kin_guide(text: str, example_text: str) -> str | None:
    """신의소리에 지식iN 전용 답변 지침(kin_guide) 추가 — 예시 설정의 글을 answer_guide 바로 앞에, 이미 있으면 그대로."""
    m = _example_kin_block(example_text)
    parts = split_products(text)
    if m is None or parts is None:
        return None
    head, blocks, tail = parts
    out = []
    for pid, block in blocks:
        if pid == "sinui" and not re.search(r"^    kin_guide:", block, re.M):
            g = re.search(r"^    answer_guide:", block, re.M)
            if g:
                block = block[: g.start()] + m.group(0) + block[g.start() :]
            else:
                block = block.rstrip("\n") + "\n" + m.group(0)
        out.append((pid, block))
    new = join_products(head, out, tail)
    return None if new == text else new


_KIN_GUIDE_V1 = '    # kin_guide: 지식iN 답변에만 쓰는 지침 (유튜브·카페 댓글에는 안 씀). answer_guide·답변 예시보다 우선합니다.\n    kin_guide: |\n      1. 상투적인 추천·후기 문구를 쓰지 않는다.\n         어디에나 붙일 수 있는 추천 문장, 후기형 문장은 질문 내용과 직접 연결되지 않으므로 쓰지 않는다.\n         금지 예시: "궁금하시면 00쌤 한번 보세요." / "저도 비슷할 때 봤는데 마음이 풀렸어요." / "저도 비슷한 상황이었는데 좋아졌어요."\n      2. 단어만 바꾼 비슷한 문장도 같은 문장(중복)으로 본다. 표현이 달라도 의미와 역할이 같으면 같은 문장이다.\n         예) "궁금하시면 00쌤 한번 보세요." / "답답하시면 00쌤께 한번 물어보세요." / "속마음이 궁금하면 00쌤 상담도 괜찮아요."\n             → 모두 \'상담사 추천\'이라는 같은 역할이므로 중복이다.\n         예) "저도 비슷할 때 상담받았어요." / "저도 힘들 때 봤는데 도움이 됐어요." / "저는 상담 후 마음이 정리됐어요."\n             → 모두 같은 후기형 문장이다.\n         여러 답변에서 이런 구조가 반복되면 빼거나 완전히 다른 방식으로 바꾼다.\n      3. 최종 검수: 답변을 쓴 뒤 아래 3가지를 스스로 확인하고, 하나라도 해당되면 고친 뒤 답변한다.\n         - "추천", "상담받아보세요", "저도 비슷했어요" 같은 상투 표현이 있는가?\n         - 마지막 문장이 홍보로 끝나는가?\n         - 질문자가 실제로 궁금해한 내용에 공감 없이 1차적인 답만 했는가?\n      신의소리를 언급하더라도 질문 내용과 직접 이어지는 맥락에서만 하고, 마지막 문장은 질문자에게 건네는 말로 끝낸다.\n'


def _sinui_kin_guide_v2(text: str, example_text: str) -> str | None:
    """신의소리 지식iN 지침을 새 지침으로 교체 — 예전 기본 지침 그대로일 때만 (직접 고친 글은 그대로)."""
    m = _example_kin_block(example_text)
    if m is None or _KIN_GUIDE_V1 not in text:
        return None
    return text.replace(_KIN_GUIDE_V1, m.group(0), 1)


_KIN_GUIDE_V2 = '    # kin_guide: 지식iN 답변에만 쓰는 지침 (유튜브·카페 댓글에는 안 씀). answer_guide·답변 예시보다 우선합니다.\n    kin_guide: |\n      1. 추천·경험 공유의 취지는 유지하되, 상투적인 표현을 반복하지 않는다.\n         상담을 권하거나 실제 상담 경험을 나누는 내용은 가능하다. 다만 아래 문장을 정형화된 마무리로 사용하지 않는다.\n         금지 표현: "궁금하시면 00쌤 한번 보세요." / "저도 비슷할 때 봤는데 마음이 풀렸어요." / "저도 비슷한 상황이었는데 좋아졌어요."\n         단순히 단어만 바꾸지 말고, 질문자가 겪는 상황과 고민에 맞춰 문장 전체를 새로 작성한다.\n         무엇 때문에 상담을 떠올렸는지, 어떤 고민을 이야기해볼 수 있는지가 앞의 답변과 자연스럽게 이어져야 한다.\n         말투는 지인에게 이야기하듯 편하게 쓰며, 추상적이거나 광고 같은 표현은 피한다.\n         경험담은 실제로 제공된 경험에 한해서 사용한다.\n      2. AI 최종 검수 기준: 답변 작성 후 아래 3가지를 확인한다.\n         - 금지 표현을 그대로 쓰거나 단어만 바꿔 사용했는가?\n         - 질문자의 구체적인 상황에 공감하고, 실제로 물어본 내용에 답했는가?\n         - 추천·경험 공유 문장이 앞의 내용과 연결되지 않고 갑자기 붙었는가?\n         금지 표현이나 뜬금없는 추천이 있거나, 질문에 대한 공감·답변이 부족하면 수정한 뒤 답변한다.\n'
_SINUI_GUIDE_OLD = '    answer_guide: |\n      신의소리는 신점·타로·사주 상담 사이트입니다.\n      질문자의 고민에 먼저 공감하고, 질문에 정보(생년월일, 상황 등)가 있으면 일반적인 해석 방향을\n      짧게 제시한 뒤, 더 자세한 상담이 필요하면 신의소리에서 상담받을 수 있다고 안내합니다.\n'
_SINUI_GUIDE_NEW = '    answer_guide: |\n      신의소리는 신점·타로·사주 상담 사이트입니다.\n      질문자의 고민에 먼저 반응하고, 질문에 정보(생년월일, 상황 등)가 있으면 자기 생각이나 조언을 짧게 말한 뒤,\n      신의소리는 지인에게 말하듯 짧고 자연스럽게 추천합니다.\n'


def _sinui_kin_guide_v3(text: str, example_text: str) -> str | None:
    """신의소리 지식iN 말투 지침으로 교체 + 금지 표현(kin_banned), 제품 설명의 '상담받을 수 있다고 안내' 문장 정리.
    예전 기본 글 그대로일 때만 바꾼다 (직접 고친 글은 그대로)."""
    m = _example_kin_block(example_text)
    if m is None:
        return None
    new = text.replace(_KIN_GUIDE_V2, m.group(0), 1).replace(_SINUI_GUIDE_OLD, _SINUI_GUIDE_NEW, 1)
    return None if new == text else new


_KIN_BLOCK_V3 = '    # kin_guide: 지식iN 답변에만 쓰는 지침 (유튜브·카페 댓글에는 안 씀). 공통 원칙·answer_guide·답변 예시보다 우선합니다.\n    kin_guide: |\n      1. 서비스 안내문처럼 쓰지 않는다.\n         "짚어볼 수 있어요", "살펴볼 수 있어요", "마음의 방향을 확인해 보세요", "관계의 흐름을 알아보세요" 같은 표현은 사용하지 않는다.\n      2. 지인에게 댓글을 달듯 편한 구어체로 쓴다.\n         설명하거나 상담으로 유도하려고 길게 돌려 말하지 않는다.\n         "신의소리 00쌤 잘보세요", "저는 00쌤 추천요!"처럼 짧고 직접적인 추천도 가능하다.\n      3. 질문에 먼저 반응하고 추천을 이어간다.\n         질문 속 구체적인 상황에 대해 자신의 생각이나 조언을 먼저 말한다.\n         추천을 넣을 명분을 만들려고 질문에 없는 고민을 추가하지 않는다.\n      4. 추천 문장을 매번 같은 형식으로 끝내지 않는다.\n         "궁금하시면", "계속 마음에 걸리면", "저도 비슷한 상황이었는데"를 습관적으로 붙이지 않는다.\n         문장의 길이와 위치는 질문에 맞게 정한다.\n    # kin_banned: 지식iN 초안에 들어가면 AI 가 그 문장을 다시 고쳐 씁니다 (신의소리가 나온 문장의 "~볼 수 있어요" 안내 말투도 함께 잡음)\n    kin_banned: [짚어볼 수, 살펴볼 수, 마음의 방향, 관계의 흐름, 궁금하시면, 계속 마음에 걸리면, 마음에 걸리면, 저도 비슷한 상황, 저도 비슷할 때]\n'


def _sinui_kin_guide_v4(text: str, example_text: str) -> str | None:
    """신의소리 지식iN 지침에 호칭·반복 구조·어색한 표현 규칙 추가 (예전 기본 글 그대로일 때만)."""
    m = _example_kin_block(example_text)
    if m is None or _KIN_BLOCK_V3 not in text:
        return None
    return text.replace(_KIN_BLOCK_V3, m.group(0), 1)


_CAFE_MORE = {
    "sinui": '    cafe:\n      # [카페] 탭: 네이버 카페에 올라온 신점·타로·연애 고민 글\n      keywords: [신점, 타로, 재회, 점집, 무당, 신당, 연애, 속마음, 궁합, 부적, 이별, 운세]\n',
    "myeongyeon": '    cafe:\n      # [카페] 탭: 네이버 카페에 올라온 사주·철학관 글 (OO살: 자주 찾는 살 이름)\n      keywords: [사주, 철학원, 철학관, 삼재, 일주, 귀인, 망신살, 도화살, 역마살, 백호살, 원진살, 홍염살, 괴강살, 화개살]\n',
}


def _cafe_more(text: str, example_text: str) -> str | None:
    """신의소리·명연당에 카페 검색어(cafe:) 추가 (social: 바로 뒤에, 이미 있으면 그대로) + 카페 검색어당 글 50→100."""
    parts = split_products(text)
    if parts is None:
        return None
    head, blocks, tail = parts
    out = []
    for pid, block in blocks:
        if pid in _CAFE_MORE and not re.search(r"^    cafe:", block, re.M):
            m = _SOCIAL_RE.search(block)
            if m:
                block = block[: m.end()] + _CAFE_MORE[pid] + block[m.end() :]
            else:
                block = block.rstrip("\n") + "\n" + _CAFE_MORE[pid]
        out.append((pid, block))
    new = join_products(head, out, tail)
    new = re.sub(r"^  cafe_results: 50 ([^\n]*)$", r"  cafe_results: 100\1", new, count=1, flags=re.M)
    return None if new == text else new


_MYEONGUN_NEW_KEYWORDS = [
    "신생아 작명", "이름 작명", "인터넷 작명", "출산택일", "제왕 날짜", "남자 이름", "여자 이름",
    '"re:이름.{0,8}(지어|짓|추천|봐|괜찮|어떤가|어때|좋은지|골라)"',
    '"re:(아기|아이|아들|딸|남아|여아|신생아|쌍둥이|첫째|둘째|셋째|손주|손자|손녀)\\\\s*이름"',
]
_MYEONGUN_TAEIL = (
    "      - name: 출산택일\n"
    "        keywords: [출산택일, 출산 택일, 제왕절개 날짜, 제왕절개 택일, 제왕 날짜, 수술 날짜, 출산 날짜, 택일]\n"
    "        search: [제왕절개 날짜]\n"
)
_MYEONGUN_GUIDE_OLD = "      개명 질문이면 법원 개명 허가 신청 절차를 간단히 안내합니다.\n"
_MYEONGUN_GUIDE_NEW = _MYEONGUN_GUIDE_OLD + "      출산택일(제왕절개 날짜) 질문이면 아기 사주가 좋게 나오는 날짜를 고르는 기준을 짧게 설명합니다.\n"


def _myeongun_more(text: str, example_text: str) -> str | None:
    """명운연구소 지식iN 수집 늘리기: 검색어(신생아 작명·출산택일·남자/여자 이름 등)와 자연스러운 문장 패턴 추가,
    출산택일 카테고리, 답변 가이드 한 줄. 이미 있는 것은 그대로 두고 빠진 것만 더한다."""
    parts = split_products(text)
    if parts is None:
        return None
    head, blocks, tail = parts
    out = []
    for pid, block in blocks:
        if pid == "myeongun":
            m = re.search(r"^    keywords: \[(.*?)\]\n", block, re.M | re.S)
            if m:
                raw = m.group(1)
                have = {k.strip().strip('"').strip("'").replace(" ", "") for k in re.split(r",\s*", raw.replace("\n", " "))}
                # 정규식(re:)은 안에 쉼표가 있어 통째로 찾는다
                add = [k for k in _MYEONGUN_NEW_KEYWORDS
                       if (k not in raw if k.startswith('"re:') else k.replace(" ", "") not in have)]
                if add:
                    new_list = raw.rstrip() + ",\n               " + ", ".join(add)
                    block = block[: m.start(1)] + new_list + block[m.end(1) :]
            if not re.search(r"^      - name: 출산택일\s*$", block, re.M):
                w = re.search(r"^    watch_urls:", block, re.M) or re.search(r"^    exposure:", block, re.M)
                if w and re.search(r"^    categories:", block, re.M):
                    block = block[: w.start()] + _MYEONGUN_TAEIL + block[w.start() :]
            if _MYEONGUN_GUIDE_OLD in block and _MYEONGUN_GUIDE_NEW not in block:
                block = block.replace(_MYEONGUN_GUIDE_OLD, _MYEONGUN_GUIDE_NEW, 1)
        out.append((pid, block))
    new = join_products(head, out, tail)
    return None if new == text else new


# 설정 파일과 함께 DB 에 저장된 값도 한 번 고친다 (키: 설정 업데이트 키)
STORE_MIGRATIONS: dict[str, Callable[[Store], None]] = {"2026-10-sinui-split": _sinui_seeds}


MIGRATIONS: list[tuple[str, str, Callable[[str, str], str | None]]] = [
    ("2026-09-myeongun", "명운연구소 추가, 제품 순서 변경", _myeongun),
    ("2026-09-ai-sonnet", "AI 초안 모델 Sonnet 5.5 · 생각 깊이 low", _ai_sonnet),
    ("2026-10-social", "제품별 유튜브·쓰레드 검색어(social) 추가", _social),
    ("2026-10-no-bans", "제품별 답변 가이드에서 금지 표현 문장 삭제", _no_product_bans),
    ("2026-10-sinui-split", "신의소리는 신점·타로·연애·재회만, 사주는 새 서비스 명연당으로, 치디핏 추가, 제품 사이트 주소", _sinui_split),
    ("2026-10-social-8h", "유튜브·쓰레드 찾는 간격 6시간 → 8시간", _social_8h),
    ("2026-10-safemom", "세이프맘 탄소매트 추가", _safemom),
    ("2026-10-no-disclosure", "AI 초안에서 운영자 소개 문장·링크 빼기", _no_disclosure),
    ("2026-10-exposure-views", "상위노출에 지식iN 조회수순 추가, 지역 확대, 경쟁사 사주나루 검색어", _exposure_views),
    ("2026-10-max-bytes", "신의소리·명연당·명운연구소 지식iN 답변 500byte 이하", _max_bytes),
    ("2026-10-cafe", "명운연구소 네이버 카페 글 검색어 추가", _cafe),
    ("2026-10-sinui-kin-guide", "신의소리 지식iN 답변 지침 추가", _sinui_kin_guide),
    ("2026-10-sinui-kin-guide-v2", "신의소리 지식iN 답변 지침 교체", _sinui_kin_guide_v2),
    ("2026-10-sinui-kin-tone", "신의소리 지식iN 말투 지침·금지 표현", _sinui_kin_guide_v3),
    ("2026-10-sinui-kin-tone-2", "신의소리 지식iN 지침 추가 (호칭·어색한 표현)", _sinui_kin_guide_v4),
    ("2026-10-cafe-more", "신의소리·명연당 카페 검색어 추가, 카페 검색어당 글 100개", _cafe_more),
    ("2026-10-calc-tools", "명연당·명운연구소 AI 초안이 명연당 계산(만세력·이름 판정)을 씀", _calc_tools),
    ("2026-10-myeongun-more", "명운연구소 지식iN 검색어·문장 패턴·출산택일 추가", _myeongun_more),
]


def migrate_config(path: Path, store: Store, log: Callable[[str], None] = print, example_path: Path = EXAMPLE_CONFIG_PATH) -> list[str]:
    """아직 안 한 설정 업데이트를 적용하고, 적용한 항목의 설명을 돌려준다."""
    path = Path(path)
    if not path.exists():
        return []  # 새로 설치 — 최신 예시 파일이 그대로 복사된다
    example_text = Path(example_path).read_text(encoding="utf-8")
    done = []
    for key, label, fn in MIGRATIONS:
        mark = f"migration:{key}"
        if store.kv_get(mark):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            new = fn(text, example_text)
            if new is not None:
                parse_config(new)  # 결과가 올바른 설정일 때만 저장
                if key in STORE_MIGRATIONS:
                    STORE_MIGRATIONS[key](store)
                path.with_name(f"config.backup-{key}.yaml").write_text(text, encoding="utf-8")
                tmp = path.with_suffix(".yaml.tmp")
                tmp.write_text(new, encoding="utf-8")
                tmp.replace(path)
                done.append(label)
                log(f"설정 자동 업데이트: {label} (이전 파일은 config.backup-{key}.yaml)")
        except OSError as e:
            log(f"설정 자동 업데이트 실패 ({label}): {e}")
            continue  # 파일을 못 읽거나 못 쓴 경우는 다음 시작 때 다시 시도
        except (ConfigError, ValueError) as e:
            log(f"설정 자동 업데이트 건너뜀 ({label}): {e}")
        store.kv_set(mark, iso(now_kst()))
    return done
