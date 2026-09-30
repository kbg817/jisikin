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
        "신의소리는 신점·타로만, 사주는 새 서비스 명연당으로, 치디핏 추가",
    ]
    assert ids(path) == ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit"]
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
    assert ids(path) == ["sinui", "myeongyeon", "myeongun", "daksaren", "chidifit", "shop"]  # 모르는 제품은 뒤로, 순서 유지
    assert load_config(path).ai.effort == "low"


def test_existing_product_with_same_name_is_not_duplicated(tmp_path):
    mine = """  - id: naming
    name: 명운연구소
    keywords: [작명]
"""
    path = tmp_path / "config.yaml"
    path.write_text(HEAD + DAK + mine + SINUI, encoding="utf-8")
    migrate_config(path, Store(":memory:"))
    assert ids(path) == ["sinui", "myeongyeon", "daksaren", "chidifit", "naming"]


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
    assert [p.id for p in state.cfg.products] == ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit"]
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
    new_sinui_kw = "    keywords: [신점, 타로]\n"
    old_kw = "    keywords: [" + ", ".join(_OLD_SINUI_KEYWORDS) + "]\n"
    # 예전 기본 신의소리 + 명연당·치디핏 없음 + 사이트 주소는 직접 넣은 상태
    old = example.replace(new_sinui_kw, old_kw, 1).replace("    require_keyword: true     # 지식iN 은 신점·타로 질문만 (사주는 명연당)\n", "", 1)
    old = old.replace('url: ""                   # 사이트 주소', 'url: "https://sinui.kr"   # 사이트 주소', 1)
    parts = split_products(old)
    head, blocks, tail = parts
    old = join_products(head, [b for b in blocks if b[0] not in ("myeongyeon", "chidifit")], tail)
    new = _sinui_split(old, example)
    cfg = parse_config(new)
    assert [p.id for p in cfg.products] == ["sinui", "myeongyeon", "myeongun", "daksaren", "eumpa", "chidifit"]
    s = cfg.product("sinui")
    assert s.keywords == ["신점", "타로"] and s.require_keyword and s.url == "https://sinui.kr"
    assert "재회 주파수" in s.social_queries()
    # 신의소리 키워드를 직접 고쳐 둔 경우: 신의소리는 그대로, 새 서비스만 추가
    custom = old.replace(old_kw, "    keywords: [신점, 타로, 내 키워드]\n", 1)
    cfg2 = parse_config(_sinui_split(custom, example))
    assert cfg2.product("sinui").keywords == ["신점", "타로", "내 키워드"] and cfg2.product("chidifit")
