"""수집한 질문을 SQLite 에 저장한다."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .matcher import Matcher
from .naver import KST, UNKNOWN_TITLE, NaverError, QuestionDetail, RawQuestion

STATUSES = ("new", "opened", "answered", "skipped")
TODO_STATUSES = ("new", "opened")

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    doc_id            TEXT PRIMARY KEY,
    url               TEXT NOT NULL,
    title             TEXT NOT NULL,
    snippet           TEXT NOT NULL DEFAULT '',
    body              TEXT NOT NULL DEFAULT '',
    dir_name          TEXT NOT NULL DEFAULT '',
    answer_count      INTEGER,
    reward            INTEGER,
    asked_at          TEXT,
    sources           TEXT NOT NULL DEFAULT '[]',
    product           TEXT,
    score             REAL NOT NULL DEFAULT 0,
    categories        TEXT NOT NULL DEFAULT '[]',
    matches           TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'new',
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    status_changed_at TEXT,
    detail_fetched_at TEXT,
    detail_error      TEXT,
    draft             TEXT,
    draft_at          TEXT,
    draft_edited      INTEGER NOT NULL DEFAULT 0,  -- 1: 직원이 AI 초안을 고쳐서 저장함
    views             INTEGER,
    in_feed           INTEGER NOT NULL DEFAULT 1,  -- 1: 새 질문 수집으로 찾음, 0: 상위노출 확인으로만 찾음
    status_by         TEXT                         -- 상태를 마지막으로 바꾼 직원 아이디
);
CREATE INDEX IF NOT EXISTS idx_q_product ON questions(product, status);
CREATE INDEX IF NOT EXISTS idx_q_first_seen ON questions(first_seen);

-- 상위노출 확인 1회 (검색어 × 검색 위치)
CREATE TABLE IF NOT EXISTS exposure_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product     TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    source      TEXT NOT NULL,          -- pc / mobile / kin
    checked_at  TEXT NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_kw ON exposure_checks(product, keyword, source, id);

CREATE TABLE IF NOT EXISTS exposure_ranks (
    check_id    INTEGER NOT NULL,
    doc_id      TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    PRIMARY KEY (check_id, doc_id)
);

CREATE TABLE IF NOT EXISTS views_history (
    doc_id      TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    views       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_views_doc ON views_history(doc_id, checked_at);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    mode          TEXT,
    queries       INTEGER DEFAULT 0,
    fetched       INTEGER DEFAULT 0,
    new_total     INTEGER DEFAULT 0,
    new_relevant  INTEGER DEFAULT 0,
    details       INTEGER DEFAULT 0,
    errors        TEXT NOT NULL DEFAULT '[]'
);

-- 직원 계정 (관리자 계정은 서버 환경변수로 따로 둔다)
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

-- 누가 언제 어떤 글을 답변완료/제외/열어봄 했는지
CREATE TABLE IF NOT EXISTS activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    TEXT NOT NULL,
    username  TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity(at);

-- 간단한 설정값 저장소 (메인 키워드 등)
CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- 메인 키워드에서 자동으로 만든 상위노출 검색어
CREATE TABLE IF NOT EXISTS auto_keywords (
    product     TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    seed        TEXT NOT NULL DEFAULT '',
    sources     TEXT NOT NULL DEFAULT '[]',
    pc          INTEGER,
    mobile      INTEGER,
    score       REAL NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    user_set    INTEGER NOT NULL DEFAULT 0,   -- 1: 사람이 직접 켜고/끔 → 다시 생성해도 유지
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (product, keyword)
);

-- AI 초안이 참고할 모범 답변 (관리자가 ⭐ 로 지정한 것만 사용)
CREATE TABLE IF NOT EXISTS answer_examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product     TEXT NOT NULL,
    doc_id      TEXT,                            -- 직원이 답변완료한 질문에서 온 경우
    title       TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    answer      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',  -- manual: 직접 넣음, final: 직원이 올린 답변
    edited      INTEGER NOT NULL DEFAULT 0,      -- 1: 직원이 AI 초안을 고쳐서 올림
    starred     INTEGER NOT NULL DEFAULT 0,      -- 1: AI 초안의 예시로 사용
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    starred_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ex_doc ON answer_examples(doc_id) WHERE doc_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ex_product ON answer_examples(product, starred);

-- 유튜브 영상 · 쓰레드 글 (상태 값은 questions 와 같음: answered = 댓글완료)
CREATE TABLE IF NOT EXISTS social_posts (
    post_id           TEXT PRIMARY KEY,            -- yt:<영상 id> / th:<글 id>
    platform          TEXT NOT NULL,               -- youtube / threads
    url               TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    body              TEXT NOT NULL DEFAULT '',
    author            TEXT NOT NULL DEFAULT '',
    author_url        TEXT NOT NULL DEFAULT '',
    thumbnail         TEXT NOT NULL DEFAULT '',
    published_at      TEXT,
    views             INTEGER,
    likes             INTEGER,
    comments          INTEGER,
    duration          INTEGER,                     -- (유튜브) 영상 길이(초)
    is_short          INTEGER,                     -- (유튜브) 숏츠면 1
    queries           TEXT NOT NULL DEFAULT '[]',  -- 이 글을 찾은 검색어
    product           TEXT,
    score             REAL NOT NULL DEFAULT 0,
    categories        TEXT NOT NULL DEFAULT '[]',
    matches           TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'new',
    status_by         TEXT,
    status_changed_at TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    draft             TEXT,
    draft_at          TEXT,
    draft_edited      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_social_list ON social_posts(platform, product, status);
"""


def now_kst() -> datetime:
    return datetime.now(KST)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(KST).isoformat(timespec="seconds") if dt else None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self._conn() as c:
            _migrate(c)
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            if self._memory_conn is not None:
                conn = self._memory_conn
            else:
                conn = sqlite3.connect(self.path, timeout=30)
                conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                if self._memory_conn is None:
                    conn.close()

    # ------------------------------------------------------------ 수집 결과 반영

    def upsert_raw(self, rq: RawQuestion, source: str, now: datetime | None = None, feed: bool = True) -> bool:
        """질문을 저장한다. 처음 본 질문이면 True.

        feed=False 는 상위노출 확인으로 찾은 글 (옛날 글일 수 있어 '새 질문' 목록에는 넣지 않음).
        """
        now_s = iso(now or now_kst())
        with self._conn() as c:
            row = c.execute("SELECT sources, snippet, answer_count, asked_at FROM questions WHERE doc_id=?", (rq.doc_id,)).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO questions (doc_id, url, title, snippet, dir_name, answer_count, asked_at,
                                              sources, first_seen, last_seen, in_feed)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rq.doc_id, rq.url, rq.title, rq.snippet, rq.dir_name, rq.answer_count,
                        iso(rq.asked_at), json.dumps([source], ensure_ascii=False), now_s, now_s, int(feed),
                    ),
                )
                return True
            sources = json.loads(row["sources"] or "[]")
            if source not in sources:
                sources.append(source)
            c.execute(
                """UPDATE questions SET last_seen=?, sources=?,
                          title=CASE WHEN title=? AND ?<>? THEN ? ELSE title END,
                          snippet=CASE WHEN length(?) > length(snippet) THEN ? ELSE snippet END,
                          answer_count=COALESCE(?, answer_count),
                          asked_at=COALESCE(asked_at, ?),
                          in_feed=MAX(in_feed, ?)
                   WHERE doc_id=?""",
                (
                    now_s, json.dumps(sources[-20:], ensure_ascii=False),
                    UNKNOWN_TITLE, rq.title, UNKNOWN_TITLE, rq.title,  # 제목 모름 → 제목을 알게 되면 채움
                    rq.snippet, rq.snippet,
                    rq.answer_count, iso(rq.asked_at), int(feed), rq.doc_id,
                ),
            )
            return False

    def update_detail(self, doc_id: str, d: QuestionDetail, now: datetime | None = None) -> None:
        now_s = iso(now or now_kst())
        with self._conn() as c:
            c.execute(
                """UPDATE questions SET
                          title=CASE WHEN ?<>'' THEN ? ELSE title END,
                          body=CASE WHEN ?<>'' THEN ? ELSE body END,
                          answer_count=COALESCE(?, answer_count),
                          asked_at=COALESCE(?, asked_at),
                          reward=COALESCE(?, reward),
                          views=COALESCE(?, views),
                          detail_fetched_at=?, detail_error=NULL
                   WHERE doc_id=?""",
                (
                    d.title, d.title, d.body, d.body, d.answer_count, iso(d.asked_at), d.reward, d.views,
                    now_s, doc_id,
                ),
            )
            if d.views is not None:
                c.execute("INSERT INTO views_history (doc_id, checked_at, views) VALUES (?,?,?)", (doc_id, now_s, d.views))

    def mark_detail_failed(self, doc_id: str, error: str, now: datetime | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE questions SET detail_fetched_at=?, detail_error=? WHERE doc_id=?",
                (iso(now or now_kst()), error[:300], doc_id),
            )

    def classify(self, matcher: Matcher, doc_ids: list[str] | None = None) -> int:
        """저장된 질문을 다시 분류한다. doc_ids 가 None 이면 전체. 관련 질문 수를 돌려준다."""
        relevant = 0
        with self._conn() as c:
            if doc_ids is None:
                rows = c.execute("SELECT doc_id, title, snippet, body FROM questions").fetchall()
            else:
                rows = []
                ids = list(dict.fromkeys(doc_ids))
                for i in range(0, len(ids), 500):
                    chunk = ids[i : i + 500]
                    rows += c.execute(
                        f"SELECT doc_id, title, snippet, body FROM questions WHERE doc_id IN ({','.join('?' * len(chunk))})",
                        chunk,
                    ).fetchall()
            for r in rows:
                matches = matcher.classify(r["title"], r["body"] or r["snippet"])
                best = matcher.best(matches)
                if best:
                    relevant += 1
                c.execute(
                    "UPDATE questions SET product=?, score=?, categories=?, matches=? WHERE doc_id=?",
                    (
                        best.product_id if best else None,
                        best.score if best else (matches[0].score if matches else 0),
                        json.dumps(best.categories if best else [], ensure_ascii=False),
                        json.dumps([m.to_dict() for m in matches], ensure_ascii=False),
                        r["doc_id"],
                    ),
                )
        return relevant

    def pending_details(self, limit: int, max_age_days: int, now: datetime | None = None) -> list[sqlite3.Row]:
        """상세 정보를 아직 안 가져온 관련 질문 (최근 것부터)."""
        since = iso((now or now_kst()) - timedelta(days=max_age_days))
        with self._conn() as c:
            return c.execute(
                """SELECT doc_id, url FROM questions
                   WHERE product IS NOT NULL AND detail_fetched_at IS NULL AND in_feed=1
                     AND status IN ('new','opened') AND first_seen >= ?
                   ORDER BY CAST(doc_id AS INTEGER) DESC LIMIT ?""",
                (since, limit),
            ).fetchall()

    def purge(self, keep_days: int, now: datetime | None = None) -> int:
        """오래된 미처리/무관 질문 정리. 답변완료는 보관."""
        now = now or now_kst()
        old = iso(now - timedelta(days=keep_days))
        old_irrelevant = iso(now - timedelta(days=min(7, keep_days)))
        with self._conn() as c:
            n = c.execute(
                "DELETE FROM questions WHERE status<>'answered' AND last_seen < ?", (old,)
            ).rowcount
            n += c.execute(
                "DELETE FROM questions WHERE product IS NULL AND status='new' AND last_seen < ?", (old_irrelevant,)
            ).rowcount
            c.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 200)")
            c.execute("DELETE FROM exposure_checks WHERE checked_at < ?", (old,))
            c.execute("DELETE FROM exposure_ranks WHERE check_id NOT IN (SELECT id FROM exposure_checks)")
            c.execute("DELETE FROM views_history WHERE checked_at < ?", (old,))
            c.execute("DELETE FROM kv WHERE key LIKE 'api_calls:%' AND key < ?", (f"api_calls:{now - timedelta(days=40):%Y-%m-%d}",))
        return n

    # ------------------------------------------------------------ 화면용

    def get(self, doc_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM questions WHERE doc_id=?", (doc_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def set_status(self, doc_id: str, status: str, by: str = "", now: datetime | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(status)
        now_s = iso(now or now_kst())
        with self._conn() as c:
            changed = c.execute(
                "UPDATE questions SET status=?, status_changed_at=?, status_by=? WHERE doc_id=?",
                (status, now_s, by or None, doc_id),
            ).rowcount > 0
            if changed:
                c.execute("INSERT INTO activity (doc_id, username, status, at) VALUES (?,?,?,?)", (doc_id, by, status, now_s))
            return changed

    def answer_stats(self, now: datetime | None = None) -> dict[str, dict[str, int]]:
        """직원별 답변완료 수 (지식iN 답변 + 유튜브·쓰레드 댓글): {username: {today, week, total}} ('' = 로그인 없이 사용)"""
        now = now or now_kst()
        today = iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
        week = iso((now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0))
        out: dict[str, dict[str, int]] = {}
        with self._conn() as c:
            # 같은 글을 여러 번 답변완료로 눌러도 한 번만 센다 (마지막 기록 기준)
            rows = c.execute(
                """SELECT a.username, a.at FROM activity a
                   WHERE a.status = 'answered'
                     AND a.id = (SELECT MAX(id) FROM activity b WHERE b.doc_id = a.doc_id AND b.status = 'answered')
                     AND (EXISTS (SELECT 1 FROM questions q WHERE q.doc_id = a.doc_id AND q.status = 'answered')
                          OR EXISTS (SELECT 1 FROM social_posts s WHERE s.post_id = a.doc_id AND s.status = 'answered'))"""
            ).fetchall()
        for r in rows:
            s = out.setdefault(r["username"], {"today": 0, "week": 0, "total": 0})
            s["total"] += 1
            if r["at"] >= week:
                s["week"] += 1
            if r["at"] >= today:
                s["today"] += 1
        return out

    # ------------------------------------------------------------ 직원 계정

    def list_users(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT username, name, active, created_at FROM users ORDER BY created_at")]

    def get_user(self, username: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def save_user(self, username: str, name: str, password_hash: str | None = None, active: bool = True) -> None:
        with self._conn() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                if password_hash:
                    c.execute("UPDATE users SET password_hash=? WHERE username=?", (password_hash, username))
                c.execute("UPDATE users SET name=?, active=? WHERE username=?", (name, int(active), username))
            else:
                if not password_hash:
                    raise ValueError("password required")
                c.execute(
                    "INSERT INTO users (username, name, password_hash, active, created_at) VALUES (?,?,?,?,?)",
                    (username, name, password_hash, int(active), iso(now_kst())),
                )

    def delete_user(self, username: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM users WHERE username=?", (username,))

    # ------------------------------------------------------------ 설정값 / 자동 검색어

    def kv_get(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def kv_update(self, key: str, fn, default=None):
        """읽고-고치고-쓰기를 한 번에 (여러 직원이 동시에 눌러도 값이 빠지지 않게)."""
        with self._lock:
            value = fn(self.kv_get(key, default))
            self.kv_set(key, value)
            return value

    def kv_incr(self, key: str, n: int = 1) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO kv (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT)""",
                (key, str(n), n),
            )

    def auto_keywords(self, product: str | None = None, enabled_only: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM auto_keywords WHERE 1=1", []
        if product:
            sql += " AND product=?"
            args.append(product)
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY product, user_set DESC, score DESC, keyword"
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["sources"] = json.loads(r["sources"] or "[]")
        return rows

    def replace_auto_keywords(self, product: str, candidates: list[dict], max_enabled: int, min_volume: int = 0) -> None:
        """새로 만든 후보로 교체. 사람이 직접 켜고/끈 검색어는 그 선택을 유지한다.

        candidates: 점수 높은 순 [{keyword, seed, sources, pc, mobile, score}]
        min_volume: 월간 검색수(PC+모바일)를 알 때, 이보다 적은 검색어는 켜지 않는다.
        """
        now_s = iso(now_kst())
        with self._conn() as c:
            kept = {
                r["keyword"]: dict(r)
                for r in c.execute("SELECT * FROM auto_keywords WHERE product=? AND user_set=1", (product,))
            }
            c.execute("DELETE FROM auto_keywords WHERE product=? AND user_set=0", (product,))
            auto_on = 0
            for cand in candidates:
                kw = cand["keyword"]
                if kw in kept:
                    c.execute(
                        "UPDATE auto_keywords SET seed=?, sources=?, pc=?, mobile=?, score=?, updated_at=? WHERE product=? AND keyword=?",
                        (cand.get("seed", ""), json.dumps(cand.get("sources", []), ensure_ascii=False), cand.get("pc"),
                         cand.get("mobile"), cand.get("score", 0), now_s, product, kw),
                    )
                    continue
                known = cand.get("pc") is not None or cand.get("mobile") is not None
                volume = (cand.get("pc") or 0) + (cand.get("mobile") or 0)
                enabled = auto_on < max_enabled and not (known and volume < min_volume)
                auto_on += int(enabled)
                c.execute(
                    """INSERT INTO auto_keywords (product, keyword, seed, sources, pc, mobile, score, enabled, user_set, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,0,?)""",
                    (product, kw, cand.get("seed", ""), json.dumps(cand.get("sources", []), ensure_ascii=False),
                     cand.get("pc"), cand.get("mobile"), cand.get("score", 0), int(enabled), now_s),
                )

    def set_auto_keyword(self, product: str, keyword: str, enabled: bool, sources: list[str] | None = None) -> None:
        """사람이 켜고/끄거나 직접 추가한 검색어 (다시 생성해도 유지)."""
        now_s = iso(now_kst())
        with self._conn() as c:
            if c.execute("SELECT 1 FROM auto_keywords WHERE product=? AND keyword=?", (product, keyword)).fetchone():
                c.execute(
                    "UPDATE auto_keywords SET enabled=?, user_set=1, updated_at=? WHERE product=? AND keyword=?",
                    (int(enabled), now_s, product, keyword),
                )
            else:
                c.execute(
                    """INSERT INTO auto_keywords (product, keyword, sources, score, enabled, user_set, updated_at)
                       VALUES (?,?,?,0,?,1,?)""",
                    (product, keyword, json.dumps(sources or ["직접"], ensure_ascii=False), int(enabled), now_s),
                )

    def exposure_counts(self) -> dict[tuple[str, str], int]:
        """(제품, 검색어) → 최근 확인에서 노출된 지식iN 글 수 (성공한 확인만)."""
        out: dict[tuple[str, str], int] = {}
        for g in self.latest_exposures():
            if any(not s["error"] for s in g["sources"].values()):
                out[(g["product"], g["keyword"])] = len(g["posts"])
        return out

    def set_draft(self, doc_id: str, draft: str, edited: bool = False) -> None:
        """AI 가 새로 쓴 초안(edited=False) 또는 직원이 고친 초안(edited=True) 저장."""
        with self._conn() as c:
            c.execute(
                "UPDATE questions SET draft=?, draft_at=?, draft_edited=? WHERE doc_id=?",
                (draft, iso(now_kst()), int(edited), doc_id),
            )

    def save_draft_edit(self, doc_id: str, text: str) -> bool:
        """직원이 초안 칸에서 고친 내용. 내용이 바뀐 경우에만 '고침'으로 표시한다."""
        text = (text or "").strip()
        with self._conn() as c:
            row = c.execute("SELECT draft, draft_edited FROM questions WHERE doc_id=?", (doc_id,)).fetchone()
            if row is None:
                return False
            if text and text != (row["draft"] or "").strip():
                c.execute(
                    "UPDATE questions SET draft=?, draft_at=?, draft_edited=1 WHERE doc_id=?", (text, iso(now_kst()), doc_id)
                )
            return True

    # ------------------------------------------------------------ 모범 답변 예시

    def capture_final_answer(self, doc_id: str, product: str, by: str = "") -> int | None:
        """답변완료한 질문의 초안(직원이 고친 최종본)을 예시 후보로 남긴다. 이미 있으면 내용만 갱신."""
        q = self.get(doc_id)
        answer = ((q or {}).get("draft") or "").strip()
        if not q or not answer:
            return None
        question = (q.get("body") or q.get("snippet") or "")[:1000]
        now_s = iso(now_kst())
        with self._conn() as c:
            row = c.execute("SELECT id FROM answer_examples WHERE doc_id=?", (doc_id,)).fetchone()
            if row:
                c.execute(
                    "UPDATE answer_examples SET answer=?, edited=?, created_by=?, created_at=? WHERE id=?",
                    (answer, q.get("draft_edited") or 0, by, now_s, row["id"]),
                )
                return row["id"]
            return c.execute(
                """INSERT INTO answer_examples (product, doc_id, title, question, answer, source, edited, created_by, created_at)
                   VALUES (?,?,?,?,?,'final',?,?,?)""",
                (product, doc_id, q.get("title") or "", question, answer, q.get("draft_edited") or 0, by, now_s),
            ).lastrowid

    def drop_final_answer(self, doc_id: str) -> None:
        """답변완료를 취소하면 ⭐ 안 한 예시 후보도 지운다."""
        with self._conn() as c:
            c.execute("DELETE FROM answer_examples WHERE doc_id=? AND starred=0", (doc_id,))

    def add_example(self, product: str, answer: str, title: str = "", question: str = "", by: str = "", starred: bool = True) -> int:
        now_s = iso(now_kst())
        with self._conn() as c:
            return c.execute(
                """INSERT INTO answer_examples (product, title, question, answer, source, starred, created_by, created_at, starred_at)
                   VALUES (?,?,?,?,'manual',?,?,?,?)""",
                (product, title.strip(), question.strip(), answer.strip(), int(starred), by, now_s, now_s if starred else None),
            ).lastrowid

    def get_example(self, example_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM answer_examples WHERE id=?", (example_id,)).fetchone()
        return dict(row) if row else None

    def list_examples(self, product: str | None = None) -> list[dict]:
        sql = "SELECT e.*, q.url AS url FROM answer_examples e LEFT JOIN questions q ON q.doc_id = e.doc_id"
        args: tuple = ()
        if product:
            sql += " WHERE e.product=?"
            args = (product,)
        sql += " ORDER BY e.starred DESC, COALESCE(e.starred_at, e.created_at) DESC, e.id DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def starred_count(self, product: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM answer_examples WHERE product=? AND starred=1", (product,)).fetchone()[0]

    def starred_examples(self, product: str, limit: int) -> list[dict]:
        """AI 초안에 넣을 예시. 순서를 고정해(오래된 것부터) 같은 제품이면 프롬프트가 같도록 한다 (캐시 적중)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM (SELECT * FROM answer_examples WHERE product=? AND starred=1
                   ORDER BY starred_at DESC, id DESC LIMIT ?) ORDER BY id""",
                (product, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_example_star(self, example_id: int, starred: bool) -> bool:
        with self._conn() as c:
            return c.execute(
                "UPDATE answer_examples SET starred=?, starred_at=? WHERE id=?",
                (int(starred), iso(now_kst()) if starred else None, example_id),
            ).rowcount > 0

    def update_example(self, example_id: int, answer: str, title: str | None = None, question: str | None = None) -> bool:
        fields, args = ["answer=?"], [answer.strip()]
        if title is not None:
            fields.append("title=?")
            args.append(title.strip())
        if question is not None:
            fields.append("question=?")
            args.append(question.strip())
        with self._conn() as c:
            return c.execute(f"UPDATE answer_examples SET {', '.join(fields)} WHERE id=?", (*args, example_id)).rowcount > 0

    def delete_example(self, example_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM answer_examples WHERE id=?", (example_id,)).rowcount > 0

    def list_questions(
        self,
        product: str | None = None,
        category: str | None = None,
        status: str = "todo",
        unanswered: bool = False,
        include_low: bool = False,
        max_age_days: int | None = None,
        query: str = "",
        sort: str = "priority",
        limit: int = 300,
        now: datetime | None = None,
    ) -> list[dict]:
        now = now or now_kst()
        where, args = ["in_feed=1"], []
        if product:
            if include_low:
                where.append("(product=? OR matches LIKE ?)")
                args += [product, f'%"product_id": "{product}"%']
            else:
                where.append("product=?")
                args.append(product)
        elif include_low:
            where.append("(product IS NOT NULL OR score > 0)")  # 점수 0 (완전 무관) 은 제외
        else:
            where.append("product IS NOT NULL")
        if category:
            where.append("categories LIKE ?")
            args.append("%" + json.dumps(category, ensure_ascii=False) + "%")
        if status == "todo":
            where.append("status IN ('new','opened')")
        elif status in STATUSES:
            where.append("status=?")
            args.append(status)
        if unanswered:
            where.append("(answer_count IS NULL OR answer_count=0)")
        if max_age_days and status == "todo":
            where.append("(asked_at IS NULL OR asked_at >= ?)")
            args.append(iso(now - timedelta(days=max_age_days)))
        if query:
            where.append("(title LIKE ? OR body LIKE ? OR snippet LIKE ?)")
            args += [f"%{query}%"] * 3
        sql = "SELECT * FROM questions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CAST(doc_id AS INTEGER) DESC LIMIT 2000"
        with self._conn() as c:
            rows = [_row_to_dict(r) for r in c.execute(sql, args).fetchall()]
        for r in rows:
            r["priority"] = priority(r, now)
        if sort == "priority":
            rows.sort(key=lambda r: (r["priority"], int(r["doc_id"])), reverse=True)
        elif sort == "status":
            rows.sort(key=lambda r: r.get("status_changed_at") or "", reverse=True)
        return rows[:limit]

    def todo_counts(self, max_age_days: int | None = None, now: datetime | None = None) -> dict:
        """제품별 / 카테고리별 할 일 개수."""
        now = now or now_kst()
        sql = (
            "SELECT product, categories, status FROM questions "
            "WHERE product IS NOT NULL AND in_feed=1 AND status IN ('new','opened')"
        )
        args: list = []
        if max_age_days:
            sql += " AND (asked_at IS NULL OR asked_at >= ?)"
            args.append(iso(now - timedelta(days=max_age_days)))
        products: dict[str, dict] = {}
        with self._conn() as c:
            for r in c.execute(sql, args):
                p = products.setdefault(r["product"], {"total": 0, "new": 0, "categories": {}})
                p["total"] += 1
                if r["status"] == "new":
                    p["new"] += 1
                for cat in json.loads(r["categories"] or "[]"):
                    p["categories"][cat] = p["categories"].get(cat, 0) + 1
            answered_today = c.execute(
                "SELECT COUNT(*) FROM questions WHERE status='answered' AND status_changed_at >= ?",
                (iso(now.replace(hour=0, minute=0, second=0, microsecond=0)),),
            ).fetchone()[0]
        return {"products": products, "answered_today": answered_today}

    # ------------------------------------------------------------ 수집 기록

    def start_run(self, mode: str) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO runs (started_at, mode) VALUES (?,?)", (iso(now_kst()), mode)).lastrowid

    def finish_run(self, run_id: int, **fields) -> None:
        fields = dict(fields)
        if "errors" in fields:
            fields["errors"] = json.dumps(fields["errors"], ensure_ascii=False)
        fields["finished_at"] = iso(now_kst())
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def recent_runs(
        self, limit: int = 10, mode: str | None = None, exclude_mode: str | tuple[str, ...] | None = None
    ) -> list[dict]:
        sql, args = "SELECT * FROM runs", []
        if mode:
            sql += " WHERE mode=?"
            args.append(mode)
        elif exclude_mode:
            excluded = (exclude_mode,) if isinstance(exclude_mode, str) else tuple(exclude_mode)
            sql += f" WHERE mode IS NULL OR mode NOT IN ({','.join('?' * len(excluded))})"
            args.extend(excluded)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["errors"] = json.loads(d.get("errors") or "[]")
            out.append(d)
        return out

    # ------------------------------------------------------------ 상위노출

    def record_exposure(
        self, product: str, keyword: str, source: str, doc_ids: list[str],
        error: str | None = None, now: datetime | None = None,
    ) -> int:
        with self._conn() as c:
            check_id = c.execute(
                "INSERT INTO exposure_checks (product, keyword, source, checked_at, error) VALUES (?,?,?,?,?)",
                (product, keyword, source, iso(now or now_kst()), error),
            ).lastrowid
            for rank, doc_id in enumerate(dict.fromkeys(doc_ids), start=1):
                c.execute("INSERT INTO exposure_ranks (check_id, doc_id, rank) VALUES (?,?,?)", (check_id, doc_id, rank))
            return check_id

    def last_exposure_time(self) -> datetime | None:
        with self._conn() as c:
            row = c.execute("SELECT MAX(checked_at) FROM exposure_checks").fetchone()
        return from_iso(row[0]) if row else None

    def exposure_product(self, doc_id: str) -> str | None:
        """이 글이 최근 어느 제품 검색어에서 노출됐는지."""
        with self._conn() as c:
            row = c.execute(
                """SELECT ec.product FROM exposure_ranks er JOIN exposure_checks ec ON ec.id = er.check_id
                   WHERE er.doc_id=? ORDER BY ec.id DESC LIMIT 1""",
                (doc_id,),
            ).fetchone()
        return row[0] if row else None

    def stale_details(self, doc_ids: list[str], older_than_hours: float, limit: int, now: datetime | None = None) -> list[sqlite3.Row]:
        """상세 정보(조회수 등)를 새로 읽어야 하는 글. doc_ids 순서(=노출 순위 우선)를 유지한다."""
        cutoff = iso((now or now_kst()) - timedelta(hours=older_than_hours))
        out = []
        with self._conn() as c:
            for doc_id in dict.fromkeys(doc_ids):
                row = c.execute(
                    "SELECT doc_id, url FROM questions WHERE doc_id=? AND (detail_fetched_at IS NULL OR detail_fetched_at < ?)",
                    (doc_id, cutoff),
                ).fetchone()
                if row:
                    out.append(row)
                if len(out) >= limit:
                    break
        return out

    def latest_exposures(self, product: str | None = None, now: datetime | None = None) -> list[dict]:
        """검색어별 최신 상위노출 현황.

        [{product, keyword, checked_at, sources: {pc: {checked_at, error, count}}, posts: [...]}]
        posts 에는 질문 정보 + ranks{source: 순위} + prev_ranks + views_per_day 가 들어간다.
        """
        now = now or now_kst()
        sql = "SELECT * FROM exposure_checks"
        args: list = []
        if product:
            sql += " WHERE product=?"
            args.append(product)
        sql += " ORDER BY id DESC LIMIT 5000"
        with self._conn() as c:
            checks = [dict(r) for r in c.execute(sql, args).fetchall()]
            # (product, keyword, source) 마다: 가장 최근 확인(오류 포함) + 성공한 최근 2회
            latest_any: dict[tuple, dict] = {}
            latest: dict[tuple, list[dict]] = {}
            for ch in checks:
                key = (ch["product"], ch["keyword"], ch["source"])
                latest_any.setdefault(key, ch)
                bucket = latest.setdefault(key, [])
                if len(bucket) < 2 and not ch["error"]:
                    bucket.append(ch)
            check_ids = [ch["id"] for b in latest.values() for ch in b]
            ranks: dict[int, dict[str, int]] = {}
            for i in range(0, len(check_ids), 500):
                chunk = check_ids[i : i + 500]
                for r in c.execute(
                    f"SELECT check_id, doc_id, rank FROM exposure_ranks WHERE check_id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ):
                    ranks.setdefault(r["check_id"], {})[r["doc_id"]] = r["rank"]

            groups: dict[tuple, dict] = {}
            for (prod, kw, src), bucket in latest.items():
                g = groups.setdefault((prod, kw), {"product": prod, "keyword": kw, "checked_at": None, "sources": {}, "_ranks": {}})
                last = latest_any[(prod, kw, src)]
                cur = bucket[0] if bucket else None
                prev = bucket[1] if len(bucket) > 1 else None
                cur_ranks = ranks.get(cur["id"], {}) if cur else {}
                g["sources"][src] = {"checked_at": last["checked_at"], "error": last["error"], "count": len(cur_ranks)}
                g["checked_at"] = max(filter(None, [g["checked_at"], last["checked_at"]]))
                prev_ranks = ranks.get(prev["id"], {}) if prev else {}
                for doc_id, rank in cur_ranks.items():
                    entry = g["_ranks"].setdefault(doc_id, {"ranks": {}, "prev_ranks": {}})
                    entry["ranks"][src] = rank
                    if prev:
                        entry["prev_ranks"][src] = prev_ranks.get(doc_id)  # None = 이번에 새로 진입

            doc_ids = list({d for g in groups.values() for d in g["_ranks"]})
            questions: dict[str, dict] = {}
            for i in range(0, len(doc_ids), 500):
                chunk = doc_ids[i : i + 500]
                for r in c.execute(f"SELECT * FROM questions WHERE doc_id IN ({','.join('?' * len(chunk))})", chunk):
                    questions[r["doc_id"]] = _row_to_dict(r)
            history: dict[str, list[tuple[str, int]]] = {}
            since = iso(now - timedelta(days=7))
            for i in range(0, len(doc_ids), 500):
                chunk = doc_ids[i : i + 500]
                for r in c.execute(
                    f"""SELECT doc_id, checked_at, views FROM views_history
                        WHERE checked_at >= ? AND doc_id IN ({','.join('?' * len(chunk))}) ORDER BY checked_at""",
                    [since, *chunk],
                ):
                    history.setdefault(r["doc_id"], []).append((r["checked_at"], r["views"]))

        out = []
        for g in groups.values():
            posts = []
            for doc_id, entry in g.pop("_ranks").items():
                q = questions.get(doc_id)
                if not q:
                    continue
                q = dict(q)
                q.update(entry)
                q["best_rank"] = min(entry["ranks"].values())
                q["views_per_day"], q["views_per_day_kind"] = views_per_day(q, history.get(doc_id, []), now)
                posts.append(q)
            # 최고 순위 → 여러 곳에 노출될수록 → 하루 조회수 많을수록
            posts.sort(key=lambda p: (p["best_rank"], -len(p["ranks"]), -(p["views_per_day"] or 0)))
            g["posts"] = posts
            out.append(g)
        return out

    # ------------------------------------------------------------ 유튜브 · 쓰레드

    def upsert_social(self, post, query: str, now: datetime | None = None) -> bool:
        """영상/글을 저장한다 (post: social.SocialPost). 처음 본 글이면 True. 다시 보면 조회수 등만 갱신."""
        now_s = iso(now or now_kst())
        with self._conn() as c:
            row = c.execute("SELECT queries FROM social_posts WHERE post_id=?", (post.post_id,)).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO social_posts (post_id, platform, url, title, body, author, author_url, thumbnail,
                                                 published_at, views, likes, comments, duration, is_short,
                                                 queries, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        post.post_id, post.platform, post.url, post.title, post.body, post.author, post.author_url,
                        post.thumbnail, iso(post.published_at), post.views, post.likes, post.comments,
                        post.duration, _bool_int(post.is_short),
                        json.dumps([query], ensure_ascii=False), now_s, now_s,
                    ),
                )
                return True
            queries = json.loads(row["queries"] or "[]")
            if query not in queries:
                queries.append(query)
            c.execute(
                """UPDATE social_posts SET last_seen=?, queries=?,
                          body=CASE WHEN length(?) > length(body) THEN ? ELSE body END,
                          views=COALESCE(?, views), likes=COALESCE(?, likes), comments=COALESCE(?, comments),
                          duration=COALESCE(?, duration), is_short=COALESCE(?, is_short),
                          url=CASE WHEN ? THEN ? ELSE url END
                   WHERE post_id=?""",
                (
                    now_s, json.dumps(queries[-20:], ensure_ascii=False), post.body, post.body,
                    post.views, post.likes, post.comments, post.duration, _bool_int(post.is_short),
                    post.is_short is not None, post.url, post.post_id,
                ),
            )
            return False

    def classify_social(self, matcher: Matcher, post_ids: list[str] | None = None) -> int:
        relevant = 0
        with self._conn() as c:
            if post_ids is None:
                rows = c.execute("SELECT post_id, title, body FROM social_posts").fetchall()
            else:
                rows = []
                ids = list(dict.fromkeys(post_ids))
                for i in range(0, len(ids), 500):
                    chunk = ids[i : i + 500]
                    rows += c.execute(
                        f"SELECT post_id, title, body FROM social_posts WHERE post_id IN ({','.join('?' * len(chunk))})",
                        chunk,
                    ).fetchall()
            for r in rows:
                # 쓰레드 글은 제목이 없으므로 첫 줄을 제목처럼 본다
                title, body = r["title"], r["body"] or ""
                if not title:
                    title, _, body = body.partition("\n")
                matches = matcher.classify(title, body)
                best = matcher.best(matches)
                if best:
                    relevant += 1
                c.execute(
                    "UPDATE social_posts SET product=?, score=?, categories=?, matches=? WHERE post_id=?",
                    (
                        best.product_id if best else None,
                        best.score if best else (matches[0].score if matches else 0),
                        json.dumps(best.categories if best else [], ensure_ascii=False),
                        json.dumps([m.to_dict() for m in matches], ensure_ascii=False),
                        r["post_id"],
                    ),
                )
        return relevant

    def get_social(self, post_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM social_posts WHERE post_id=?", (post_id,)).fetchone()
        return _row_to_dict(row, ("queries", "categories", "matches")) if row else None

    def set_social_status(self, post_id: str, status: str, by: str = "", now: datetime | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(status)
        now_s = iso(now or now_kst())
        with self._conn() as c:
            changed = c.execute(
                "UPDATE social_posts SET status=?, status_changed_at=?, status_by=? WHERE post_id=?",
                (status, now_s, by or None, post_id),
            ).rowcount > 0
            if changed:
                c.execute("INSERT INTO activity (doc_id, username, status, at) VALUES (?,?,?,?)", (post_id, by, status, now_s))
            return changed

    def set_social_draft(self, post_id: str, draft: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE social_posts SET draft=?, draft_at=?, draft_edited=0 WHERE post_id=?", (draft, iso(now_kst()), post_id)
            )

    def save_social_draft_edit(self, post_id: str, text: str) -> bool:
        text = (text or "").strip()
        with self._conn() as c:
            row = c.execute("SELECT draft FROM social_posts WHERE post_id=?", (post_id,)).fetchone()
            if row is None:
                return False
            if text and text != (row["draft"] or "").strip():
                c.execute(
                    "UPDATE social_posts SET draft=?, draft_at=?, draft_edited=1 WHERE post_id=?",
                    (text, iso(now_kst()), post_id),
                )
            return True

    def list_social(
        self,
        platform: str,
        product: str | None = None,
        category: str | None = None,
        status: str = "todo",
        include_low: bool = False,
        max_age_days: int | None = None,
        query: str = "",
        sort: str = "priority",
        shorts: str = "",
        limit: int = 300,
        now: datetime | None = None,
    ) -> list[dict]:
        """shorts: "only" 숏츠만 / "exclude" 일반 영상만 / 그 외 전체."""
        now = now or now_kst()
        where, args = ["platform=?"], [platform]
        if shorts == "only":
            where.append("is_short=1")
        elif shorts == "exclude":
            where.append("(is_short IS NULL OR is_short=0)")
        if product:
            if include_low:
                where.append("(product=? OR matches LIKE ?)")
                args += [product, f'%"product_id": "{product}"%']
            else:
                where.append("product=?")
                args.append(product)
        elif not include_low:
            where.append("product IS NOT NULL")
        if category:
            where.append("categories LIKE ?")
            args.append("%" + json.dumps(category, ensure_ascii=False) + "%")
        if status == "todo":
            where.append("status IN ('new','opened')")
        elif status in STATUSES:
            where.append("status=?")
            args.append(status)
        if max_age_days and status == "todo":
            where.append("(published_at IS NULL OR published_at >= ?)")
            args.append(iso(now - timedelta(days=max_age_days)))
        if query:
            where.append("(title LIKE ? OR body LIKE ? OR author LIKE ?)")
            args += [f"%{query}%"] * 3
        sql = "SELECT * FROM social_posts WHERE " + " AND ".join(where) + " ORDER BY published_at DESC LIMIT 2000"
        with self._conn() as c:
            rows = [_row_to_dict(r, ("queries", "categories", "matches")) for r in c.execute(sql, args).fetchall()]
        for r in rows:
            r["priority"] = social_priority(r, now)
        if sort == "priority":
            rows.sort(key=lambda r: (r["priority"], r.get("published_at") or ""), reverse=True)
        elif sort == "views":
            rows.sort(key=lambda r: (r.get("views") or -1, r.get("published_at") or ""), reverse=True)
        elif sort == "status":
            rows.sort(key=lambda r: r.get("status_changed_at") or "", reverse=True)
        return rows[:limit]

    def social_counts(self, max_age_days: int | dict[str, int] | None = None, now: datetime | None = None) -> dict:
        """{platform: {product: {total, new, categories}}} — 할 일(new/opened)만.

        max_age_days 는 모든 플랫폼 공통 일수 또는 {platform: 일수}."""
        now = now or now_kst()
        sql = "SELECT platform, product, categories, status, published_at FROM social_posts WHERE product IS NOT NULL AND status IN ('new','opened')"
        ages = max_age_days if isinstance(max_age_days, dict) else {}
        out: dict[str, dict] = {"youtube": {}, "threads": {}}
        with self._conn() as c:
            for r in c.execute(sql):
                days = ages.get(r["platform"]) if ages else max_age_days
                if days and r["published_at"] and r["published_at"] < iso(now - timedelta(days=days)):
                    continue
                p = out.setdefault(r["platform"], {}).setdefault(r["product"], {"total": 0, "new": 0, "categories": {}})
                p["total"] += 1
                if r["status"] == "new":
                    p["new"] += 1
                for cat in json.loads(r["categories"] or "[]"):
                    p["categories"][cat] = p["categories"].get(cat, 0) + 1
        return out

    def purge_social(self, keep_days: int, now: datetime | None = None) -> int:
        """오래된 미처리/무관 글 정리. 댓글완료는 보관."""
        now = now or now_kst()
        old = iso(now - timedelta(days=keep_days))
        old_irrelevant = iso(now - timedelta(days=min(7, keep_days)))
        with self._conn() as c:
            n = c.execute("DELETE FROM social_posts WHERE status<>'answered' AND last_seen < ?", (old,)).rowcount
            n += c.execute(
                "DELETE FROM social_posts WHERE product IS NULL AND status='new' AND last_seen < ?", (old_irrelevant,)
            ).rowcount
            c.execute("DELETE FROM kv WHERE key LIKE 'yt_units:%' AND key < ?", (f"yt_units:{now - timedelta(days=40):%Y-%m-%d}",))
        return n


class ApiBudget:
    """네이버 검색 API 하루 호출 수를 세고, 상한에 닿으면 더 부르지 않게 막는다.

    네이버 무료 한도(하루 25,000회)를 넘으면 호출이 거부되고, 앞으로 초과분이 유료가 될 수 있어
    그보다 낮은 상한(설정 api_daily_limit)을 둔다. 날짜는 한국 시간 기준, 기록은 DB 에 남아 재시작해도 이어진다.
    """

    def __init__(self, store: Store, daily_limit: int):
        self.store = store
        self.daily_limit = daily_limit

    def _key(self) -> str:
        return f"api_calls:{now_kst():%Y-%m-%d}"

    def used(self) -> int:
        return int(self.store.kv_get(self._key(), 0) or 0)

    def check(self) -> None:
        if self.daily_limit > 0 and self.used() >= self.daily_limit:
            raise NaverError(
                f"오늘 네이버 API 호출 상한({self.daily_limit:,}회)에 도달해 API 검색을 멈췄습니다. "
                "밤 12시(한국 시간)에 다시 시작합니다.",
                fatal=True,
                budget=True,
            )

    def record(self, n: int = 1) -> None:
        self.store.kv_incr(self._key(), n)


def views_per_day(q: dict, history: list[tuple[str, int]], now: datetime) -> tuple[float | None, str]:
    """하루 조회수 증가량. 최근 기록이 6시간 이상 쌓였으면 'recent', 아니면 작성일 이후 평균 'lifetime'."""
    if len(history) >= 2:
        t0, v0 = from_iso(history[0][0]), history[0][1]
        t1, v1 = from_iso(history[-1][0]), history[-1][1]
        if t0 and t1 and (t1 - t0).total_seconds() >= 6 * 3600:
            days = (t1 - t0).total_seconds() / 86400
            return round(max(0, v1 - v0) / days, 1), "recent"
    views = q.get("views")
    asked = from_iso(q.get("asked_at"))
    if views is not None and asked:
        days = max(1.0, (now - asked).total_seconds() / 86400)
        return round(views / days, 1), "lifetime"
    return None, ""


def _bool_int(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _migrate(c: sqlite3.Connection) -> None:
    """이전 버전 DB 에 새 열을 추가한다."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(questions)")}
    if not cols:
        return  # 새 DB
    if "views" not in cols:
        c.execute("ALTER TABLE questions ADD COLUMN views INTEGER")
    if "in_feed" not in cols:
        c.execute("ALTER TABLE questions ADD COLUMN in_feed INTEGER NOT NULL DEFAULT 1")
    if "status_by" not in cols:
        c.execute("ALTER TABLE questions ADD COLUMN status_by TEXT")
    if "draft_edited" not in cols:
        c.execute("ALTER TABLE questions ADD COLUMN draft_edited INTEGER NOT NULL DEFAULT 0")
    social_cols = {r[1] for r in c.execute("PRAGMA table_info(social_posts)")}
    if social_cols and "is_short" not in social_cols:
        c.execute("ALTER TABLE social_posts ADD COLUMN duration INTEGER")
        c.execute("ALTER TABLE social_posts ADD COLUMN is_short INTEGER")


def _row_to_dict(row: sqlite3.Row, json_keys: tuple[str, ...] = ("sources", "categories", "matches")) -> dict:
    d = dict(row)
    for key in json_keys:
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except ValueError:
            d[key] = []
    return d


def priority(q: dict, now: datetime) -> float:
    """추천순 정렬 점수: 관련도 + 답변이 적을수록 + 최근일수록 + 내공."""
    p = float(q.get("score") or 0)
    ac = q.get("answer_count")
    if ac is None:
        p += 1
    elif ac == 0:
        p += 4
    elif ac == 1:
        p += 2
    elif ac == 2:
        p += 1
    elif ac >= 5:
        p -= 1
    asked = from_iso(q.get("asked_at"))
    if asked:
        age_h = (now - asked).total_seconds() / 3600
        if age_h < 1:
            p += 3
        elif age_h < 6:
            p += 2
        elif age_h < 24:
            p += 1
        elif age_h > 24 * 7:
            p -= 1
    else:
        # 작성일을 아직 모르면(상세 미수집) 처음 본 시각으로 약하게만 가산
        # — 첫 수집 때 한꺼번에 들어온 옛 질문이 맨 위로 올라오지 않게
        seen = from_iso(q.get("first_seen"))
        if seen and (now - seen).total_seconds() < 6 * 3600:
            p += 1
    if (q.get("reward") or 0) > 0:
        p += 1
    return p


def social_priority(post: dict, now: datetime) -> float:
    """추천순: 관련도 + 최근 글일수록 + (유튜브) 조회수가 많을수록 댓글이 많이 읽힌다."""
    p = float(post.get("score") or 0)
    published = from_iso(post.get("published_at"))
    if published:
        age_h = (now - published).total_seconds() / 3600
        if age_h < 6:
            p += 3
        elif age_h < 24:
            p += 2
        elif age_h < 72:
            p += 1
        elif age_h > 24 * 7:
            p -= 1
    views = post.get("views")
    if views:
        p += min(2.0, math.log10(max(views, 1)) / 2)  # 100회 +1, 1만 회 이상 +2
    comments = post.get("comments")
    if comments is not None and comments < 5:
        p += 1  # 댓글이 적은 영상은 내 댓글이 위에 보이기 쉬움
    return p
