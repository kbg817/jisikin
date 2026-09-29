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


def cmd_serve(args) -> int:
    from .web import AppState, create_app

    state = AppState(config_mod.CONFIG_PATH, config_mod.DB_PATH)
    app = create_app(state)
    port = args.port or state.cfg.settings.port
    url = f"http://127.0.0.1:{port}"
    # 개발용 서버 경고/요청 로그 없이 조용히 실행 (이 PC 에서만 접속 가능: 127.0.0.1)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        server = make_server("127.0.0.1", port, app, threaded=True)
    except (OSError, SystemExit):  # werkzeug 는 포트가 사용 중이면 SystemExit 을 낸다
        print(f"포트 {port} 가 이미 사용 중입니다. 이미 실행 중이 아닌지 확인하거나 config.yaml 의 port 를 바꾸세요.")
        return 1
    print(f"\n  지식iN 질문 수집기 대시보드: {url}\n  (종료: Ctrl+C)\n", flush=True)
    if not args.no_collect:
        state.start_scheduler()
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
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
    from .collector import effective_interval, estimate_api_calls_per_day, resolve_mode
    from .drafter import ai_status
    from .naver import NaverClient, NaverError

    ok = True
    try:
        cfg = config_mod.load_config()
        print(f"[OK] 설정 파일: {config_mod.CONFIG_PATH}")
        for p in cfg.products:
            print(f"     - {p.name}: 키워드 {len(p.keywords)}개, 카테고리 {len(p.categories)}개, 검색어 {len(p.search_queries())}개")
    except ConfigError as e:
        print(f"[오류] 설정 파일: {e}")
        return 1

    creds = config_mod.naver_credentials()
    mode = resolve_mode(cfg)
    print(f"[{'OK' if creds else '--'}] 네이버 API 키: {'있음' if creds else '없음 (웹 검색 모드로 동작)'}")
    print(f"     수집 방식: {mode}, 자동 수집 간격: {effective_interval(cfg)}분")
    if mode == "api":
        calls = estimate_api_calls_per_day(cfg)
        warn = "  ← 하루 한도(25,000회)에 가까움. 간격을 늘리거나 검색어를 줄이세요." if calls > 20000 else ""
        print(f"     예상 API 호출: 하루 약 {calls:,}회{warn}")

    client = NaverClient(credentials=creds, delay_seconds=cfg.settings.request_delay_seconds)
    query = cfg.all_search_queries()[0]
    try:
        items = client.search_api(query, 5) if mode == "api" else client.search_web(query)
        print(f"[{'OK' if items else '??'}] '{query}' 검색: {len(items)}건")
        for it in items[:3]:
            print(f"     · {it.title}  ({it.url})")
        if not items:
            ok = False
            print("     결과가 0건입니다. 웹 모드라면 네이버 화면 구조가 바뀌었을 수 있습니다.")
    except NaverError as e:
        ok = False
        items = []
        print(f"[오류] 검색 실패: {e}")

    if items and cfg.settings.fetch_details:
        try:
            d = client.fetch_detail(items[0].url)
            answers = d.answer_count if d.answer_count is not None else "모름"
            asked = f"{d.asked_at:%Y-%m-%d %H:%M}" if d.asked_at else "모름"
            print(
                f"[{'OK' if d.title else '??'}] 상세 페이지: 제목={'O' if d.title else 'X'} "
                f"본문={'O' if d.body else 'X'} 답변수={answers} 작성일={asked}"
            )
        except NaverError as e:
            print(f"[오류] 상세 페이지: {e}")

    ai_ok, reason = ai_status()
    print(f"[{'OK' if ai_ok else '--'}] AI 답변 초안: {reason}")
    return 0 if ok else 1


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
    parser = argparse.ArgumentParser(prog="python -m jisikin", description="네이버 지식iN 질문 수집기")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="대시보드 실행 (기본)")
    p.add_argument("--port", type=int, default=0)
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
