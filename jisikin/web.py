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
from .diagnose import run_diagnostics
from .drafter import DraftError, ai_status, generate_draft
from .exposure import check_exposure
from .matcher import Matcher
from .storage import STATUSES, TODO_STATUSES, Store, iso, now_kst

API_DAILY_LIMIT = 25000


class AppState:
    def __init__(self, config_path: Path, db_path: Path | str, env_path: Path | None = None):
        self.config_path = Path(config_path)
        self.env_path = Path(env_path) if env_path else config_mod.ENV_PATH
        self.cfg: AppConfig = config_mod.load_config(self.config_path)
        self.store = Store(db_path)
        self.running = False
        self.running_kind = ""  # collect / exposure
        self.next_run_at: datetime | None = None
        self.next_exposure_at: datetime | None = None
        self.logs: deque[str] = deque(maxlen=300)
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._force = False
        self._force_exposure = False
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

    # --- 수집 / 상위노출 확인 (동시에 하나만 실행)
    def _run(self, kind: str, label: str, job):
        if not self._run_lock.acquire(blocking=False):
            return None
        self.running, self.running_kind = True, kind
        try:
            self.log(f"{label} 시작")
            summary = job()
            self.log(f"{label} 완료 — " + summary.text())
            return summary
        except Exception:
            self.log(f"{label} 중 오류:\n" + traceback.format_exc())
            return None
        finally:
            self.running, self.running_kind = False, ""
            self._run_lock.release()

    def run_collection(self):
        return self._run("collect", "수집", lambda: collect(self.cfg, self.store, log=self.log))

    def run_exposure(self):
        return self._run("exposure", "상위노출 확인", lambda: check_exposure(self.cfg, self.store, log=self.log))

    def exposure_interval_hours(self) -> int:
        h = self.cfg.settings.exposure_interval_hours
        return h if h > 0 and self.cfg.exposure_targets() else 0

    def trigger(self, kind: str = "collect") -> bool:
        """지금 실행. 다른 작업이 진행 중이면 끝난 뒤 이어서 실행한다."""
        if self.running and self.running_kind == kind:
            return False
        if self._thread and self._thread.is_alive():
            if kind == "exposure":
                self._force_exposure = True
            else:
                self._force = True
            self._wake.set()
            return True
        if self.running:
            return False
        target = self.run_exposure if kind == "exposure" else self.run_collection
        threading.Thread(target=target, daemon=True).start()
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

            hours = self.exposure_interval_hours()
            now = now_kst()
            if hours > 0 and self.next_exposure_at is None:
                last = self.store.last_exposure_time()  # 재시작해도 주기를 이어서
                self.next_exposure_at = last + timedelta(hours=hours) if last else now
            if self._force_exposure or (hours > 0 and now >= self.next_exposure_at):
                self._force_exposure = False
                self.run_exposure()
                self.next_exposure_at = now_kst() + timedelta(hours=hours) if hours > 0 else None
            elif hours <= 0:
                self.next_exposure_at = None
            elif self.next_exposure_at > now + timedelta(hours=hours):
                self.next_exposure_at = now + timedelta(hours=hours)

            wait = 60.0
            for t in (self.next_run_at, self.next_exposure_at):
                if t:
                    wait = min(wait, (t - now_kst()).total_seconds())
            self._wake.wait(timeout=max(1.0, wait))
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
        runs = state.store.recent_runs(1, exclude_mode="exposure")
        exp_runs = state.store.recent_runs(1, mode="exposure")
        return jsonify(
            products=product_payload(cfg),
            counts=state.store.todo_counts(cfg.settings.max_age_days),
            running=state.running,
            running_kind=state.running_kind,
            last_run=runs[0] if runs else None,
            next_run_at=iso(state.next_run_at),
            interval=effective_interval(cfg),
            mode=resolve_mode(cfg),
            ai={"enabled": ok, "reason": reason},
            exposure={
                "keywords": len(cfg.exposure_targets()),
                "interval_hours": state.exposure_interval_hours(),
                "next_at": iso(state.next_exposure_at),
                "last_run": exp_runs[0] if exp_runs else None,
                "sources": [{"id": s, "name": config_mod.EXPOSURE_SOURCES[s]} for s in cfg.settings.exposure_sources],
            },
        )

    @app.get("/api/exposure")
    def api_exposure():
        a = request.args
        groups = state.store.latest_exposures(product=a.get("product") or None)
        order = {(p.id, kw): i for i, (p, kw) in enumerate(state.cfg.exposure_targets())}
        keep = (
            "doc_id url title snippet body answer_count reward asked_at views views_per_day views_per_day_kind "
            "status draft ranks prev_ranks best_rank categories matches detail_fetched_at first_seen"
        ).split()
        out = []
        for g in groups:
            if (g["product"], g["keyword"]) not in order:
                continue  # 설정에서 지운 검색어
            posts = g["posts"]
            if a.get("unanswered") == "1":
                posts = [p for p in posts if p["status"] in TODO_STATUSES]
            items = []
            for p in posts:
                item = {k: p.get(k) for k in keep}
                item["body"] = (p.get("body") or "")[:300]
                item["product"] = g["product"]
                items.append(item)
            out.append({**{k: g[k] for k in ("product", "keyword", "checked_at", "sources")}, "posts": items})
        # 노출 글이 있는 검색어 먼저 (설정 순서 유지), 노출 글이 없는 검색어는 아래로
        out.sort(key=lambda g: (not g["posts"], order[(g["product"], g["keyword"])]))
        return jsonify(groups=out)

    @app.post("/api/exposure/check")
    def api_exposure_check():
        require_api_header()
        if not state.cfg.exposure_targets():
            return jsonify(error="설정에 상위노출 검색어(exposure)가 없습니다"), 400
        return jsonify(started=state.trigger("exposure"))

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
        product = cfg.product(q.get("product")) or cfg.product(state.store.exposure_product(doc_id))
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

    @app.post("/api/diagnose")
    def api_diagnose():
        require_api_header()
        ok, lines = run_diagnostics(state.cfg)
        return jsonify(ok=ok, lines=lines)

    @app.post("/settings/keys")
    def settings_keys():
        if request.form.get("csrf") != csrf_token:
            abort(403)
        # 비워둔 칸은 기존 값 유지, '삭제' 를 입력하면 지움
        values = {}
        for key in config_mod.ENV_KEYS:
            v = (request.form.get(key) or "").strip()
            if v == "삭제":
                values[key] = ""
            elif v:
                values[key] = v
        if values:
            try:
                config_mod.save_env_values(values, state.env_path)
            except ConfigError as e:
                return redirect(url_for("settings", key_error=str(e)))
            state.log("API 키 저장 — 새 설정으로 수집을 시작합니다")
            state.trigger()
        return redirect(url_for("settings", keys_saved="1"))

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
            exposure_count=len(cfg.exposure_targets()),
            exposure_hours=state.exposure_interval_hours(),
            api_calls=estimate_api_calls_per_day(cfg),
            api_limit=API_DAILY_LIMIT,
            ai_ok=ai_ok,
            ai_reason=ai_reason,
            masked=config_mod.masked_env(),
            keys_saved=request.args.get("keys_saved") == "1",
            key_error=request.args.get("key_error"),
            runs=state.store.recent_runs(15),
        )

    return app
