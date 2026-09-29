#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "처음 실행: 가상환경을 만들고 필요한 패키지를 설치합니다..."
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
exec python -m jisikin serve "$@"
