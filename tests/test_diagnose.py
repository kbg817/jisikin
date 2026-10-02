from datetime import datetime

from jisikin.diagnose import run_diagnostics
from jisikin.naver import KST, NaverError, QuestionDetail, RawQuestion


def _rq(doc_id, title):
    return RawQuestion(doc_id=doc_id, url=f"https://kin.naver.com/qna/detail.naver?docId={doc_id}", title=title, snippet="s", answer_count=0)


class Client:
    credentials = None

    def __init__(self, detail=None, fail=False, integrated=None):
        self.detail = detail
        self.fail = fail
        self.integrated = integrated if integrated is not None else [_rq("9", "신점 잘보는곳 추천")]
        self.integrated_calls = []

    def search_web(self, query, page=1):
        if self.fail:
            raise NaverError("지식iN 연결 실패: ProxyError")
        return [_rq("1", "타로 질문")]

    def search_integrated(self, query, platform="pc"):
        self.integrated_calls.append((query, platform))
        if self.fail:
            raise NaverError("네이버 통합검색 연결 실패: ProxyError")
        return self.integrated

    def search_kin_by_views(self, query, count=10):
        if self.fail:
            raise NaverError("지식iN 연결 실패: ProxyError")
        return self.integrated[:count]

    def fetch_detail(self, url):
        return self.detail

    def autocomplete(self, query):
        if self.fail:
            raise NaverError("네이버 자동완성 연결 실패: ProxyError")
        return [query, f"{query} 추천", f"{query} 원인"]


def test_all_good(example_cfg):
    d = QuestionDetail(title="t", body="b", answer_count=1, asked_at=datetime(2026, 9, 29, 10, 0, tzinfo=KST), reward=10)
    client = Client(detail=d)
    ok, lines = run_diagnostics(example_cfg, client)
    text = "\n".join(lines)
    assert ok, text
    assert "검색 (web): 1건" in text and "답변수=1" in text and "작성일=2026-09-29 10:00" in text
    targets = example_cfg.exposure_targets()
    first_kw = targets[0][1]
    second_kw = next(kw for p, kw in targets if p.id != targets[0][0].id)  # 다른 제품의 첫 검색어
    assert f"상위노출 '{first_kw}' 통합검색 PC: 지식iN 글 1개" in text
    assert client.integrated_calls == [(first_kw, "pc"), (first_kw, "mobile"), (second_kw, "pc"), (second_kw, "mobile")]
    assert "[OK] 네이버 자동완성 '신점': 3개" in text
    assert "[--] 검색광고 API: 키 없음" in text


def test_searchad_check(example_cfg):
    class Ad:
        def keyword_stats(self, hints):
            return [{"keyword": "신점", "pc": 1200, "mobile": 9800}]

    d = QuestionDetail(title="t", body="b", answer_count=1, asked_at=datetime(2026, 9, 29, tzinfo=KST))
    ok, lines = run_diagnostics(example_cfg, Client(detail=d), ad_client=Ad())
    assert any("[OK] 검색광고 API: 연관 키워드 1개 (예: 신점 PC 1200 / 모바일 9800)" in line for line in lines)


def test_partial_detail_is_flagged(example_cfg):
    ok, lines = run_diagnostics(example_cfg, Client(detail=QuestionDetail(title="t", body="b")))
    assert not ok
    assert any("[??] 상세 페이지" in line and "답변수=모름" in line for line in lines)


def test_connection_failure(example_cfg):
    ok, lines = run_diagnostics(example_cfg, Client(fail=True))
    assert not ok and any("[오류] 검색 실패" in line for line in lines)
    assert any("[오류] 상위노출" in line for line in lines)


def test_no_exposure_results_is_reported(example_cfg):
    d = QuestionDetail(title="t", body="b", answer_count=1, asked_at=datetime(2026, 9, 29, tzinfo=KST))
    ok, lines = run_diagnostics(example_cfg, Client(detail=d, integrated=[]))
    assert any("[??] 상위노출" in line for line in lines)


def test_debug_lines_when_nothing_found(example_cfg):
    d = QuestionDetail(title="t", body="b", answer_count=1)  # 작성일 모름
    client = Client(detail=d, integrated=[])
    client.last_status = 200
    client.last_html = '<html><title>인천 건선 : 네이버 검색</title><script>captcha</script><body>지식iN 결과 <a href="https://kin.naver.com/x">x</a> 2026.09.01.</body></html>'
    ok, lines = run_diagnostics(example_cfg, client)
    text = "\n".join(lines)
    assert not ok
    assert "진단(상세): HTTP 200" in text and "'날짜' 1회" in text
    assert "진단(통합검색 PC): HTTP 200" in text and "제목=인천 건선 : 네이버 검색" in text and "차단/캡차 문구=없음" in text
    assert "'지식iN 링크' 1회" in text
    assert "통합검색에서 지식iN 글을 하나도 못 찾았습니다" in text
