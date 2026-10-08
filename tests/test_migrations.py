import re
import shutil

from jisikin.config import EXAMPLE_CONFIG_PATH, load_config, parse_config
from jisikin.matcher import Matcher
from jisikin.migrations import join_products, migrate_config, split_products
from jisikin.storage import Store

HEAD = """settings:
  interval_minutes: 5   # 직접 바꾼 값

products:
"""
SINUI = """  - id: sinui
    name: 신의소리
    keywords: [신점, 타로, 내가 넣은 키워드]
    exposure:
      keywords: [신점 잘보는곳]
"""
EUMPA = """  - id: eumpa
    name: 음파쑥쑥
    keywords: [키 성장]
"""
DAK = """  - id: daksaren
    name: 닥사렌 모각크림
    keywords: [건선]
    # 메모: 내가 단 주석
"""
OLD = HEAD + SINUI + "\n" + EUMPA + "\n" + DAK


def ids(path):
    return [p.id for p in load_config(path).products]


def test_adds_myeongun_and_reorders_keeping_user_edits(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(OLD, encoding="utf-8")
    store = Store(":memory:")
    logs = []
    assert migrate_config(path, store, log=logs.append) == [
        "명운연구소 추가, 제품 순서 변경", "제품별 유튜브·쓰레드 검색어(social) 추가",
        "신의소리는 신점·타로·연애·재회만, 사주는 새 서비스 명연당으로, 치디핏 추가, 제품 사이트 주소",
        "세이프맘 탄소매트 추가",
        "신의소리·명연당·명운연구소 지식iN 답변 500byte 이하",
        "신의소리 지식iN 답변 지침 추가",
        "신의소리·명연당 카페 검색어 추가, 카페 검색어당 글 100개",
    ]
    assert ids(path) == ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit", "safemom"]
    text = path.read_text(encoding="utf-8")
    assert "interval_minutes: 5   # 직접 바꾼 값" in text and "내가 넣은 키워드" in text and "# 메모: 내가 단 주석" in text
    cfg = load_config(path)
    assert cfg.settings.interval_minutes == 5
    assert cfg.product("myeongun").name == "명운연구소" and "작명" in cfg.product("myeongun").keywords
    assert (tmp_path / "config.backup-2026-09-myeongun.yaml").read_text(encoding="utf-8") == OLD
    assert "backup" in logs[0]

    # 한 번만: 나중에 사용자가 명운연구소를 지우면 되살리지 않음
    path.write_text(OLD, encoding="utf-8")
    assert migrate_config(path, store) == []
    assert ids(path) == ["sinui", "eumpa", "daksaren"]


def test_latest_example_is_left_alone(tmp_path):
    path = tmp_path / "config.yaml"
    shutil.copyfile(EXAMPLE_CONFIG_PATH, path)
    before = path.read_text(encoding="utf-8")
    assert migrate_config(path, Store(":memory:")) == []
    assert path.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("config.backup-*"))


def test_other_products_and_sections_are_kept(tmp_path):
    extra = """  - id: shop
    name: 우리 쇼핑몰
    keywords: [쇼핑]
"""
    text = HEAD + extra + "\n" + DAK + "\n" + SINUI + "\nai:\n  effort: low\n"
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    migrate_config(path, Store(":memory:"))
    assert ids(path) == ["sinui", "myeongyeon", "myeongun", "daksaren", "chidifit", "safemom", "shop"]  # 모르는 제품은 뒤로, 순서 유지
    assert load_config(path).ai.effort == "low"


def test_existing_product_with_same_name_is_not_duplicated(tmp_path):
    mine = """  - id: naming
    name: 명운연구소
    keywords: [작명]
"""
    path = tmp_path / "config.yaml"
    path.write_text(HEAD + DAK + mine + SINUI, encoding="utf-8")
    migrate_config(path, Store(":memory:"))
    assert ids(path) == ["sinui", "myeongyeon", "daksaren", "chidifit", "safemom", "naming"]


def test_unrecognized_layout_is_not_touched(tmp_path):
    text = 'products: [{id: sinui, name: 신의소리, keywords: [신점]}]\n'
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    store = Store(":memory:")
    logs = []
    assert migrate_config(path, store, log=logs.append) == []
    assert path.read_text(encoding="utf-8") == text
    assert store.kv_get("migration:2026-09-myeongun")  # 다시 시도하지 않음


def test_startup_applies_migration_and_default_seeds(tmp_path):
    from jisikin.keywords import seed_settings
    from jisikin.web import AppState

    path = tmp_path / "config.yaml"
    path.write_text(OLD, encoding="utf-8")
    state = AppState(path, tmp_path / "db.sqlite", data_dir=tmp_path)
    assert [p.id for p in state.cfg.products] == ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit", "safemom"]
    assert seed_settings(state.store, "myeongun")["seeds"] == ["작명", "개명", "아기 이름", "이름 풀이"]
    assert seed_settings(state.store, "daksaren")["seeds"] == ["건선", "모공각화증"]
    assert any("명운연구소" in line for line in state.logs)


def test_myeongun_matching():
    cfg = parse_config(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    m = Matcher(cfg.products)

    def best(title):
        b = m.best(m.classify(title))
        return (b.product_id, b.categories) if b else None

    assert best("아기 이름 좀 지어주세요") == ("myeongun", ["아기 작명"])
    assert best("개명하고 싶은데 법원 허가 오래 걸리나요") == ("myeongun", ["개명"])
    assert best("카페 가게 이름 추천 부탁") == ("myeongun", ["상호·브랜드 작명"])
    assert best("작명소 추천해주세요 인천")[0] == "myeongun"
    for title in ["강아지 이름 추천해주세요", "게임 닉네임 추천", "영어 이름 추천", "유튜브 채널명 추천", "파일 이름 바꾸는 법", "상호명 변경 방법"]:
        assert best(title) is None, title


def test_ai_defaults_move_to_sonnet_only_when_unchanged(tmp_path):
    old = "ai:\n  model: claude-opus-5-5\n  effort: medium              # low | medium | high\n\n" + HEAD + SINUI
    path = tmp_path / "config.yaml"
    path.write_text(old, encoding="utf-8")
    migrate_config(path, Store(":memory:"))
    cfg = load_config(path)
    assert (cfg.ai.model, cfg.ai.effort) == ("claude-sonnet-5-5", "low")
    assert "effort: low              # low | medium | high" in path.read_text(encoding="utf-8")

    custom = old.replace("claude-opus-5-5", "claude-opus-5").replace("effort: medium", "effort: high")
    path.write_text(custom, encoding="utf-8")
    migrate_config(path, Store(":memory:"))
    cfg = load_config(path)
    assert (cfg.ai.model, cfg.ai.effort) == ("claude-opus-5", "high")  # 직접 바꾼 값은 그대로


def test_migration_removes_product_bans(tmp_path):
    from jisikin.migrations import _no_product_bans

    new_example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    for word in ("굿을 해야", "불행해진다", "완치", "몇 cm"):
        assert word not in new_example
    assert "피부과 진료를 먼저 권합니다" in new_example and "성장클리닉 진료를 권합니다" in new_example
    # 예전 기본 가이드 문장만 빠지고, 직접 고친 문장은 그대로
    text = (
        "      화장품은 질병을 치료한다고 표현할 수 없으므로 \"치료\", \"완치\", \"낫는다\" 같은 표현은 쓰지 않고,\n"
        "      건선처럼 증상이 넓거나 심하면 피부과 진료를 먼저 권합니다.\n"
        "      우리가 직접 넣은 금지 문장입니다.\n"
    )
    out = _no_product_bans(text, new_example)
    assert "완치" not in out and "피부과 진료" in out and "직접 넣은 금지 문장" in out
    assert _no_product_bans("변경 없음\n", new_example) is None


def test_sinui_split_updates_default_sinui_and_adds_products(tmp_path):
    from jisikin.migrations import _OLD_SINUI_KEYWORDS, _sinui_split

    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    import re

    old_kw = "    keywords: [" + ", ".join(_OLD_SINUI_KEYWORDS) + "]\n"
    new_sinui_kw = re.search(r"^    keywords: \[신점, 타로,[^\]]*\]\n", example, re.M).group(0)
    # 예전 기본 신의소리 + 명연당·치디핏 없음 + 사이트 주소는 직접 넣은 상태
    old = example.replace(new_sinui_kw, old_kw, 1)
    old = re.sub(r"^    require_keyword: true .*신점.*\n", "", old, count=1, flags=re.M)
    old = old.replace('url: "https://voiceofgod.co.kr/"', 'url: "https://sinui.kr"', 1)
    old = old.replace('url: "https://myeongunlab.co.kr/"', 'url: ""', 1)  # 비어 있던 주소는 채워짐
    parts = split_products(old)
    head, blocks, tail = parts
    old = join_products(head, [b for b in blocks if b[0] not in ("myeongyeon", "chidifit")], tail)
    new = _sinui_split(old, example)
    cfg = parse_config(new)
    assert [p.id for p in cfg.products] == ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit", "safemom"]
    s = cfg.product("sinui")
    assert s.keywords[:3] == ["신점", "타로", "재회"] and s.require_keyword and s.url == "https://sinui.kr"
    assert "재회 주파수" in s.social_queries() and "짝사랑 주파수" in s.social_queries()
    assert cfg.product("myeongun").url == "https://myeongunlab.co.kr/"
    assert "한자" in cfg.product("myeongyeon").answer_guide
    # 신의소리 키워드를 직접 고쳐 둔 경우: 신의소리는 그대로, 새 서비스만 추가
    custom = old.replace(old_kw, "    keywords: [신점, 타로, 내 키워드]\n", 1)
    cfg2 = parse_config(_sinui_split(custom, example))
    assert cfg2.product("sinui").keywords == ["신점", "타로", "내 키워드"] and cfg2.product("chidifit")


def test_social_interval_6_to_8_only_when_default():
    from jisikin.migrations import _social_8h

    assert "social_interval_hours: 8   # x" in _social_8h("settings:\n  social_interval_hours: 6   # x\n", "")
    assert _social_8h("settings:\n  social_interval_hours: 12\n", "") is None


def test_no_disclosure_replaces_old_default_lines():
    from jisikin.migrations import _DISCLOSURE_LINES, _no_disclosure

    old = "ai:\n  common_guide: |\n" + _DISCLOSURE_LINES[0][0] + "  social_guide: |\n" + _DISCLOSURE_LINES[1][0]
    new = _no_disclosure(old, "")
    assert "공정위" not in new and "운영하는 사람인데요\" 같은" in new and new.count("링크(URL)는 넣지 않습니다") == 2
    assert _no_disclosure(new, "") is None
    parse_config(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))


def test_calc_tools_added_to_myeongyeon_and_myeongun(tmp_path):
    from jisikin.migrations import _calc_tools

    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    # 지금 서버 설정: calc_tools 가 없는 예시 설정
    old = example.replace("    calc_tools: [saju, name]\n", "", 1).replace("    calc_tools: [name, hanja, saju]\n", "", 1)
    assert all(not p.calc_tools for p in parse_config(old).products)
    new = _calc_tools(old, example)
    cfg = parse_config(new)
    assert cfg.product("myeongyeon").calc_tools == ["saju", "name"]
    assert cfg.product("myeongun").calc_tools == ["name", "hanja", "saju"]
    assert not cfg.product("sinui").calc_tools and not cfg.product("daksaren").calc_tools
    assert new == example  # 예시 설정과 같은 자리에 들어감
    assert _calc_tools(new, example) is None  # 이미 있으면 그대로
    # 직접 바꿔 둔 값은 그대로
    mine = new.replace("    calc_tools: [name, hanja, saju]\n", "    calc_tools: [name]\n", 1)
    assert _calc_tools(mine, example) is None

    path = tmp_path / "config.yaml"
    path.write_text(old, encoding="utf-8")
    store = Store(":memory:")
    from jisikin.migrations import MIGRATIONS

    for key, _, _ in MIGRATIONS:
        if key != "2026-10-calc-tools":
            store.kv_set(f"migration:{key}", "done")  # 서버에서 이미 한 업데이트
    assert migrate_config(path, store) == ["명연당·명운연구소 AI 초안이 명연당 계산(만세력·이름 판정)을 씀"]
    assert load_config(path).product("myeongun").calc_tools == ["name", "hanja", "saju"]


def test_max_bytes_added_once_to_three_products():
    from jisikin.migrations import _max_bytes

    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    old = example.replace("    max_bytes: 500            # 지식iN 답변 최대 길이 (한글 1자=2byte → 약 240자)\n", "")
    assert "max_bytes" not in old.split("products:")[1]
    new = _max_bytes(old, example)
    cfg = parse_config(new)
    assert [p.id for p in cfg.products if p.max_bytes == 500] == ["sinui", "myeongyeon", "myeongun"]
    assert new == example and _max_bytes(new, example) is None


def test_sinui_kin_guide_added_once():
    import re

    from jisikin.migrations import _sinui_kin_guide

    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    from jisikin.migrations import _example_kin_block

    m = _example_kin_block(example)
    old = example.replace(m.group(0), "")
    assert not parse_config(old).product("sinui").kin_guide
    new = _sinui_kin_guide(old, example)
    assert new == example and _sinui_kin_guide(new, example) is None


def test_sinui_kin_guide_v2_replaces_only_old_default():
    from jisikin.migrations import _KIN_GUIDE_V1, _sinui_kin_guide_v2

    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    from jisikin.migrations import _example_kin_block

    m = _example_kin_block(example)
    old = example.replace(m.group(0), _KIN_GUIDE_V1)
    assert "상투적인 추천·후기 문구" in parse_config(old).product("sinui").kin_guide
    new = _sinui_kin_guide_v2(old, example)
    assert new == example and _sinui_kin_guide_v2(new, example) is None
    edited = old.replace("1. 상투적인 추천·후기 문구를", "1. 직접 고친 문장:")
    assert _sinui_kin_guide_v2(edited, example) is None  # 직접 고친 지침은 그대로


def test_sinui_kin_tone_from_any_older_guide():
    from jisikin.migrations import (
        _KIN_GUIDE_V1, _KIN_GUIDE_V2, _SINUI_GUIDE_NEW, _SINUI_GUIDE_OLD,
        _KIN_BLOCK_V3, _example_kin_block, _sinui_kin_guide, _sinui_kin_guide_v2, _sinui_kin_guide_v3,
        _sinui_kin_guide_v4,
    )

    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    block = _example_kin_block(example).group(0)
    for older in ("", _KIN_GUIDE_V1, _KIN_GUIDE_V2, _KIN_BLOCK_V3):
        text = example.replace(block, older).replace(_SINUI_GUIDE_NEW, _SINUI_GUIDE_OLD)
        for fn in (_sinui_kin_guide, _sinui_kin_guide_v2, _sinui_kin_guide_v3, _sinui_kin_guide_v4):
            text = fn(text, example) or text
        assert text == example
    sinui = parse_config(example).product("sinui")
    assert "서비스 안내문처럼 쓰지 않는다" in sinui.kin_guide and "짚어볼 수" in sinui.kin_banned
    assert "상담받을 수 있다고 안내" not in sinui.answer_guide
    assert "호칭은 질문자가 쓴 말 그대로" in sinui.kin_guide and "그녀" in sinui.kin_banned


def test_myeongun_more_keywords_category_guide():
    """명운연구소 수집 늘리기: 빠진 검색어·패턴·출산택일만 더하고, 직접 고친 문장은 그대로."""
    from jisikin.matcher import Matcher
    from jisikin.migrations import _MYEONGUN_GUIDE_NEW, _MYEONGUN_GUIDE_OLD, _MYEONGUN_TAEIL, _myeongun_more

    example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    import re

    before = "    keywords: [작명, 작명소, 개명, 아기 이름, 신생아 이름, 이름 짓기, 이름 지어, 이름 풀이, 이름 추천, 성명학, 상호명 추천, 가게 이름 추천]\n"
    old = re.sub(r"^    keywords: \[작명, 작명소.*?\]\n", lambda m: before, example, count=1, flags=re.M | re.S)
    old = old.replace(_MYEONGUN_TAEIL, "").replace(_MYEONGUN_GUIDE_NEW, _MYEONGUN_GUIDE_OLD)
    assert "출산택일" not in parse_config(old).product("myeongun").keywords
    new = _myeongun_more(old, example)
    assert new == example and _myeongun_more(new, example) is None
    cfg = parse_config(new)
    p = cfg.product("myeongun")
    assert "출산택일" in [c.name for c in p.categories] and "출산택일" in p.search_queries()
    assert not any(q.startswith("re:") for q in p.search_queries())  # 정규식은 검색어로 안 보냄
    m = Matcher(cfg.products)
    for title in ["남자아이 이름 좀 지어주세요", "제왕절개 날짜 잡으려는데요", "여자이름 추천 부탁"]:
        assert Matcher.best(m.classify(title)) is not None and any(
            x.product_id == "myeongun" and x.relevant for x in m.classify(title)), title
