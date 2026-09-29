from datetime import datetime

import pytest

from jisikin.naver import (
    KST,
    NaverClient,
    NaverError,
    extract_questions,
    parse_api_items,
    parse_detail,
    parse_kin_url,
    parse_korean_datetime,
)

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=KST)


def test_parse_kin_url_normalizes():
    doc_id, url = parse_kin_url(
        "https://kin.naver.com/qna/detail.naver?d1id=8&dirId=80203&docId=487654321&qb=7YOA66Gc&enc=utf8&section=kin.qna&rank=1"
    )
    assert doc_id == "487654321"
    assert url == "https://kin.naver.com/qna/detail.naver?d1id=8&dirId=80203&docId=487654321"


def test_parse_kin_url_mobile_and_invalid():
    assert parse_kin_url("https://m.kin.naver.com/mobile/qna/detail.naver?d1id=7&dirId=70101&docId=123")[0] == "123"
    assert parse_kin_url("https://kin.naver.com/qna/list.naver?dirId=8") is None
    assert parse_kin_url("https://blog.naver.com/qna/detail.naver?docId=1") is None
    assert parse_kin_url("https://kin.naver.com/qna/detail.naver?docId=abc") is None


def test_parse_api_items_strips_tags_and_entities():
    items = parse_api_items(
        [
            {
                "title": "<b>타로</b> 재회운 봐주실 분 &quot;급해요&quot;",
                "link": "https://kin.naver.com/qna/detail.naver?d1id=8&dirId=81702&docId=480000001&qb=abc",
                "description": "전남친이랑 <b>타로</b>로 재회운을 봤는데 &amp; 결과가...",
            },
            {"title": "not a question", "link": "https://blog.naver.com/x", "description": ""},
        ]
    )
    assert len(items) == 1
    assert items[0].title == '타로 재회운 봐주실 분 "급해요"'
    assert items[0].snippet == "전남친이랑 타로로 재회운을 봤는데 & 결과가..."


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026.09.28.", datetime(2026, 9, 28, tzinfo=KST)),
        ("작성일 2026.09.28. 14:30", datetime(2026, 9, 28, 14, 30, tzinfo=KST)),
        ("2026. 9. 3. 오후 2:05", datetime(2026, 9, 3, 14, 5, tzinfo=KST)),
        ("3시간 전", datetime(2026, 9, 29, 12, 0, tzinfo=KST)),
        ("15분 전", datetime(2026, 9, 29, 14, 45, tzinfo=KST)),
        ("어제", datetime(2026, 9, 28, 15, 0, tzinfo=KST)),
        ("방금", NOW),
        ("10:20", datetime(2026, 9, 29, 10, 20, tzinfo=KST)),
        ("조회수 12", None),
    ],
)
def test_parse_korean_datetime(text, expected):
    assert parse_korean_datetime(text, NOW) == expected


SEARCH_HTML = """
<html><body><div id="s_content"><ul class="basic1">
<li>
  <dl>
    <dt><a href="https://kin.naver.com/qna/detail.naver?d1id=8&amp;dirId=81702&amp;docId=480000010&amp;qb=x" class="_nclicks:kin.txt _searchListTitleAnchor" target="_blank">전남친 <b>재회</b> 가능할까요</a></dt>
    <dd class="txt_inline">2026.09.29.</dd>
    <dd>헤어진지 3달 됐는데 연락이 올까요 <b>재회</b>운 궁금해요</dd>
    <dd class="txt_block"><span class="buttonset"><a href="/qna/list.naver?dirId=81702" class="txt_g1">운세, 사주</a></span> <span class="hit">답변수 0</span></dd>
  </dl>
</li>
<li>
  <dl>
    <dt><a href="/qna/detail.naver?d1id=7&amp;dirId=70112&amp;docId=480000009" target="_blank">팔 오돌토돌 모공각화증</a></dt>
    <dd class="txt_inline">3시간 전</dd>
    <dd>팔뚝에 닭살처럼 오돌토돌 올라와요</dd>
    <dd class="txt_block"><a href="/qna/list.naver?dirId=70112">피부과</a> 답변수 2 <a href="/qna/detail.naver?d1id=7&amp;dirId=70112&amp;docId=480000009#answer">답변하기</a></dd>
  </dl>
</li>
</ul></div></body></html>
"""


def test_extract_questions_from_search_page():
    qs = extract_questions(SEARCH_HTML, "https://kin.naver.com/search/list.naver?query=x")
    assert [q.doc_id for q in qs] == ["480000010", "480000009"]
    a, b = qs
    assert a.title == "전남친 재회 가능할까요"
    assert a.answer_count == 0
    assert a.asked_at == datetime(2026, 9, 29, tzinfo=KST)
    assert "연락이 올까요" in a.snippet and "답변수" not in a.snippet
    assert b.title == "팔 오돌토돌 모공각화증"
    assert b.url == "https://kin.naver.com/qna/detail.naver?d1id=7&dirId=70112&docId=480000009"
    assert b.answer_count == 2


LIST_HTML = """
<table class="boardtype2"><tbody>
<tr><td class="title"><a href="/qna/detail.naver?d1id=8&dirId=81702&docId=480000100">궁합 좀 봐주세요</a></td>
    <td class="field"><a href="/qna/list.naver?dirId=81702">운세, 사주</a></td>
    <td class="t_num">0</td><td class="t_num">2026.09.29.</td></tr>
<tr><td class="title"><a href="/qna/detail.naver?d1id=8&dirId=81702&docId=480000099">올해 사업운</a></td>
    <td class="field">운세, 사주</td><td class="t_num">1</td><td class="t_num">2026.09.28.</td></tr>
</tbody></table>
"""


def test_extract_questions_from_directory_table():
    qs = extract_questions(LIST_HTML, "https://kin.naver.com/qna/list.naver?dirId=81702")
    assert [q.title for q in qs] == ["궁합 좀 봐주세요", "올해 사업운"]
    assert qs[0].asked_at == datetime(2026, 9, 29, tzinfo=KST)


DETAIL_HTML = """
<html><head>
<meta property="og:title" content="전남친 재회 가능할까요 : 지식iN">
<meta property="og:description" content="헤어진지 3달...">
</head><body>
<div class="c-heading _questionContentsArea">
  <div class="c-heading__title"><div class="c-heading__title-inner"><div class="title">전남친 재회 가능할까요</div></div></div>
  <div class="c-heading__content">헤어진지 3달 됐는데 연락이 올까요?
     97년생이고 전남친은 95년생이에요.</div>
  <div class="c-userinfo"><span class="c-userinfo__info">작성일 2026.09.29. 13:10</span><span class="c-userinfo__info">조회수 15</span></div>
  <span class="c-heading__reward">내공 50</span>
</div>
<div class="answer-content__list">
  <div class="answer-content__item">답변1</div>
  <div class="answer-content__item">답변2</div>
</div>
</body></html>
"""


def test_parse_detail_full():
    d = parse_detail(DETAIL_HTML, NOW)
    assert d.title == "전남친 재회 가능할까요"
    assert d.body.startswith("헤어진지 3달 됐는데") and "95년생" in d.body
    assert d.asked_at == datetime(2026, 9, 29, 13, 10, tzinfo=KST)
    assert d.reward == 50
    assert d.answer_count == 2


def test_parse_detail_falls_back_to_meta_and_zero_answers():
    html = """<html><head><meta property="og:title" content="타로 봐주세요 : 네이버 지식iN">
    <meta property="og:description" content="짝사랑 속마음이 궁금해요"></head>
    <body><p>아직 답변이 없습니다. 첫 번째 답변을 남겨주세요.</p></body></html>"""
    d = parse_detail(html, NOW)
    assert d.title == "타로 봐주세요"
    assert d.body == "짝사랑 속마음이 궁금해요"
    assert d.answer_count == 0
    assert d.asked_at is None


class FakeResponse:
    def __init__(self, status=200, json_data=None, text="", encoding="utf-8"):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.encoding = encoding
        self.apparent_encoding = "utf-8"

    def json(self):
        if self._json is None:
            raise ValueError
        return self._json


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.response


API_DATA = {"items": [{"title": "<b>건선</b> 질문", "link": "https://kin.naver.com/qna/detail.naver?d1id=7&dirId=1&docId=5", "description": "d"}]}


def test_search_api_hub_key():
    """NAVER API HUB(네이버클라우드) 키: 새 주소 + X-NCP-APIGW 헤더로 먼저 시도한다."""
    session = FakeSession(FakeResponse(json_data=API_DATA))
    client = NaverClient(credentials=("id", "secret"), session=session)
    items = client.search_api("건선", 150)
    assert items[0].title == "건선 질문"
    url, params, headers = session.calls[0]
    assert url == "https://naverapihub.apigw.ntruss.com/search/v1/kin"
    assert params["display"] == 100 and params["sort"] == "date" and params["format"] == "json"
    assert headers == {"X-NCP-APIGW-API-KEY-ID": "id", "X-NCP-APIGW-API-KEY": "secret"}
    assert client.api_provider == "hub" and len(session.calls) == 1


class RoutingSession(FakeSession):
    """HUB 주소는 401, 개발자센터 주소는 200 (예전에 발급받은 키)."""

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if "ntruss" in url:
            return FakeResponse(status=401, json_data={"error": {"errorCode": "200", "message": "Authentication Failed"}})
        return FakeResponse(json_data=API_DATA)


def test_search_api_falls_back_to_developers_key_and_remembers():
    session = RoutingSession(None)
    client = NaverClient(credentials=("old-id", "secret"), session=session)
    assert client.search_api("건선")[0].title == "건선 질문"
    assert [c[0] for c in session.calls] == [
        "https://naverapihub.apigw.ntruss.com/search/v1/kin",
        "https://openapi.naver.com/v1/search/kin.json",
    ]
    assert session.calls[1][2] == {"X-Naver-Client-Id": "old-id", "X-Naver-Client-Secret": "secret"}
    # 다음부터는 성공한 곳으로 바로 (다른 클라이언트 객체여도)
    session.calls.clear()
    NaverClient(credentials=("old-id", "secret"), session=session).search_api("타로", sort="sim")
    assert [c[0] for c in session.calls] == ["https://openapi.naver.com/v1/search/kin.json"]


def test_search_api_wrong_key_message():
    session = FakeSession(FakeResponse(status=401, json_data={"error": {"message": "Authentication Failed"}}))
    with pytest.raises(NaverError) as ei:
        NaverClient(credentials=("bad", "bad"), session=session).search_api("q")
    assert ei.value.fatal and ei.value.auth
    assert "NAVER API HUB" in str(ei.value) and "Authentication Failed" in str(ei.value)
    assert len(session.calls) == 2  # 두 발급처 모두 시도


@pytest.mark.parametrize("status, fatal", [(401, True), (429, True), (500, False)])
def test_search_api_errors(status, fatal):
    client = NaverClient(credentials=("id", "s"), session=FakeSession(FakeResponse(status=status, json_data={"errorMessage": "x"})))
    with pytest.raises(NaverError) as ei:
        client.search_api("q")
    assert ei.value.fatal is fatal


def test_search_api_without_credentials():
    with pytest.raises(NaverError):
        NaverClient(credentials=None, session=FakeSession(FakeResponse())).search_api("q")


def test_search_web_uses_date_sort():
    session = FakeSession(FakeResponse(text=SEARCH_HTML))
    client = NaverClient(session=session, delay_seconds=0)
    items = client.search_web("재회", page=2)
    assert len(items) == 2
    _, params, _ = session.calls[0]
    assert params["sort"] == "date" and params["page"] == 2 and params["query"] == "재회"


def test_inline_tags_do_not_split_words():
    html = '<ul><li><dl><dt><a href="/qna/detail.naver?docId=77"><b>재회</b>운 <strong>타로</strong>로 봤어요</a></dt><dd>본문<br>둘째 줄</dd></dl></li></ul>'
    q = extract_questions(html)[0]
    assert q.title == "재회운 타로로 봤어요"
    assert q.snippet == "본문 둘째 줄"


def test_scripts_are_ignored():
    html = "<html><body><script>var t='답변을 기다리는 질문';</script><div class='c-heading__title'>제목</div></body></html>"
    d = parse_detail(html, NOW)
    assert d.title == "제목"
    assert d.answer_count is None


def test_detail_date_from_script_data():
    html = """<html><head><title>사주 좀 풀어주세요 : 지식iN</title>
    <script>window.__DATA__ = {"question":{"title":"사주 좀 풀어주세요","regDate":"2026-09-28 21:14:05","viewCount":321}};</script>
    </head><body><div class="c-heading"><div class="c-heading__title">사주 좀 풀어주세요</div>
    <div class="c-heading__content">생년월일 알려드려요</div></div></body></html>"""
    d = parse_detail(html, NOW)
    assert d.asked_at == datetime(2026, 9, 28, 21, 14, 5, tzinfo=KST)


def test_detail_date_inside_heading_without_label():
    html = """<div class="c-heading"><div class="c-heading__title">제목</div><div class="c-heading__content">본문</div>
    <div class="c-userinfo"><span class="nick">익명</span><span class="c-userinfo__time">2026.09.27.</span></div></div>"""
    assert parse_detail(html, NOW).asked_at == datetime(2026, 9, 27, tzinfo=KST)


def test_detail_date_near_label_outside_heading():
    html = """<div class="c-heading"><div class="c-heading__title">제목</div></div>
    <div class="etc"><em>등록</em> <span>3시간 전</span></div>"""
    assert parse_detail(html, NOW).asked_at == datetime(2026, 9, 29, 12, 0, tzinfo=KST)


def test_extract_kin_links_from_script_json():
    from jisikin.naver import extract_kin_links_raw

    html = r"""<script>var data = {"items":[
      {"url":"https:\/\/kin.naver.com\/qna\/detail.naver?d1id=7&dirId=70112&docId=490000001"},
      {"url":"https://m.kin.naver.com/mobile/qna/detail.naver?d1id=7&amp;dirId=70112&amp;docId=490000002"},
      {"url":"https:\/\/kin.naver.com\/qna\/detail.naver?d1id=7&dirId=70112&docId=490000001#answer"}]}</script>"""
    items = extract_kin_links_raw(html)
    assert [q.doc_id for q in items] == ["490000001", "490000002"]
    assert items[0].url == "https://kin.naver.com/qna/detail.naver?d1id=7&dirId=70112&docId=490000001"
    assert items[0].title == "(제목 확인 중)"


def test_search_integrated_falls_back_to_raw_links():
    html = r'<html><body><div id="app"></div><script>__STATE__={"kin":["https:\/\/kin.naver.com\/qna\/detail.naver?dirId=1&docId=777"]}</script></body></html>'
    client = NaverClient(session=FakeSession(FakeResponse(text=html)), delay_seconds=0)
    items = client.search_integrated("인천 건선", "pc")
    assert [q.doc_id for q in items] == ["777"]
    assert client.last_status == 200 and "__STATE__" in client.last_html
