import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jisikin.config import EXAMPLE_CONFIG_PATH, parse_config  # noqa: E402


@pytest.fixture
def example_cfg():
    return parse_config(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    # 개발자 PC 의 실제 키로 네트워크 요청이 나가지 않게
    for key in (
        "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "NAVER_AD_CUSTOMER_ID", "NAVER_AD_ACCESS_LICENSE", "NAVER_AD_SECRET_KEY", "JISIKIN_ADMIN_PASSWORD",
        "YOUTUBE_API_KEY", "THREADS_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    from jisikin import naver

    naver._provider_cache.clear()  # 키 발급처 자동 판별 기록은 테스트마다 새로
