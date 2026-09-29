"""로컬 대시보드(Flask) + 자동 수집 스케줄러."""
from __future__ import annotations

import secrets
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from . import config as config_mod
from .collector import collect, effective_interval, estimate_api_calls_per_day, resolve_mode
from .config import AppConfig, ConfigError
from .drafter import DraftError, ai_status, generate_draft
from .matcher import Matcher
from .storage import STATUSES, Store, iso, now_kst

API_DAILY_LIMIT = 25000


class AppState:
    def __init__(self, config_path: Path, db_path: Path | str):
        self.config_path = Path(config_path)
        self.cfg: AppConfig = config_mod.load_config(self.config_path)
        self.store = Store(db_path)
        self.running = False
        self.next_run_at: datetime | None = None
        self.logs: deque[str] = deque(maxlen=300)
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._force = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 설정이 바뀌었을 수 있으니 시작할 때 한 번 전체 재분류
        self.store.classify(Matcher(self.cfg.products))

    def log(self, msg: str) -> None:
        line = f"{now_kst():%H:%M:%S} {msg}"
        self.logs.append(line)
        print(line, flush=True)

    # --- 설정
    def save_config(self, text: str) -> AppConfig:
        cfg = config_mod.save_config_text(text, self.config_path)
        self.cfg = cfg
        n = self.store.classify(Matcher(cfg.products))
        self.log(f"설정 저장 — 전체 재분류 완료 (관련 질문 {n}건)")
        self._wake.set()  # 수집 주기 변경 반영
        return cfg

    # --- 수집
    def run_collection(self):
        if not self._run_lock.acquire(blocking=False):
            return None
        self.running = True
        try:
            self.log("수집 시작")
            summary = collect(self.cfg, self.store, log=self.log)
            self.log("수집 완료 — " + summary.text())
            return summary
        except Exception:
            self.log("수집 중 오류:\n" + traceback.format_exc())
            return None
        finally:
            self.running = False
            self._run_lock.release()

    def trigger(self) -> bool:
        if self.running:
            return False
        if self._thread and self._thread.is_alive():
            self._force = True
            self._wake.set()
        else:
            threading.Thread(target=self.run_collection, daemon=True).start()
        return True

    def start_scheduler(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = effective_interval(self.cfg)
            now = now_kst()
            due = interval > 0 and (self.next_run_at is None or now >= self.next_run_at)
            if self._force or due:
                self._force = False
                self.run_collection()
                self.next_run_at = now_kst() + timedelta(minutes=interval) if interval > 0 else None
            elif interval <= 0:
                self.next_run_at = None
            elif self.next_run_at and self.next_run_at > now + timedelta(minutes=interval):
                self.next_run_at = now + timedelta(minutes=interval)  # 주기를 줄인 경우
            wait = 60.0
            if self.next_run_at:
                wait = max(1.0, min(wait, (self.next_run_at - now_kst()).total_seconds()))
            self._wake.wait(timeout=wait)
            self._wake.clear()


def create_app(state: AppState) -> Flask:
    app = Flask(__name__)
    app.json.ensure_ascii = False
    csrf_token = secrets.token_urlsafe(16)

    def require_api_header():
        # 다른 웹사이트가 이 로컬 서버로 요청을 보내지 못하게 (사용자 정의 헤더는 교차 출처에서 보낼 수 없음)
        if request.headers.get("X-Jisikin") != "1":
            abort(403)

    def product_payload(cfg: AppConfig) -> list[dict]:
        return [
            {"id": p.id, "name": p.name, "color": p.color, "url": p.url, "categories": [c.name for c in p.categories]}
            for p in cfg.products
        ]

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/meta")
    def api_meta():
        cfg = state.cfg
        ok, reason = ai_status()
        runs = state.store.recent_runs(1)
        return jsonify(
            products=product_payload(cfg),
            counts=state.store.todo_counts(cfg.settings.max_age_days),
            running=state.running,
            last_run=runs[0] if runs else None,
            next_run_at=iso(state.next_run_at),
            interval=effective_interval(cfg),
            mode=resolve_mode(cfg),
            ai={"enabled": ok, "reason": reason},
        )

    @app.get("/api/questions")
    def api_questions():
        a = request.args
        rows = state.store.list_questions(
            product=a.get("product") or None,
            category=a.get("category") or None,
            status=a.get("status", "todo"),
            unanswered=a.get("unanswered") == "1",
            include_low=a.get("include_low") == "1",
            max_age_days=state.cfg.settings.max_age_days,
            query=(a.get("q") or "").strip(),
            sort=a.get("sort", "priority"),
        )
        keep = (
            "doc_id url title snippet body answer_count reward asked_at first_seen product score "
            "categories matches status draft priority detail_fetched_at"
        ).split()
        items = []
        for r in rows:
            item = {k: r.get(k) for k in keep}
            item["body"] = (r.get("body") or "")[:600]
            items.append(item)
        return jsonify(items=items)

    @app.post("/api/questions/<doc_id>/status")
    def api_set_status(doc_id: str):
        require_api_header()
        status = (request.get_json(silent=True) or {}).get("status")
        if status not in STATUSES:
            return jsonify(error="잘못된 상태"), 400
        if not state.store.set_status(doc_id, status):
            return jsonify(error="질문을 찾을 수 없습니다"), 404
        return jsonify(ok=True)

    @app.post("/api/questions/<doc_id>/draft")
    def api_draft(doc_id: str):
        require_api_header()
        q = state.store.get(doc_id)
        if not q:
            return jsonify(error="질문을 찾을 수 없습니다"), 404
        cfg = state.cfg
        product = cfg.product(q.get("product"))
        if product is None and q.get("matches"):
            product = cfg.product(q["matches"][0].get("product_id"))
        if product is None:
            return jsonify(error="어느 제품과 관련된 질문인지 알 수 없습니다"), 400
        try:
            draft = generate_draft(cfg, product, q)
        except DraftError as e:
            return jsonify(error=str(e)), 400
        state.store.set_draft(doc_id, draft)
        return jsonify(draft=draft)

    @app.post("/api/collect")
    def api_collect():
        require_api_header()
        return jsonify(started=state.trigger(), running=True)

    @app.post("/api/classify-test")
    def api_classify_test():
        require_api_header()
        data = request.get_json(silent=True) or {}
        matches = Matcher(state.cfg.products).classify(data.get("title", ""), data.get("body", ""))
        names = {p.id: p.name for p in state.cfg.products}
        return jsonify(matches=[{**m.to_dict(), "product_name": names.get(m.product_id)} for m in matches])

    @app.get("/api/logs")
    def api_logs():
        return jsonify(lines=list(state.logs))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        error = None
        saved = request.args.get("saved") == "1"
        text = config_mod.read_config_text(state.config_path)
        if request.method == "POST":
            if request.form.get("csrf") != csrf_token:
                abort(403)
            text = request.form.get("config", "").replace("\r\n", "\n")
            try:
                state.save_config(text)
                return redirect(url_for("settings", saved="1"))
            except ConfigError as e:
                error = str(e)
        cfg = state.cfg
        ai_ok, ai_reason = ai_status()
        return render_template(
            "settings.html",
            config_text=text,
            error=error,
            saved=saved,
            csrf=csrf_token,
            mode=resolve_mode(cfg),
            source=cfg.settings.source,
            has_naver_keys=config_mod.naver_credentials() is not None,
            interval=effective_interval(cfg),
            query_count=len(cfg.all_search_queries()),
            api_calls=estimate_api_calls_per_day(cfg),
            api_limit=API_DAILY_LIMIT,
            ai_ok=ai_ok,
            ai_reason=ai_reason,
            runs=state.store.recent_runs(15),
        )

    return app
