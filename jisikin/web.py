"""대시보드(Flask) + 자동 수집 스케줄러.

PC 에서 혼자 쓸 때는 로그인 없이, 서버에 올려 여러 명이 쓸 때는(JISIKIN_ADMIN_PASSWORD 설정) 로그인 후 사용.
"""
from __future__ import annotations

import os
import secrets
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, link
from . import config as config_mod
from .collector import collect, effective_interval, estimate_api_calls_per_day, resolve_mode
from .config import AppConfig, ConfigError
from .diagnose import run_diagnostics
from .calc import calc_status
from .drafter import MAX_EXAMPLES, USAGE_KINDS, DraftError, ai_status, generate_draft_with_calc, generate_social_draft, monthly_usage
from .exposure import all_targets, check_exposure
from .keywords import SOURCE_LABELS, due_products, ensure_default_seeds, generate_for_product, save_seed_settings, seed_settings
from .matcher import Matcher
from .migrations import migrate_config
from .searchad import compact
from .cafe import collect_cafe
from .social import PLATFORMS, YouTubeBudget, collect_social, social_matcher, social_status
from .tracker import check_answers
from .storage import STATUSES, TODO_STATUSES, ApiBudget, Store, from_iso, iso, now_kst

API_DAILY_LIMIT = 25000
EXAMPLE_CHANNELS = {"kin": "지식iN 답변", "youtube": "유튜브 댓글", "cafe": "카페 댓글"}  # AI 초안 예시는 채널별로 따로
KRW_PER_USD = 1400  # 화면에 원화로 대략 보여줄 때만 쓰는 환율


class AppState:
    def __init__(self, config_path: Path, db_path: Path | str, env_path: Path | None = None, data_dir: Path | None = None):
        self.config_path = Path(config_path)
        self.env_path = Path(env_path) if env_path else config_mod.ENV_PATH
        self.data_dir = Path(data_dir) if data_dir else Path(db_path).parent if str(db_path) != ":memory:" else config_mod.DATA_DIR
        self.logs: deque[str] = deque(maxlen=300)
        self.store = Store(db_path)
        migrate_config(self.config_path, self.store, log=self.log)  # 예전에 복사한 config.yaml 에 새 제품 등 반영 (한 번만)
        self.cfg: AppConfig = config_mod.load_config(self.config_path)
        self._default_seeds()
        self.running = False
        self.running_kind = ""  # collect / exposure / keywords / social
        self.next_run_at: datetime | None = None
        self.next_exposure_at: datetime | None = None
        self.next_social_at: datetime | None = None
        self.next_cafe_at: datetime | None = None
        self.next_track_at: datetime | None = None
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._force = False
        self._force_exposure = False
        self._force_social = False
        self._force_cafe = False
        self._force_track = False
        self._pending_keywords: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 설정이 바뀌었을 수 있으니 시작할 때 한 번 전체 재분류
        self.store.classify(Matcher(self.cfg.products))
        self.store.classify_social(social_matcher(self.cfg))

    def log(self, msg: str) -> None:
        line = f"{now_kst():%H:%M:%S} {msg}"
        self.logs.append(line)
        print(line, flush=True)

    def _default_seeds(self) -> None:
        added = ensure_default_seeds(self.cfg, self.store)
        if added:
            self.log(f"메인 키워드 기본값 등록: {', '.join(added)} — 세부 검색어를 자동으로 만듭니다")

    # --- 설정
    def save_config(self, text: str) -> AppConfig:
        cfg = config_mod.save_config_text(text, self.config_path)
        self.cfg = cfg
        self._default_seeds()  # 새로 넣은 제품의 exposure.seeds
        n = self.store.classify(Matcher(cfg.products))
        m = self.store.classify_social(social_matcher(cfg))
        self.log(f"설정 저장 — 전체 재분류 완료 (관련 질문 {n}건, 유튜브·쓰레드 {m}건)")
        self._wake.set()  # 수집 주기 변경 반영
        return cfg

    # --- 작업 실행 (수집 / 상위노출 확인 / 검색어 생성 — 동시에 하나만)
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

    def run_keywords(self, product_id: str):
        return self._run(
            "keywords", "검색어 자동 생성", lambda: generate_for_product(self.cfg, self.store, product_id, log=self.log)
        )

    def run_social(self):
        return self._run(
            "social", "유튜브·쓰레드 수집",
            lambda: collect_social(self.cfg, self.store, log=self.log, save_threads_token=self._save_threads_token),
        )

    def run_track(self):
        return self._run("track", "작업 결과 확인", lambda: check_answers(self.cfg, self.store, log=self.log))

    def next_track_time(self, now: datetime | None = None) -> datetime | None:
        """다음 확인 시각: 매일 track_check_hour 시. 오늘 그 시각이 지났는데 아직 안 했으면 지금."""
        hour = self.cfg.settings.track_check_hour
        if hour < 0:
            return None
        now = now or now_kst()
        today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        runs = self.store.recent_runs(1, mode="track")
        last = from_iso(runs[0]["started_at"]) if runs else None
        if now < today:
            return today
        if last is None or last < today:
            return now
        return today + timedelta(days=1)

    def _save_threads_token(self, token: str) -> None:
        config_mod.save_env_values({"THREADS_ACCESS_TOKEN": token}, self.env_path)

    def social_interval_hours(self) -> int:
        h = self.cfg.settings.social_interval_hours
        st = social_status()
        return h if h > 0 and (st["youtube"] or st["threads"]) else 0

    def run_cafe(self):
        return self._run("cafe", "카페 글 수집", lambda: collect_cafe(self.cfg, self.store, log=self.log))

    def cafe_interval_minutes(self) -> int:
        m = self.cfg.settings.cafe_interval_minutes
        return m if m > 0 and social_status()["cafe"] and self.cfg.cafe_queries() else 0

    def exposure_targets(self):
        return all_targets(self.cfg, self.store)

    def exposure_interval_hours(self) -> int:
        h = self.cfg.settings.exposure_interval_hours
        return h if h > 0 and self.exposure_targets() else 0

    def trigger(self, kind: str = "collect", product: str | None = None, then_exposure: bool = False) -> bool:
        """지금 실행. 다른 작업이 진행 중이면 끝난 뒤 이어서 실행한다."""
        if kind != "keywords" and self.running and self.running_kind == kind:
            return False
        if self._thread and self._thread.is_alive():
            if kind == "keywords":
                if product and product not in self._pending_keywords:
                    self._pending_keywords.append(product)
                if then_exposure:
                    self._force_exposure = True
            elif kind == "exposure":
                self._force_exposure = True
            elif kind == "social":
                self._force_social = True
            elif kind == "cafe":
                self._force_cafe = True
            elif kind == "track":
                self._force_track = True
            else:
                self._force = True
            self._wake.set()
            return True
        if self.running:
            return False
        if kind == "keywords":
            target, args = self.run_keywords, (product,)
        else:
            runners = {"exposure": self.run_exposure, "social": self.run_social, "track": self.run_track, "cafe": self.run_cafe}
            target, args = runners.get(kind, self.run_collection), ()
        threading.Thread(target=target, args=args, daemon=True).start()
        return True

    def start_scheduler(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                self.log("스케줄러 오류:\n" + traceback.format_exc())
            wait = 60.0
            for t in (self.next_run_at, self.next_exposure_at, self.next_social_at, self.next_track_at, self.next_cafe_at):
                if t:
                    wait = min(wait, (t - now_kst()).total_seconds())
            self._wake.wait(timeout=max(1.0, wait))
            self._wake.clear()

    def _tick(self) -> None:
        # 1) 새 질문 수집
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

        # 2) 메인 키워드 → 세부 검색어 (요청받은 것 + 일주일 지난 것)
        for pid in due_products(self.cfg, self.store):
            if pid not in self._pending_keywords:
                self._pending_keywords.append(pid)
        while self._pending_keywords and not self._stop.is_set():
            res = self.run_keywords(self._pending_keywords.pop(0))
            if res is not None and res.candidates:
                self._force_exposure = True  # 새 검색어로 바로 상위노출 확인

        # 3) 상위노출 확인
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

        # 4) 유튜브 · 쓰레드
        hours = self.social_interval_hours()
        now = now_kst()
        if hours > 0 and self.next_social_at is None:
            runs = self.store.recent_runs(1, mode="social")  # 재시작해도 주기를 이어서
            last = from_iso(runs[0]["started_at"]) if runs else None
            self.next_social_at = last + timedelta(hours=hours) if last else now
        if self._force_social or (hours > 0 and now >= self.next_social_at):
            self._force_social = False
            self.run_social()
            self.next_social_at = now_kst() + timedelta(hours=hours) if hours > 0 else None
        elif hours <= 0:
            self.next_social_at = None
        elif self.next_social_at > now + timedelta(hours=hours):
            self.next_social_at = now + timedelta(hours=hours)

        # 5) 네이버 카페 글
        minutes = self.cafe_interval_minutes()
        now = now_kst()
        if minutes > 0 and self.next_cafe_at is None:
            runs = self.store.recent_runs(1, mode="cafe")  # 재시작해도 주기를 이어서
            last = from_iso(runs[0]["started_at"]) if runs else None
            self.next_cafe_at = last + timedelta(minutes=minutes) if last else now
        if self._force_cafe or (minutes > 0 and now >= self.next_cafe_at):
            self._force_cafe = False
            self.run_cafe()
            self.next_cafe_at = now_kst() + timedelta(minutes=minutes) if minutes > 0 else None
        elif minutes <= 0:
            self.next_cafe_at = None
        elif self.next_cafe_at > now + timedelta(minutes=minutes):
            self.next_cafe_at = now + timedelta(minutes=minutes)

        # 6) 작업 결과 (매일 한 번)
        self.next_track_at = self.next_track_time()
        if self._force_track or (self.next_track_at and now_kst() >= self.next_track_at):
            self._force_track = False
            self.run_track()
            self.next_track_at = self.next_track_time()


def create_app(state: AppState, behind_proxy: bool = False) -> Flask:
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.secret_key = auth.secret_key(state.data_dir)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("JISIKIN_SECURE_COOKIE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if behind_proxy:
        # 클라우드(Render 등)는 HTTPS 를 앞단에서 처리하고 요청을 넘겨준다
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    limiter = auth.LoginLimiter()
    tickets = link.TicketBook()

    # ------------------------------------------------------------ 공통

    def csrf_token() -> str:
        tok = session.get("csrf")
        if not tok:
            tok = session["csrf"] = secrets.token_urlsafe(16)
        return tok

    def check_csrf() -> None:
        sent = request.form.get("csrf", "").encode()
        expected = (session.get("csrf") or "-").encode()
        if not secrets.compare_digest(sent, expected):
            abort(403)

    def require_api_header() -> None:
        # 다른 웹사이트가 이 서버로 요청을 보내지 못하게 (사용자 정의 헤더는 교차 출처에서 보낼 수 없음)
        if request.headers.get("X-Jisikin") != "1":
            abort(403)

    def goal_payload(cfg) -> list[dict]:
        targets = [(k, n, g) for k, n, g in (
            ("kin", "지식iN", cfg.settings.goal_kin), ("cafe", "카페", cfg.settings.goal_cafe), ("youtube", "유튜브", cfg.settings.goal_youtube),
        ) if g > 0]
        if not targets or not auth.auth_enabled():
            return []
        done = state.store.today_by_channel()
        return [
            {"username": u["username"], "name": u["name"],
             "items": [{"channel": k, "label": n, "done": done.get(u["username"], {}).get(k, 0), "goal": g} for k, n, g in targets]}
            for u in state.store.list_users() if u["active"]
        ]

    def people() -> dict[str, str]:
        names = {"": "", auth.admin_username(): "관리자"}
        names.update({u["username"]: u["name"] for u in state.store.list_users()})
        return names

    def admin_required(view):
        @wraps(view)
        def wrapper(*a, **kw):
            if g.user["role"] != "admin":
                if request.path.startswith("/api/"):
                    return jsonify(error="관리자만 사용할 수 있습니다"), 403
                abort(403)
            return view(*a, **kw)

        return wrapper

    @app.before_request
    def load_user():
        if not auth.auth_enabled():
            g.user = auth.LOCAL_USER
            return None
        user = session.get("user")
        if user and user.get("role") == "staff":
            row = state.store.get_user(user["username"])
            if not row or not row["active"]:
                session.clear()
                user = None
        g.user = user
        if user or request.endpoint in ("login", "static", "healthz", "sso", "link_summary", "link_deactivate"):
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="로그인이 필요합니다"), 401
        return redirect(url_for("login", next=request.path))

    @app.context_processor
    def inject():
        return {"user": g.get("user") or auth.LOCAL_USER, "auth_on": auth.auth_enabled(), "csrf": csrf_token()}

    @app.errorhandler(403)
    def forbidden(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="권한이 없습니다"), 403
        return render_template("message.html", title="권한이 없습니다", message="관리자만 볼 수 있는 화면입니다."), 403

    def product_payload(cfg: AppConfig) -> list[dict]:
        return [
            {
                "id": p.id, "name": p.name, "color": p.color, "url": p.url,
                "categories": [c.name for c in p.categories], "social_queries": p.social_queries(),
                "max_bytes": p.max_bytes,
            }
            for p in cfg.products
        ]

    # ------------------------------------------------------------ 로그인

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    # ------------------------------------------------------------ 업무 데스크 연동 (link.py)

    @app.get("/api/link/summary")
    def link_summary():
        """업무 데스크 홈에 보여줄 숫자. 업무 데스크만 알고 있는 비밀값으로 확인한다."""
        if not link.bearer_ok(request.headers.get("Authorization")):
            abort(404)
        cfg = state.cfg
        kin = state.store.todo_counts(cfg.settings.max_age_days)["products"]
        social = state.store.social_counts({pf: cfg.settings.social_age_days(pf) for pf in ("youtube", "threads", "cafe")})
        stats = state.store.answer_stats()
        names = people()
        total = lambda d: sum(p["total"] for p in d.values())  # noqa: E731
        return jsonify(
            title="답변·댓글 센터",
            counts=[
                {"label": "답변할 지식iN 질문", "value": total(kin)},
                {"label": "아직 안 연 질문", "value": sum(p["new"] for p in kin.values())},
                {"label": "유튜브 댓글 거리", "value": total(social.get("youtube", {}))},
                {"label": "쓰레드 댓글 거리", "value": total(social.get("threads", {}))},
                {"label": "카페 댓글 거리", "value": total(social.get("cafe", {}))},
                {"label": "오늘 답변·댓글 완료", "value": sum(s["today"] for s in stats.values())},
            ],
            staff=[
                {
                    "login": u, "name": names.get(u, u), "today": s["today"], "week": s["week"],
                    "role": "admin" if u == auth.admin_username() else "staff",
                }
                for u, s in stats.items()
                if u
            ],
            updated_at=iso(now_kst()),
        )

    @app.get("/sso")
    def sso():
        """업무 데스크에서 [열기]를 누르면 1회용 입장권을 들고 온다. 확인되면 바로 로그인."""
        nxt = request.args.get("next") or "/"
        nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
        if not auth.auth_enabled():
            return redirect(nxt)
        try:
            claims = link.verify_ticket(request.args.get("ticket", ""), tickets)
        except link.TicketError as e:
            return render_template("message.html", title="업무 데스크에서 다시 열어 주세요", message=str(e)), 403
        if claims["role"] == "admin":
            user = {"username": auth.admin_username(), "name": "관리자", "role": "admin"}
        else:
            username = str(claims["sub"]).strip().lower()
            if not auth.USERNAME_RE.match(username) or username == auth.admin_username():
                return render_template(
                    "message.html", title="아이디를 확인해 주세요",
                    message="업무 데스크 아이디를 답변 센터에서 쓸 수 없습니다. 영문 소문자·숫자 2~20자로 바꿔 주세요.",
                ), 403
            name = str(claims.get("name") or username)[:40]
            # 직원 계정은 업무 데스크가 기준: 없으면 만들고 꺼져 있으면 다시 켠다. 기존 비밀번호는 그대로 둔다.
            exists = state.store.get_user(username) is not None
            state.store.save_user(username, name, None if exists else auth.hash_password(secrets.token_urlsafe(24)), active=True)
            user = {"username": username, "name": name, "role": "staff"}
        session.clear()
        session.permanent = True
        session["user"] = user
        return redirect(nxt)

    @app.post("/api/link/deactivate")
    def link_deactivate():
        """업무 데스크에서 직원을 사용 중지하면 여기서도 끈다."""
        if not link.bearer_ok(request.headers.get("Authorization")):
            abort(404)
        username = str((request.get_json(silent=True) or {}).get("login", "")).strip().lower()
        row = state.store.get_user(username) if username else None
        if row and row["active"]:
            state.store.save_user(username, row["name"], None, active=False)
            state.log(f"업무 데스크에서 {row['name']} 계정 사용 중지")
        return jsonify(ok=True, found=bool(row))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.auth_enabled():
            return redirect(url_for("index"))
        error = None
        ip = request.remote_addr or "?"
        if request.method == "POST":
            check_csrf()
            if limiter.blocked(ip):
                error = "로그인 시도가 너무 많습니다. 15분 뒤에 다시 시도하세요."
            else:
                user = auth.authenticate(state.store, request.form.get("username", ""), request.form.get("password", ""))
                if user:
                    limiter.reset(ip)
                    session.clear()
                    session.permanent = True
                    session["user"] = user
                    nxt = request.args.get("next") or "/"
                    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
                limiter.fail(ip)
                error = "아이디 또는 비밀번호가 맞지 않습니다."
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login") if auth.auth_enabled() else url_for("index"))

    # ------------------------------------------------------------ 대시보드

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/meta")
    def api_meta():
        cfg = state.cfg
        ok, reason = ai_status()
        runs = state.store.recent_runs(1, exclude_mode=("exposure", "social", "track", "cafe"))
        social_runs = state.store.recent_runs(1, mode="social")
        exp_runs = state.store.recent_runs(1, mode="exposure")
        cafe_runs = state.store.recent_runs(1, mode="cafe")
        track_runs = state.store.recent_runs(1, mode="track")
        names = people()
        stats = state.store.answer_stats()
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
            user=g.user,
            auth=auth.auth_enabled(),
            people=names,
            answer_stats=[
                {"username": u, "name": names.get(u, u), **s} for u, s in sorted(stats.items(), key=lambda kv: -kv[1]["today"])
            ],
            # 직원별 오늘 목표: 지식iN·카페·유튜브 따로 (사용 중인 직원 계정 모두, 0건이어도 보임)
            goals=goal_payload(cfg),
            track={
                "hour": cfg.settings.track_check_hour,
                "next_at": iso(state.next_track_at or state.next_track_time()),
                "last_run": track_runs[0] if track_runs else None,
                "youtube": bool(social_status().get("youtube")),
            },
            exposure={
                "keywords": len(state.exposure_targets()),
                "interval_hours": state.exposure_interval_hours(),
                "next_at": iso(state.next_exposure_at),
                "last_run": exp_runs[0] if exp_runs else None,
                "sources": [{"id": s, "name": config_mod.EXPOSURE_SOURCES[s]} for s in cfg.settings.exposure_sources],
            },
            social={
                "platforms": social_status(),
                "counts": state.store.social_counts({pf: cfg.settings.social_age_days(pf) for pf in ("youtube", "threads", "cafe")}),
                "interval_hours": state.social_interval_hours(),
                "next_at": iso(state.next_social_at),
                "last_run": social_runs[0] if social_runs else None,
                "queries": len(cfg.social_queries()),
            },
            cafe={
                "enabled": social_status()["cafe"],
                "queries": len(cfg.cafe_queries()),
                "interval_minutes": state.cafe_interval_minutes(),
                "next_at": iso(state.next_cafe_at),
                "last_run": cafe_runs[0] if cafe_runs else None,
            },
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
            "categories matches status status_by status_changed_at draft draft_calc priority detail_fetched_at"
        ).split()
        items = []
        for r in rows:
            item = {k: r.get(k) for k in keep}
            item["body"] = (r.get("body") or "")[:600]
            items.append(item)
        return jsonify(items=items)

    def question_product(q: dict):
        cfg = state.cfg
        product = cfg.product(q.get("product")) or cfg.product(state.store.exposure_product(q["doc_id"]))
        if product is None and q.get("matches"):
            product = cfg.product(q["matches"][0].get("product_id"))
        return product

    @app.post("/api/questions/<doc_id>/status")
    def api_set_status(doc_id: str):
        require_api_header()
        data = request.get_json(silent=True) or {}
        status = data.get("status")
        if status not in STATUSES:
            return jsonify(error="잘못된 상태"), 400
        if isinstance(data.get("draft"), str):
            state.store.save_draft_edit(doc_id, data["draft"])  # 초안 칸에서 마지막으로 고친 내용
        by = g.user["username"]
        if not state.store.set_status(doc_id, status, by=by):
            return jsonify(error="질문을 찾을 수 없습니다"), 404
        if status == "answered":
            # 올린 답변(초안을 고친 최종본)을 [답변 예시]의 후보로 남긴다
            q = state.store.get(doc_id)
            product = question_product(q) if q else None
            if product:
                state.store.capture_final_answer(doc_id, product.id, by=by)
        else:
            state.store.drop_final_answer(doc_id)
        return jsonify(ok=True)

    @app.post("/api/questions/<doc_id>/draft")
    def api_draft(doc_id: str):
        require_api_header()
        q = state.store.get(doc_id)
        if not q:
            return jsonify(error="질문을 찾을 수 없습니다"), 404
        product = question_product(q)
        if product is None:
            return jsonify(error="어느 제품과 관련된 질문인지 알 수 없습니다"), 400
        try:
            draft, calc_info = generate_draft_with_calc(state.cfg, product, q, store=state.store)
        except DraftError as e:
            return jsonify(error=str(e)), 400
        state.store.set_draft(doc_id, draft, calc=calc_info)
        return jsonify(draft=draft, calc=calc_info)

    @app.post("/api/questions/<doc_id>/draft/save")
    def api_draft_save(doc_id: str):
        require_api_header()
        text = (request.get_json(silent=True) or {}).get("draft")
        if not isinstance(text, str) or len(text) > 10000:
            return jsonify(error="잘못된 요청"), 400
        if not state.store.save_draft_edit(doc_id, text):
            return jsonify(error="질문을 찾을 수 없습니다"), 404
        return jsonify(ok=True)

    @app.post("/api/collect")
    def api_collect():
        require_api_header()
        return jsonify(started=state.trigger(), running=True)

    # ------------------------------------------------------------ 상위노출

    @app.get("/api/exposure")
    def api_exposure():
        a = request.args
        groups = state.store.latest_exposures(product=a.get("product") or None)
        order = {(p.id, compact(kw)): i for i, (p, kw) in enumerate(state.exposure_targets())}
        keep = (
            "doc_id url title snippet body answer_count reward asked_at views views_per_day views_per_day_kind "
            "status status_by draft ranks prev_ranks best_rank categories matches detail_fetched_at first_seen"
        ).split()
        out = []
        for g_ in groups:
            key = (g_["product"], compact(g_["keyword"]))
            if key not in order:
                continue  # 설정/검색어 관리에서 지우거나 끈 검색어
            posts = g_["posts"]
            if a.get("unanswered") == "1":
                posts = [p for p in posts if p["status"] in TODO_STATUSES]
            items = []
            for p in posts:
                item = {k: p.get(k) for k in keep}
                item["body"] = (p.get("body") or "")[:300]
                item["product"] = g_["product"]
                items.append(item)
            out.append({**{k: g_[k] for k in ("product", "keyword", "checked_at", "sources")}, "posts": items, "_order": order[key]})
        # 노출 글이 있는 검색어 먼저 (설정 순서 유지), 노출 글이 없는 검색어는 아래로
        out.sort(key=lambda x: (not x["posts"], x.pop("_order")))
        return jsonify(groups=out)

    @app.post("/api/exposure/check")
    def api_exposure_check():
        require_api_header()
        if not state.exposure_targets():
            return jsonify(error="확인할 검색어가 없습니다. [검색어 관리]에서 메인 키워드를 넣어주세요"), 400
        return jsonify(started=state.trigger("exposure"))

    # ------------------------------------------------------------ 유튜브 · 쓰레드

    SOCIAL_KEEP = (
        "post_id platform url title body author author_url thumbnail published_at views likes comments duration is_short queries "
        "product score categories matches status status_by status_changed_at draft priority first_seen"
    ).split()

    @app.get("/api/social")
    def api_social():
        a = request.args
        platform = a.get("platform", "youtube")
        if platform not in PLATFORMS:
            return jsonify(error="잘못된 요청"), 400
        rows = state.store.list_social(
            platform,
            product=a.get("product") or None,
            category=a.get("category") or None,
            status=a.get("status", "todo"),
            include_low=a.get("include_low") == "1",
            max_age_days=state.cfg.settings.social_age_days(platform),
            query=(a.get("q") or "").strip(),
            # 유튜브는 조회수 많은 영상 = 사람들이 많이 보는 영상이 먼저
            sort=a.get("sort") or ("views" if platform == "youtube" else "priority"),
            shorts=a.get("shorts", ""),
        )
        items = []
        for r in rows:
            item = {k: r.get(k) for k in SOCIAL_KEEP}
            item["body"] = (r.get("body") or "")[:600]
            items.append(item)
        return jsonify(items=items)

    def social_product(post: dict):
        cfg = state.cfg
        product = cfg.product(post.get("product"))
        if product is None and post.get("matches"):
            product = cfg.product(post["matches"][0].get("product_id"))
        return product

    @app.post("/api/social/<path:post_id>/status")
    def api_social_status(post_id: str):
        require_api_header()
        data = request.get_json(silent=True) or {}
        status = data.get("status")
        if status not in STATUSES:
            return jsonify(error="잘못된 상태"), 400
        if isinstance(data.get("draft"), str):
            state.store.save_social_draft_edit(post_id, data["draft"])
        by = g.user["username"]
        if not state.store.set_social_status(post_id, status, by=by):
            return jsonify(error="글을 찾을 수 없습니다"), 404
        if status == "answered":
            # 올린 유튜브 댓글을 [답변 예시 > 유튜브 댓글]의 후보로 남긴다
            post = state.store.get_social(post_id)
            product = social_product(post) if post else None
            if product:
                state.store.capture_final_comment(post_id, product.id, by=by)
        else:
            state.store.drop_final_answer(post_id)
        return jsonify(ok=True)

    @app.post("/api/social/<path:post_id>/draft")
    def api_social_draft(post_id: str):
        require_api_header()
        post = state.store.get_social(post_id)
        if not post:
            return jsonify(error="글을 찾을 수 없습니다"), 404
        product = social_product(post)
        if product is None:
            return jsonify(error="어느 제품과 관련된 글인지 알 수 없습니다"), 400
        try:
            draft = generate_social_draft(state.cfg, product, post, store=state.store)
        except DraftError as e:
            return jsonify(error=str(e)), 400
        state.store.set_social_draft(post_id, draft)
        return jsonify(draft=draft)

    @app.post("/api/social/<path:post_id>/draft/save")
    def api_social_draft_save(post_id: str):
        require_api_header()
        text = (request.get_json(silent=True) or {}).get("draft")
        if not isinstance(text, str) or len(text) > 10000:
            return jsonify(error="잘못된 요청"), 400
        if not state.store.save_social_draft_edit(post_id, text):
            return jsonify(error="글을 찾을 수 없습니다"), 404
        return jsonify(ok=True)

    @app.get("/api/results")
    def api_results():
        """작업 결과: 완료한 글 + 마지막 확인 결과 (+ 지식iN 은 지금 상위노출 순위)."""
        checks = state.store.latest_answer_checks()
        exposed: dict[str, dict] = {}
        for grp in state.store.latest_exposures():
            for p in grp["posts"]:
                cur = exposed.get(p["doc_id"])
                if p.get("best_rank") is not None and (cur is None or p["best_rank"] < cur["rank"]):
                    exposed[p["doc_id"]] = {"keyword": grp["keyword"], "rank": p["best_rank"]}
        items = []
        for r in state.store.answered_items():
            if r["platform"] == "threads":
                body = (r.get("body") or "").strip()
                r["title"] = body.split("\n", 1)[0][:90] or "(내용 없음)"
            item = {k: r.get(k) for k in (
                "item_id platform url title product draft status_by status_changed_at thumbnail is_short views comments"
            ).split()}
            item["check"] = checks.get(r["item_id"])
            item["exposure"] = exposed.get(r["item_id"])
            items.append(item)
        return jsonify(items=items)

    @app.post("/api/results/check")
    def api_results_check():
        require_api_header()
        if state.cfg.settings.track_check_hour < 0:
            return jsonify(error="작업 결과 확인이 꺼져 있습니다 (설정의 track_check_hour)"), 400
        return jsonify(started=state.trigger("track"))

    @app.post("/api/social/collect")
    def api_social_collect():
        require_api_header()
        st = social_status()
        if not (st["youtube"] or st["threads"]):
            return jsonify(error="유튜브 API 키나 쓰레드 토큰이 없습니다. [설정] > API 키에 넣어 주세요"), 400
        return jsonify(started=state.trigger("social"))

    @app.post("/api/cafe/collect")
    def api_cafe_collect():
        require_api_header()
        if not social_status()["cafe"]:
            return jsonify(error="네이버 API 키가 없습니다. [설정] > API 키에 넣어 주세요"), 400
        if not state.cfg.cafe_queries():
            return jsonify(error="카페 검색어가 없습니다. [설정]에서 제품의 cafe: keywords 를 넣어 주세요"), 400
        return jsonify(started=state.trigger("cafe"))

    # ------------------------------------------------------------ 검색어 관리 (메인 키워드 → 세부 검색어)

    @app.get("/keywords")
    @admin_required
    def keywords_page():
        return render_template("keywords.html")

    @app.get("/api/keywords")
    @admin_required
    def api_keywords():
        cfg = state.cfg
        exposure = state.store.exposure_counts()
        products = []
        for p in cfg.products:
            s = seed_settings(state.store, p.id)
            auto = state.store.auto_keywords(p.id)
            products.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "color": p.color,
                    "seeds": s["seeds"],
                    "max": s["max"],
                    "min_volume": s["min_volume"],
                    "generated_at": s["generated_at"],
                    "last_error": s["last_error"],
                    "used": [SOURCE_LABELS.get(x, x) for x in s["used"]],
                    "config_keywords": [
                        {"keyword": kw, "exposure": exposure.get((p.id, kw))} for kw in p.exposure.queries()
                    ],
                    "auto": [
                        {
                            "keyword": r["keyword"], "seed": r["seed"],
                            "sources": [SOURCE_LABELS.get(x, x) for x in r["sources"]],
                            "pc": r["pc"], "mobile": r["mobile"], "score": r["score"],
                            "enabled": bool(r["enabled"]), "user_set": bool(r["user_set"]),
                            "exposure": exposure.get((p.id, r["keyword"])),
                        }
                        for r in auto
                    ],
                }
            )
        return jsonify(
            products=products,
            running=state.running and state.running_kind == "keywords",
            pending=list(state._pending_keywords),
            sources={
                "autocomplete": True,
                "searchad": config_mod.searchad_credentials() is not None,
                "ai": ai_status()[0],
            },
            total_enabled=len(state.exposure_targets()),
            exposure_hours=state.cfg.settings.exposure_interval_hours,
        )

    @app.post("/api/keywords/seeds")
    @admin_required
    def api_keywords_seeds():
        require_api_header()
        data = request.get_json(silent=True) or {}
        pid = data.get("product")
        if not state.cfg.product(pid):
            return jsonify(error="제품을 찾을 수 없습니다"), 404
        seeds = data.get("seeds") or []
        if isinstance(seeds, str):
            seeds = seeds.replace("\n", ",").split(",")
        try:
            max_n = int(data.get("max") or 20)
            min_volume = None if data.get("min_volume") in (None, "") else int(data["min_volume"])
        except (TypeError, ValueError):
            return jsonify(error="개수·검색수는 숫자로 넣어주세요"), 400
        s = save_seed_settings(state.store, pid, [str(x) for x in seeds], max_n, min_volume)
        started = bool(s["seeds"]) and state.trigger("keywords", pid, then_exposure=True)
        return jsonify(ok=True, seeds=s["seeds"], max=s["max"], min_volume=s["min_volume"], started=started)

    @app.post("/api/keywords/generate")
    @admin_required
    def api_keywords_generate():
        require_api_header()
        pid = (request.get_json(silent=True) or {}).get("product")
        if not seed_settings(state.store, pid)["seeds"]:
            return jsonify(error="먼저 메인 키워드를 넣어주세요"), 400
        return jsonify(started=state.trigger("keywords", pid, then_exposure=True))

    @app.post("/api/keywords/toggle")
    @admin_required
    def api_keywords_toggle():
        require_api_header()
        data = request.get_json(silent=True) or {}
        pid, kw = data.get("product"), (data.get("keyword") or "").strip()
        if not state.cfg.product(pid) or not kw:
            return jsonify(error="잘못된 요청"), 400
        state.store.set_auto_keyword(pid, kw, bool(data.get("enabled")))
        return jsonify(ok=True)

    @app.post("/api/keywords/add")
    @admin_required
    def api_keywords_add():
        require_api_header()
        data = request.get_json(silent=True) or {}
        pid = data.get("product")
        kw = " ".join((data.get("keyword") or "").split())
        if not state.cfg.product(pid) or not kw or len(kw) > 40:
            return jsonify(error="검색어를 확인해 주세요 (40자 이내)"), 400
        state.store.set_auto_keyword(pid, kw, True, sources=["manual"])
        return jsonify(ok=True)

    # ------------------------------------------------------------ 답변 예시 (관리자)

    def example_payload(ex: dict) -> dict:
        keep = "id product channel doc_id url title question answer source edited starred created_by created_at starred_at".split()
        out = {k: ex.get(k) for k in keep}
        out["starred"], out["edited"] = bool(ex["starred"]), bool(ex["edited"])
        out["created_by_name"] = people().get(ex.get("created_by") or "", ex.get("created_by") or "")
        return out

    @app.get("/examples")
    @admin_required
    def examples_page():
        return render_template("examples.html")

    @app.get("/api/examples")
    @admin_required
    def api_examples():
        cfg = state.cfg
        items = state.store.list_examples()
        return jsonify(
            max=MAX_EXAMPLES,
            ai=ai_status()[0],
            model=cfg.ai.model,
            channels=[{"id": k, "name": v} for k, v in EXAMPLE_CHANNELS.items()],
            products=[
                {"id": p.id, "name": p.name, "color": p.color,
                 "starred": {ch: state.store.starred_count(p.id, ch) for ch in EXAMPLE_CHANNELS}}
                for p in cfg.products
            ],
            items=[example_payload(ex) for ex in items if cfg.product(ex["product"])],
        )

    def star_room(product_id: str, channel: str) -> bool:
        return state.store.starred_count(product_id, channel) < MAX_EXAMPLES

    @app.post("/api/examples/add")
    @admin_required
    def api_examples_add():
        require_api_header()
        data = request.get_json(silent=True) or {}
        pid, answer = data.get("product"), (data.get("answer") or "").strip()
        channel = data.get("channel") or "kin"
        if not state.cfg.product(pid):
            return jsonify(error="제품을 찾을 수 없습니다"), 404
        if channel not in EXAMPLE_CHANNELS:
            return jsonify(error="잘못된 요청"), 400
        if len(answer) < 20 or len(answer) > 5000:
            return jsonify(error="답변을 20~5000자로 넣어주세요"), 400
        starred = star_room(pid, channel)
        ex_id = state.store.add_example(
            pid, answer, title=str(data.get("title") or "")[:200], question=str(data.get("question") or "")[:1000],
            by=g.user["username"], starred=starred, channel=channel,
        )
        return jsonify(ok=True, id=ex_id, starred=starred)

    @app.post("/api/examples/<int:ex_id>/star")
    @admin_required
    def api_examples_star(ex_id: int):
        require_api_header()
        ex = state.store.get_example(ex_id)
        if not ex:
            return jsonify(error="예시를 찾을 수 없습니다"), 404
        on = bool((request.get_json(silent=True) or {}).get("starred"))
        if on and not ex["starred"] and not star_room(ex["product"], ex["channel"]):
            return jsonify(error=f"⭐ 예시는 제품마다 {EXAMPLE_CHANNELS[ex['channel']]} {MAX_EXAMPLES}개까지입니다. 다른 예시의 ⭐를 먼저 빼 주세요"), 400
        state.store.set_example_star(ex_id, on)
        return jsonify(ok=True)

    @app.post("/api/examples/<int:ex_id>/update")
    @admin_required
    def api_examples_update(ex_id: int):
        require_api_header()
        data = request.get_json(silent=True) or {}
        answer = (data.get("answer") or "").strip()
        if len(answer) < 20 or len(answer) > 5000:
            return jsonify(error="답변을 20~5000자로 넣어주세요"), 400
        if not state.store.update_example(ex_id, answer):
            return jsonify(error="예시를 찾을 수 없습니다"), 404
        return jsonify(ok=True)

    @app.post("/api/examples/<int:ex_id>/delete")
    @admin_required
    def api_examples_delete(ex_id: int):
        require_api_header()
        if not state.store.delete_example(ex_id):
            return jsonify(error="예시를 찾을 수 없습니다"), 404
        return jsonify(ok=True)

    # ------------------------------------------------------------ 설정 (관리자)

    @app.post("/api/classify-test")
    @admin_required
    def api_classify_test():
        require_api_header()
        data = request.get_json(silent=True) or {}
        matches = Matcher(state.cfg.products).classify(data.get("title", ""), data.get("body", ""))
        names = {p.id: p.name for p in state.cfg.products}
        return jsonify(matches=[{**m.to_dict(), "product_name": names.get(m.product_id)} for m in matches])

    @app.post("/api/diagnose")
    @admin_required
    def api_diagnose():
        require_api_header()
        ok, lines = run_diagnostics(state.cfg, store=state.store)
        return jsonify(ok=ok, lines=lines)

    @app.post("/settings/keys")
    @admin_required
    def settings_keys():
        check_csrf()
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
            if values.get("THREADS_ACCESS_TOKEN"):
                state.store.kv_set("threads_token_refreshed_at", iso(now_kst()))  # 막 받은 토큰은 바로 연장할 필요 없음
            if values.get("YOUTUBE_API_KEY") or values.get("THREADS_ACCESS_TOKEN"):
                state.trigger("social")
            if any(k.startswith("NAVER_AD_") and v for k, v in values.items()):
                # 검색광고 키가 생기면 월간 검색수로 검색어를 다시 골라야 하므로 바로 다시 생성
                for p in state.cfg.products:
                    if seed_settings(state.store, p.id)["seeds"]:
                        state.trigger("keywords", p.id, then_exposure=True)
        return redirect(url_for("settings", keys_saved="1"))

    @app.post("/settings/users")
    @admin_required
    def settings_users():
        check_csrf()
        f = request.form
        action = f.get("action")
        username = (f.get("username") or "").strip().lower()
        name = (f.get("name") or "").strip()
        password = f.get("password") or ""
        msg = None
        if action == "add":
            if not auth.USERNAME_RE.match(username) or username == auth.admin_username():
                msg = "아이디는 영문 소문자·숫자·_.- 로 2~20자이며 관리자 아이디와 달라야 합니다."
            elif state.store.get_user(username):
                msg = f"'{username}' 아이디가 이미 있습니다."
            elif not name:
                msg = "이름을 넣어주세요."
            elif len(password) < auth.MIN_PASSWORD:
                msg = f"비밀번호는 {auth.MIN_PASSWORD}자 이상으로 해주세요."
            else:
                state.store.save_user(username, name, auth.hash_password(password))
        else:
            user = state.store.get_user(username)
            if not user:
                msg = "직원을 찾을 수 없습니다."
            elif action == "password":
                if len(password) < auth.MIN_PASSWORD:
                    msg = f"비밀번호는 {auth.MIN_PASSWORD}자 이상으로 해주세요."
                else:
                    state.store.save_user(username, user["name"], auth.hash_password(password), bool(user["active"]))
            elif action == "toggle":
                state.store.save_user(username, user["name"], active=not user["active"])
            elif action == "delete":
                state.store.delete_user(username)
            else:
                abort(400)
        if msg:
            return redirect(url_for("settings", user_error=msg) + "#users")
        return redirect(url_for("settings", users_saved="1") + "#users")

    @app.get("/api/logs")
    @admin_required
    def api_logs():
        return jsonify(lines=list(state.logs))

    @app.route("/settings", methods=["GET", "POST"])
    @admin_required
    def settings():
        error = None
        saved = request.args.get("saved") == "1"
        text = config_mod.read_config_text(state.config_path)
        if request.method == "POST":
            check_csrf()
            text = request.form.get("config", "").replace("\r\n", "\n")
            try:
                state.save_config(text)
                return redirect(url_for("settings", saved="1"))
            except ConfigError as e:
                error = str(e)
        cfg = state.cfg
        ai_ok, ai_reason = ai_status()
        calc_ok, calc_reason = calc_status()
        stats = state.store.answer_stats()
        users = []
        for u in state.store.list_users():
            users.append({**u, **stats.get(u["username"], {"today": 0, "week": 0, "total": 0})})
        admin_stats = stats.get(auth.admin_username() if auth.auth_enabled() else "", {"today": 0, "week": 0, "total": 0})
        now = now_kst()
        this_month = monthly_usage(state.store, f"{now:%Y-%m}")
        last_month = monthly_usage(state.store, f"{(now.replace(day=1) - timedelta(days=1)):%Y-%m}")
        return render_template(
            "settings.html",
            config_text=text,
            error=error,
            saved=saved,
            mode=resolve_mode(cfg),
            source=cfg.settings.source,
            has_naver_keys=config_mod.naver_credentials() is not None,
            has_ad_keys=config_mod.searchad_credentials() is not None,
            interval=effective_interval(cfg),
            query_count=len(cfg.all_search_queries()),
            exposure_count=len(state.exposure_targets()),
            exposure_hours=state.exposure_interval_hours(),
            social_keys=social_status(),
            social_hours=state.social_interval_hours(),
            social_setting_hours=cfg.settings.social_interval_hours,
            social_query_count=len(cfg.social_queries()),
            yt_used=YouTubeBudget(state.store, cfg.settings.youtube_daily_units).used(),
            yt_cap=cfg.settings.youtube_daily_units,
            api_calls=estimate_api_calls_per_day(cfg),
            api_limit=API_DAILY_LIMIT,
            api_cap=cfg.settings.api_daily_limit,
            api_used=ApiBudget(state.store, cfg.settings.api_daily_limit).used(),
            interval_setting=cfg.settings.interval_minutes,
            ai_ok=ai_ok,
            ai_reason=ai_reason,
            calc_ok=calc_ok,
            calc_reason=calc_reason,
            calc_products=[p.name for p in cfg.products if p.calc_tools],
            ai_model=cfg.ai.model,
            ai_effort=cfg.ai.effort,
            ai_usage=this_month,
            ai_usage_last=last_month,
            usage_kinds=USAGE_KINDS,
            krw_per_usd=KRW_PER_USD,
            masked=config_mod.masked_env(),
            keys_saved=request.args.get("keys_saved") == "1",
            key_error=request.args.get("key_error"),
            users=users,
            admin_name=auth.admin_username(),
            admin_stats=admin_stats,
            users_saved=request.args.get("users_saved") == "1",
            user_error=request.args.get("user_error"),
            runs=state.store.recent_runs(15),
        )

    return app
