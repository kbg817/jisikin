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

    def fetch_detail(self, url):
        return self.detail


def test_all_good(example_cfg):
    d = QuestionDetail(title="t", body="b", answer_count=1, asked_at=datetime(2026, 9, 29, 10, 0, tzinfo=KST), reward=10)
    client = Client(detail=d)
    ok, lines = run_diagnostics(example_cfg, client)
    text = "\n".join(lines)
    assert ok, text
    assert "검색 (web): 1건" in text and "답변수=1" in text and "작성일=2026-09-29 10:00" in text
    first_kw = example_cfg.exposure_targets()[0][1]
    assert f"상위노출 '{first_kw}' 통합검색 PC: 지식iN 글 1개" in text
    assert client.integrated_calls == [(first_kw, "pc"), (first_kw, "mobile")]


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
