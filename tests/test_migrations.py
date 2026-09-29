import shutil

from jisikin.config import EXAMPLE_CONFIG_PATH, load_config, parse_config
from jisikin.matcher import Matcher
from jisikin.migrations import migrate_config
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
    assert migrate_config(path, store, log=logs.append) == ["명운연구소 추가, 제품 순서 변경"]
    assert ids(path) == ["sinui", "myeongun", "daksaren", "eumpa"]
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
    assert ids(path) == ["sinui", "myeongun", "daksaren", "shop"]  # 모르는 제품은 뒤로, 순서 유지
    assert load_config(path).ai.effort == "low"


def test_existing_product_with_same_name_is_not_duplicated(tmp_path):
    mine = """  - id: naming
    name: 명운연구소
    keywords: [작명]
"""
    path = tmp_path / "config.yaml"
    path.write_text(HEAD + DAK + mine + SINUI, encoding="utf-8")
    migrate_config(path, Store(":memory:"))
    assert ids(path) == ["sinui", "daksaren", "naming"]


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
    assert [p.id for p in state.cfg.products] == ["sinui", "myeongun", "daksaren", "eumpa"]
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
