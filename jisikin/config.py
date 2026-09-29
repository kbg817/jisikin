"""설정 파일(config.yaml) 로딩과 검증."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.yaml"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jisikin.db"

DEFAULT_AI_MODEL = "claude-opus-5-5"


class ConfigError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 설정 오류."""


@dataclass
class Category:
    name: str
    keywords: list[str]
    search: list[str] = field(default_factory=list)


@dataclass
class Product:
    id: str
    name: str
    keywords: list[str]
    categories: list[Category] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    watch_urls: list[str] = field(default_factory=list)
    min_score: float = 2
    url: str = ""
    color: str = "#2563eb"
    answer_guide: str = ""

    def search_queries(self) -> list[str]:
        queries = list(self.keywords)
        for cat in self.categories:
            queries.extend(cat.search)
        return _dedupe(queries)


@dataclass
class Settings:
    interval_minutes: int = 10
    results_per_keyword: int = 50
    source: str = "auto"
    fetch_details: bool = True
    max_details_per_run: int = 40
    request_delay_seconds: float = 1.0
    max_age_days: int = 14
    keep_days: int = 60
    port: int = 5000


@dataclass
class AISettings:
    model: str = DEFAULT_AI_MODEL
    effort: str = "medium"
    common_guide: str = ""


@dataclass
class AppConfig:
    settings: Settings
    ai: AISettings
    products: list[Product]

    def product(self, product_id: str | None) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)

    def all_search_queries(self) -> list[str]:
        queries: list[str] = []
        for p in self.products:
            queries.extend(p.search_queries())
        return _dedupe(queries)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = re.sub(r"\s+", " ", item).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ConfigError(f"{where} 는 목록([a, b, c]) 형식이어야 합니다.")
    out = []
    for v in value:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _validate_patterns(patterns: list[str], where: str) -> None:
    for pat in patterns:
        raw = pat[1:] if pat.startswith("!") else pat
        if raw.startswith("re:"):
            try:
                re.compile(raw[3:])
            except re.error as e:
                raise ConfigError(f"{where} 의 정규식 '{raw}' 가 잘못되었습니다: {e}") from e


def _typed(section: dict, cls, where: str):
    obj = cls()
    for key, value in (section or {}).items():
        if not hasattr(obj, key):
            continue  # 모르는 항목은 무시 (오타 때문에 실행이 막히지 않게)
        default = getattr(obj, key)
        try:
            if isinstance(default, bool):
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on", "예")
                else:
                    value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
            else:
                value = "" if value is None else str(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{where}.{key} 값 '{value}' 가 올바르지 않습니다.") from e
        setattr(obj, key, value)
    return obj


def parse_config(text: str) -> AppConfig:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 형식 오류: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("설정 파일의 최상위는 settings / ai / products 항목이어야 합니다.")

    settings = _typed(data.get("settings") or {}, Settings, "settings")
    if settings.source not in ("auto", "api", "web"):
        raise ConfigError("settings.source 는 auto, api, web 중 하나여야 합니다.")
    settings.results_per_keyword = max(1, min(settings.results_per_keyword, 100))
    settings.request_delay_seconds = max(0.0, settings.request_delay_seconds)

    ai = _typed(data.get("ai") or {}, AISettings, "ai")
    if ai.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("ai.effort 는 low, medium, high 중 하나여야 합니다.")

    raw_products = data.get("products") or []
    if not isinstance(raw_products, list) or not raw_products:
        raise ConfigError("products 에 제품을 하나 이상 등록해 주세요.")

    products: list[Product] = []
    ids: set[str] = set()
    for i, rp in enumerate(raw_products, start=1):
        if not isinstance(rp, dict):
            raise ConfigError(f"products 의 {i}번째 항목 형식이 올바르지 않습니다.")
        name = str(rp.get("name") or "").strip()
        if not name:
            raise ConfigError(f"products 의 {i}번째 제품에 name 이 없습니다.")
        pid = str(rp.get("id") or "").strip() or f"p{i}"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
            raise ConfigError(f"'{name}' 의 id 는 영문/숫자/_/- 만 사용할 수 있습니다.")
        if pid in ids:
            raise ConfigError(f"제품 id '{pid}' 가 중복되었습니다.")
        ids.add(pid)
        where = f"'{name}'"

        keywords = _str_list(rp.get("keywords"), f"{where}.keywords")
        exclude = _str_list(rp.get("exclude"), f"{where}.exclude")
        watch_urls = _str_list(rp.get("watch_urls"), f"{where}.watch_urls")
        categories = []
        for j, rc in enumerate(rp.get("categories") or [], start=1):
            if not isinstance(rc, dict) or not str(rc.get("name") or "").strip():
                raise ConfigError(f"{where} 의 {j}번째 카테고리에 name 이 없습니다.")
            cname = str(rc["name"]).strip()
            ckw = _str_list(rc.get("keywords"), f"{where} > {cname}.keywords")
            csearch = _str_list(rc.get("search"), f"{where} > {cname}.search")
            if not ckw:
                ckw = [cname]
            _validate_patterns(ckw, f"{where} > {cname}")
            categories.append(Category(name=cname, keywords=ckw, search=csearch))
        if not keywords and not categories:
            raise ConfigError(f"{where} 에 keywords 또는 categories 를 하나 이상 넣어 주세요.")
        _validate_patterns(keywords, where)
        _validate_patterns(exclude, where)
        for u in watch_urls:
            if not u.startswith("http"):
                raise ConfigError(f"{where}.watch_urls 의 '{u}' 는 http 로 시작하는 주소여야 합니다.")
        try:
            min_score = float(rp.get("min_score", 2))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{where}.min_score 는 숫자여야 합니다.") from e

        products.append(
            Product(
                id=pid,
                name=name,
                keywords=keywords,
                categories=categories,
                exclude=exclude,
                watch_urls=watch_urls,
                min_score=min_score,
                url=str(rp.get("url") or "").strip(),
                color=str(rp.get("color") or "#2563eb").strip(),
                answer_guide=str(rp.get("answer_guide") or "").strip(),
            )
        )
    return AppConfig(settings=settings, ai=ai, products=products)


def ensure_config_file(path: Path = CONFIG_PATH) -> Path:
    if not path.exists():
        shutil.copyfile(EXAMPLE_CONFIG_PATH, path)
    return path


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    ensure_config_file(path)
    return parse_config(path.read_text(encoding="utf-8"))


def read_config_text(path: Path = CONFIG_PATH) -> str:
    ensure_config_file(path)
    return path.read_text(encoding="utf-8")


def save_config_text(text: str, path: Path = CONFIG_PATH) -> AppConfig:
    """검증에 통과한 경우에만 저장한다."""
    cfg = parse_config(text)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return cfg


def load_env(path: Path = ENV_PATH) -> None:
    """.env 파일의 KEY=VALUE 를 환경변수로 읽는다. (이미 설정된 값은 덮어쓰지 않음)"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def naver_credentials() -> tuple[str, str] | None:
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None
