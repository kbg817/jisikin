"""실행 방법

  python -m jisikin              대시보드 실행 (= serve)
  python -m jisikin serve        대시보드 + 자동 수집
  python -m jisikin collect      한 번만 수집하고 결과 출력
  python -m jisikin check        설정·API 키·네이버 연결 점검
  python -m jisikin classify "질문 제목" ["질문 본문"]   분류 결과 확인 (키워드 튜닝용)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser

from werkzeug.serving import make_server

from . import config as config_mod
from .config import ConfigError


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _make_server(app, host: str, port: int):
    """waitress(운영용 서버)가 있으면 쓰고, 없으면 Flask 기본 서버. 포트가 사용 중이면 None."""
    try:
        from waitress import create_server

        logging.getLogger("waitress").setLevel(logging.WARNING)
        try:
            server = create_server(app, host=host, port=port, threads=8)
        except OSError:
            return None
        return server.run
    except ImportError:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        try:
            server = make_server(host, port, app, threaded=True)
        except (OSError, SystemExit):  # werkzeug 는 포트가 사용 중이면 SystemExit 을 낸다
            return None
        return server.serve_forever


def cmd_serve(args) -> int:
    from .auth import auth_enabled
    from .web import AppState, create_app

    host = args.host or os.environ.get("JISIKIN_HOST", "").strip() or "127.0.0.1"
    public = host not in ("127.0.0.1", "localhost", "::1")
    if public and not auth_enabled():
        print(
            "외부에서 접속할 수 있게 실행하려면 로그인 비밀번호가 필요합니다.\n"
            "환경변수 JISIKIN_ADMIN_PASSWORD 에 관리자 비밀번호를 넣어주세요.",
            file=sys.stderr,
        )
        return 1

    state = AppState(config_mod.CONFIG_PATH, config_mod.DB_PATH, data_dir=config_mod.DATA_DIR)
    app = create_app(state, behind_proxy=public)
    port = args.port or int(os.environ.get("PORT") or 0) or state.cfg.settings.port
    url = f"http://127.0.0.1:{port}"
    run = _make_server(app, host, port)
    if run is None:
        print(f"포트 {port} 가 이미 사용 중입니다. 이미 실행 중이 아닌지 확인하거나 config.yaml 의 port 를 바꾸세요.")
        return 1
    if public:
        print(f"\n  답변·댓글 센터 서버 실행 중 ({host}:{port}) — 로그인 필요\n", flush=True)
    else:
        print(f"\n  답변·댓글 센터 대시보드: {url}\n  (종료: Ctrl+C)\n", flush=True)
    if not args.no_collect:
        state.start_scheduler()
    if not args.no_browser and not public:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        run()
    finally:
        state.stop()
    return 0


def cmd_collect(args) -> int:
    from .collector import collect
    from .storage import Store

    cfg = config_mod.load_config()
    store = Store(config_mod.DB_PATH)
    summary = collect(cfg, store)
    print(summary.text())
    for e in summary.errors:
        print("  !", e)
    rows = store.list_questions(max_age_days=cfg.settings.max_age_days, limit=args.show)
    names = {p.id: p.name for p in cfg.products}
    if rows:
        print(f"\n추천순 상위 {len(rows)}건:")
    for r in rows:
        ans = "?" if r["answer_count"] is None else r["answer_count"]
        cats = ",".join(r["categories"]) or "-"
        print(f"  [{names.get(r['product'], r['product'])}|{cats}] 답변{ans} {r['title']}\n      {r['url']}")
    return 0


def cmd_check(args) -> int:
    from .diagnose import main_check

    return main_check()


def cmd_classify(args) -> int:
    from .matcher import Matcher

    cfg = config_mod.load_config()
    names = {p.id: p.name for p in cfg.products}
    matches = Matcher(cfg.products).classify(args.title, args.body or "")
    if not matches:
        print("어느 제품과도 매칭되지 않았습니다.")
    for m in matches:
        mark = "관련" if m.relevant else "낮음"
        print(f"[{mark}] {names[m.product_id]}  점수 {m.score:g}")
        print(f"   키워드: {', '.join(m.keywords) or '-'}")
        print(f"   카테고리: {', '.join(m.categories) or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    config_mod.load_env()
    parser = argparse.ArgumentParser(prog="python -m jisikin", description="답변·댓글 센터 (지식iN · 유튜브 · 쓰레드)")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="대시보드 실행 (기본)")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--host", default="", help="서버로 쓸 때 0.0.0.0 (로그인 비밀번호 필요)")
    p.add_argument("--no-browser", action="store_true", help="브라우저 자동으로 열지 않기")
    p.add_argument("--no-collect", action="store_true", help="자동 수집 끄기 (화면만)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("collect", help="한 번 수집")
    p.add_argument("--show", type=int, default=20, help="출력할 질문 수")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("check", help="설정/연결 점검")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("classify", help="분류 테스트")
    p.add_argument("title")
    p.add_argument("body", nargs="?")
    p.set_defaults(func=cmd_classify)

    args = parser.parse_args(argv)
    if not args.cmd:
        args = parser.parse_args(["serve", *(argv or sys.argv[1:])])
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
