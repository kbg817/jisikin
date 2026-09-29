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
    return add_product_in_order(text, "myeongun", ["sinui", "myeongun", "daksaren", "eumpa"], example_text)


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


MIGRATIONS: list[tuple[str, str, Callable[[str, str], str | None]]] = [
    ("2026-09-myeongun", "명운연구소 추가, 제품 순서 변경", _myeongun),
    ("2026-09-ai-sonnet", "AI 초안 모델 Sonnet 5.5 · 생각 깊이 low", _ai_sonnet),
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
