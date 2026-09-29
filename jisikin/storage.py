"""수집한 질문을 SQLite 에 저장한다."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .matcher import Matcher
from .naver import KST, QuestionDetail, RawQuestion

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
    draft_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_q_product ON questions(product, status);
CREATE INDEX IF NOT EXISTS idx_q_first_seen ON questions(first_seen);

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

    def upsert_raw(self, rq: RawQuestion, source: str, now: datetime | None = None) -> bool:
        """질문을 저장한다. 처음 본 질문이면 True."""
        now_s = iso(now or now_kst())
        with self._conn() as c:
            row = c.execute("SELECT sources, snippet, answer_count, asked_at FROM questions WHERE doc_id=?", (rq.doc_id,)).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO questions (doc_id, url, title, snippet, dir_name, answer_count, asked_at,
                                              sources, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rq.doc_id, rq.url, rq.title, rq.snippet, rq.dir_name, rq.answer_count,
                        iso(rq.asked_at), json.dumps([source], ensure_ascii=False), now_s, now_s,
                    ),
                )
                return True
            sources = json.loads(row["sources"] or "[]")
            if source not in sources:
                sources.append(source)
            c.execute(
                """UPDATE questions SET last_seen=?, sources=?,
                          snippet=CASE WHEN length(?) > length(snippet) THEN ? ELSE snippet END,
                          answer_count=COALESCE(?, answer_count),
                          asked_at=COALESCE(asked_at, ?)
                   WHERE doc_id=?""",
                (
                    now_s, json.dumps(sources[-20:], ensure_ascii=False), rq.snippet, rq.snippet,
                    rq.answer_count, iso(rq.asked_at), rq.doc_id,
                ),
            )
            return False

    def update_detail(self, doc_id: str, d: QuestionDetail, now: datetime | None = None) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE questions SET
                          title=CASE WHEN ?<>'' THEN ? ELSE title END,
                          body=CASE WHEN ?<>'' THEN ? ELSE body END,
                          answer_count=COALESCE(?, answer_count),
                          asked_at=COALESCE(?, asked_at),
                          reward=COALESCE(?, reward),
                          detail_fetched_at=?, detail_error=NULL
                   WHERE doc_id=?""",
                (
                    d.title, d.title, d.body, d.body, d.answer_count, iso(d.asked_at), d.reward,
                    iso(now or now_kst()), doc_id,
                ),
            )

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
                   WHERE product IS NOT NULL AND detail_fetched_at IS NULL
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
        return n

    # ------------------------------------------------------------ 화면용

    def get(self, doc_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM questions WHERE doc_id=?", (doc_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def set_status(self, doc_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(status)
        with self._conn() as c:
            return c.execute(
                "UPDATE questions SET status=?, status_changed_at=? WHERE doc_id=?",
                (status, iso(now_kst()), doc_id),
            ).rowcount > 0

    def set_draft(self, doc_id: str, draft: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE questions SET draft=?, draft_at=? WHERE doc_id=?", (draft, iso(now_kst()), doc_id))

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
        where, args = [], []
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
        sql = "SELECT product, categories, status FROM questions WHERE product IS NOT NULL AND status IN ('new','opened')"
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

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["errors"] = json.loads(d.get("errors") or "[]")
            out.append(d)
        return out


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("sources", "categories", "matches"):
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
