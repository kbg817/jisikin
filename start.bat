@echo off
setlocal
cd /d "%~dp0"
title 지식iN 질문 수집기

rem ---------------------------------------------------------------
rem  1) Python 3.10 이상 찾기 (없으면 winget 으로 자동 설치)
rem ---------------------------------------------------------------
call :find_python
if not defined PYEXE (
  echo Python 이 없어 자동으로 설치합니다. 1~3분 정도 걸립니다...
  echo ^(설치 허용 창이 뜨면 '예' 를 눌러주세요^)
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  call :find_python
)
if not defined PYEXE (
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  call :find_python
)
if not defined PYEXE (
  echo.
  echo [안내] Python 자동 설치에 실패했습니다.
  echo 열리는 페이지에서 Python 을 설치해 주세요.
  echo 설치 첫 화면 맨 아래 "Add python.exe to PATH" 를 꼭 체크한 뒤 Install Now 를 누르세요.
  echo 설치가 끝나면 이 파일^(start.bat^)을 다시 실행하세요.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

rem ---------------------------------------------------------------
rem  2) 가상환경 + 패키지 설치 (처음 한 번)
rem ---------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo 처음 실행: 필요한 프로그램을 설치합니다. 1~2분 정도 걸립니다...
  "%PYEXE%" %PYARGS% -m venv .venv
  if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
if not exist ".env" copy ".env.example" ".env" > nul

rem ---------------------------------------------------------------
rem  3) 실행 (이 창을 닫으면 프로그램이 종료됩니다)
rem ---------------------------------------------------------------
".venv\Scripts\python.exe" -m jisikin serve
pause
exit /b 0

:fail
echo.
echo [오류] 설치 중 문제가 생겼습니다. 인터넷 연결을 확인하고 다시 실행해 주세요.
echo 계속 안 되면 이 창의 내용을 캡처해서 알려주세요.
pause
exit /b 1

rem Python 3.10+ 을 찾아 PYEXE / PYARGS 에 넣는다
:find_python
set "PYEXE="
set "PYARGS="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" > nul 2>&1
if not errorlevel 1 (
  set "PYEXE=python"
  exit /b 0
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" > nul 2>&1
if not errorlevel 1 (
  set "PYEXE=py"
  set "PYARGS=-3"
  exit /b 0
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
  if exist "%%~D\python.exe" (
    "%%~D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" > nul 2>&1
    if not errorlevel 1 set "PYEXE=%%~D\python.exe"
  )
)
exit /b 0
