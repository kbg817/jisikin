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


def _sinui_kin_guide(text: str, example_text: str) -> str | None:
    """신의소리에 지식iN 전용 답변 지침(kin_guide) 추가 — 예시 설정의 글을 answer_guide 바로 앞에, 이미 있으면 그대로."""
    m = re.search(r"^    # kin_guide:.*\n    kin_guide: \|\n(?:      .*\n|\n)*?(?=    \S)", example_text, re.M)
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
    ("2026-10-calc-tools", "명연당·명운연구소 AI 초안이 명연당 계산(만세력·이름 판정)을 씀", _calc_tools),
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
