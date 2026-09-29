@echo off
chcp 65001 > nul
cd /d %~dp0
if not exist .venv (
  echo 처음 실행: 가상환경을 만들고 필요한 패키지를 설치합니다...
  python -m venv .venv || (echo Python 3.10 이상을 먼저 설치해 주세요: https://www.python.org/downloads/ & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if not exist .env copy .env.example .env > nul
python -m jisikin serve
pause
