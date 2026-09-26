@echo off
REM TradingAgents Control Center - one-command setup & launch (Windows)
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -c "import tradingagents, fastapi, uvicorn" 2>nul || (
  echo Installing TradingAgents + dashboard dependencies...
  python -m pip install -q --upgrade pip
  pip install -q . -r webui\requirements.txt
)
if not exist .env copy .env.example .env
set PYTHONUTF8=1
start "" http://127.0.0.1:8765
python -m webui.server
