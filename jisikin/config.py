"""설정 파일(config.yaml) 로딩과 검증."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG_PATH = ROOT / "config.example.yaml"
EXAMPLE_ENV_PATH = ROOT / ".env.example"

# 서버(클라우드)에서는 JISIKIN_DATA_DIR 한 곳(영구 디스크)에 설정·키·DB 를 모두 둔다.
# PC 에서는 기존처럼 프로그램 폴더의 config.yaml / .env / data/jisikin.db 를 쓴다.
_SERVER_DATA_DIR = os.environ.get("JISIKIN_DATA_DIR", "").strip()
DATA_DIR = Path(_SERVER_DATA_DIR) if _SERVER_DATA_DIR else ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.yaml" if _SERVER_DATA_DIR else ROOT / "config.yaml"
ENV_PATH = DATA_DIR / ".env" if _SERVER_DATA_DIR else ROOT / ".env"
DB_PATH = DATA_DIR / "jisikin.db"

ENV_KEYS = (
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "ANTHROPIC_API_KEY",
    "NAVER_AD_CUSTOMER_ID",
    "NAVER_AD_ACCESS_LICENSE",
    "NAVER_AD_SECRET_KEY",
)

DEFAULT_AI_MODEL = "claude-sonnet-5-5"


class ConfigError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 설정 오류."""


@dataclass
class Category:
    name: str
    keywords: list[str]
    search: list[str] = field(default_factory=list)


@dataclass
class Exposure:
    """상위노출 글을 확인할 검색어. keywords + (regions × region_terms 조합).

    seeds 는 [검색어 관리]의 메인 키워드 기본값 (세부 검색어 자동 생성용, 여기서 바로 검색하지는 않음).
    """

    seeds: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    region_terms: list[str] = field(default_factory=list)

    def queries(self) -> list[str]:
        combos = [f"{r} {t}" for r in self.regions for t in self.region_terms]
        return _dedupe(list(self.keywords) + combos)


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
    exposure: Exposure = field(default_factory=Exposure)

    def search_queries(self) -> list[str]:
        queries = list(self.keywords)
        for cat in self.categories:
            queries.extend(cat.search)
        return _dedupe(queries)


EXPOSURE_SOURCES = {"pc": "통합검색 PC", "mobile": "통합검색 모바일", "kin": "지식iN탭"}


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
    api_daily_limit: int = 20000  # 네이버 검색 API 하루 호출 상한 (무료 25,000회를 넘지 않게). 0 이면 상한 없음
    exposure_interval_hours: int = 12
    exposure_top_n: int = 5
    exposure_sources: list[str] = field(default_factory=lambda: ["pc", "mobile", "kin"])


@dataclass
class AISettings:
    model: str = DEFAULT_AI_MODEL
    effort: str = "low"
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

    def exposure_targets(self) -> list[tuple[Product, str]]:
        return [(p, q) for p in self.products for q in p.exposure.queries()]


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
        if isinstance(default, list):
            continue  # 목록 항목은 호출한 쪽에서 따로 검증
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
    settings.exposure_top_n = max(1, min(settings.exposure_top_n, 20))
    raw_sources = (data.get("settings") or {}).get("exposure_sources")
    if raw_sources is not None:
        sources = _str_list(raw_sources, "settings.exposure_sources")
        bad = [s for s in sources if s not in EXPOSURE_SOURCES]
        if bad:
            raise ConfigError(f"settings.exposure_sources 에는 pc, mobile, kin 만 쓸 수 있습니다. (잘못된 값: {', '.join(bad)})")
        settings.exposure_sources = list(dict.fromkeys(sources))

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
        rx = rp.get("exposure") or {}
        if not isinstance(rx, dict):
            raise ConfigError(f"{where}.exposure 는 seeds / keywords / regions / region_terms 항목을 가져야 합니다.")
        exposure = Exposure(
            seeds=_str_list(rx.get("seeds"), f"{where}.exposure.seeds"),
            keywords=_str_list(rx.get("keywords"), f"{where}.exposure.keywords"),
            regions=_str_list(rx.get("regions"), f"{where}.exposure.regions"),
            region_terms=_str_list(rx.get("region_terms"), f"{where}.exposure.region_terms"),
        )
        if exposure.regions and not exposure.region_terms:
            raise ConfigError(f"{where}.exposure 에 regions 를 넣었다면 region_terms (예: [건선, 건선 피부과]) 도 넣어 주세요.")

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
                exposure=exposure,
            )
        )
    return AppConfig(settings=settings, ai=ai, products=products)


def ensure_config_file(path: Path = CONFIG_PATH) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
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


def _env_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip().removeprefix("export ").strip()


def save_env_values(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """키 값을 .env 에 저장하고 바로 적용한다. 다른 줄(주석 등)은 그대로 둔다."""
    clean: dict[str, str] = {}
    for key, value in values.items():
        value = (value or "").strip().strip('"').strip("'")
        if re.search(r"\s", value):
            raise ConfigError(f"{key} 값에 공백이나 줄바꿈이 들어있습니다. 키만 정확히 붙여넣어 주세요.")
        clean[key] = value

    if path.exists():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    elif EXAMPLE_ENV_PATH.exists():
        lines = EXAMPLE_ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    else:
        lines = []
    remaining = dict(clean)
    out = []
    for line in lines:
        key = _env_key(line)
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

    for key, value in clean.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def masked_env(keys=ENV_KEYS) -> dict[str, str]:
    """화면 표시용: 저장된 키를 '••••abcd' 형태로."""
    out = {}
    for key in keys:
        v = os.environ.get(key, "").strip()
        out[key] = ("••••" + v[-4:]) if len(v) > 4 else ("••••" if v else "")
    return out


def naver_credentials() -> tuple[str, str] | None:
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def searchad_credentials() -> tuple[str, str, str] | None:
    """네이버 검색광고 API (월간 검색수 조회용, 선택)."""
    values = tuple(os.environ.get(k, "").strip() for k in ("NAVER_AD_CUSTOMER_ID", "NAVER_AD_ACCESS_LICENSE", "NAVER_AD_SECRET_KEY"))
    return values if all(values) else None  # type: ignore[return-value]
