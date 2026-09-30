import pytest

from jisikin.config import Category, ConfigError, Product, parse_config
from jisikin.matcher import Matcher


def make(**kw):
    base = dict(id="p", name="P", keywords=[], categories=[], exclude=[], min_score=2)
    base.update(kw)
    return Product(**base)


def test_keyword_spaces_are_optional_only_where_written():
    m = Matcher([make(keywords=["키 크는 법"])])
    assert m.best(m.classify("키크는법 알려주세요"))
    assert m.best(m.classify("키 크는법 알려주세요"))
    assert m.best(m.classify("키 크는 법 알려주세요"))


def test_short_keyword_does_not_match_across_words():
    m = Matcher([make(keywords=["건선"])])
    assert m.classify("조건 선택 어떻게 하나요") == []
    assert m.best(m.classify("팔에 건선이 생겼어요"))


def test_title_scores_more_than_body():
    m = Matcher([make(keywords=["타로"])])
    title = m.classify("타로 봐주세요")[0]
    body = m.classify("질문 있어요", "타로 결과가 궁금해요")[0]
    assert title.score == 3 and body.score == 2


def test_mask_only_removes_the_phrase():
    m = Matcher([make(keywords=["사주"], exclude=["사주세요", "사주고 싶"])])
    assert m.classify("엄마가 옷 사주세요 라고 했어요") == []
    assert m.classify("여자친구 선물 사주고 싶은데 추천") == []
    assert m.best(m.classify("사주 고민이 있어요"))  # '사주 고' 는 띄어쓰기가 달라서 가리지 않음
    assert m.best(m.classify("사주 봐주세요 (옷은 엄마가 사주세요)"))


def test_block_exclusion_drops_product():
    m = Matcher([make(keywords=["성장기"], exclude=["!주식"])])
    assert m.classify("성장기 기업 주식 추천") == []
    assert m.best(m.classify("성장기 아이 영양제"))


def test_category_only_in_title_is_relevant_but_body_only_is_not():
    p = make(keywords=["사주"], categories=[Category("재회운", ["재회", "전 남친"])])
    m = Matcher([p])
    t = m.classify("전남친이랑 재회할 수 있을까요")[0]
    assert t.relevant and t.categories == ["재회운"] and t.score == 2
    b = m.classify("고민 있어요", "전남친이랑 재회할 수 있을까요")[0]
    assert not b.relevant and b.score == 1


def test_regex_keyword():
    m = Matcher([make(keywords=["re:사주(?!세요)"])])
    assert m.classify("옷 사주세요") == []
    assert m.best(m.classify("사주 풀이"))


def test_best_picks_highest_relevant_product():
    a = make(id="a", name="A", keywords=["타로"])
    b = make(id="b", name="B", keywords=["건선", "두피 건선"])
    m = Matcher([a, b])
    res = m.classify("두피 건선 관리법", "타로는 아니고요")
    assert m.best(res).product_id == "b"
    assert {r.product_id for r in res} == {"a", "b"}


def test_terms_are_actual_text_for_highlighting():
    m = Matcher([make(keywords=["전 남친"])])
    assert m.classify("전남친 연락")[0].terms == ["전남친"]


def test_bad_regex_in_config_is_reported():
    text = """
products:
  - id: x
    name: X
    keywords: ["re:사주(("]
"""
    with pytest.raises(ConfigError):
        parse_config(text)


@pytest.mark.parametrize(
    "title, body, product, category",
    [
        ("헤어진 전남친이랑 다시 만날 수 있을까요? 타로 봤는데", "", "sinui", "재회운"),
        ("사주 봐주실 분 계신가요 96년생", "올해 이직운이 궁금합니다", "myeongyeon", "직장·시험운"),
        ("타로 잘 보는 곳 추천해주세요", "", "sinui", "점집·상담 추천"),
        ("개업 날짜 택일 사주로 봐주세요", "사업운이 궁금해요", "myeongyeon", "재물·사업운"),
        ("치질 수술 후기 궁금해요", "", "chidifit", "병원·수술"),
        ("변 볼 때 휴지에 피가 묻어나요", "항문 출혈이 며칠째", "chidifit", "출혈·통증·가려움"),
        ("초등학생 아들 키가 너무 작아요", "반에서 제일 작은데 성장판 검사 받아야 할까요", "eumpa", "자녀 키 고민"),
        ("키 크는 방법 알려주세요 중2입니다", "", "eumpa", "키 크는 방법"),
        ("성조숙증 검사 받아야 하나요", "초경이 빨라서 걱정", "eumpa", "성조숙증"),
        ("팔에 오돌토돌한 거 모공각화증인가요?", "", "daksaren", "모공각화증"),
        ("두피 건선 샴푸 추천", "", "daksaren", "건선"),
    ],
)
def test_example_config_classifies_typical_questions(example_cfg, title, body, product, category):
    m = Matcher(example_cfg.products)
    best = m.best(m.classify(title, body))
    assert best is not None, title
    assert best.product_id == product
    assert category in best.categories


@pytest.mark.parametrize(
    "title, body",
    [
        ("엄마가 새 핸드폰 사주세요 라고 조르는데", ""),
        ("조건 선택 어떻게 하나요", "엑셀 함수 질문입니다"),
        ("식물 성장 조명 추천", ""),
        ("성장기 기업 주식 추천 부탁드립니다", ""),
        ("노트북 추천해주세요", "게임용으로 쓸 거예요"),
        ("헤어진 전남친이랑 다시 만날 수 있을까요?", ""),  # 신점·타로·사주 얘기가 없으면 수집 안 함
        ("치열 교정 비용 얼마나 드나요", "교정 치과 추천"),
    ],
)
def test_example_config_ignores_unrelated_questions(example_cfg, title, body):
    m = Matcher(example_cfg.products)
    assert m.best(m.classify(title, body)) is None, title


def test_require_keyword_uses_categories_only_for_sorting(example_cfg):
    m = Matcher(example_cfg.products)
    sinui = next(x for x in m.classify("재회 가능성 있을까요", "헤어진 지 한 달") if x.product_id == "sinui")
    assert sinui.score >= 2 and not sinui.relevant and "재회운" in sinui.categories
