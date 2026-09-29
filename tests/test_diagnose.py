from datetime import datetime

from jisikin.diagnose import run_diagnostics
from jisikin.naver import KST, NaverError, QuestionDetail, RawQuestion


class Client:
    credentials = None

    def __init__(self, detail=None, fail=False):
        self.detail = detail
        self.fail = fail

    def search_web(self, query, page=1):
        if self.fail:
            raise NaverError("지식iN 연결 실패: ProxyError")
        return [RawQuestion(doc_id="1", url="https://kin.naver.com/qna/detail.naver?docId=1", title="타로 질문", snippet="s", answer_count=0)]

    def fetch_detail(self, url):
        return self.detail


def test_all_good(example_cfg):
    d = QuestionDetail(title="t", body="b", answer_count=1, asked_at=datetime(2026, 9, 29, 10, 0, tzinfo=KST), reward=10)
    ok, lines = run_diagnostics(example_cfg, Client(detail=d))
    text = "\n".join(lines)
    assert ok, text
    assert "검색 (web): 1건" in text and "답변수=1" in text and "작성일=2026-09-29 10:00" in text


def test_partial_detail_is_flagged(example_cfg):
    ok, lines = run_diagnostics(example_cfg, Client(detail=QuestionDetail(title="t", body="b")))
    assert not ok
    assert any("[??] 상세 페이지" in line and "답변수=모름" in line for line in lines)


def test_connection_failure(example_cfg):
    ok, lines = run_diagnostics(example_cfg, Client(fail=True))
    assert not ok and any("[오류] 검색 실패" in line for line in lines)
