#!/usr/bin/env bash
# TradingAgents Control Center — one-command setup & launch (macOS / Linux)
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Creating virtual environment (.venv)…"
  python3 -m venv .venv
fi
source .venv/bin/activate
if ! python -c "import tradingagents, fastapi, uvicorn" 2>/dev/null; then
  echo "Installing TradingAgents + dashboard dependencies…"
  pip install -q --upgrade pip
  pip install -q . -r webui/requirements.txt
fi
[ -f .env ] || cp .env.example .env
URL="http://127.0.0.1:${WEBUI_PORT:-8765}"
( sleep 2; (command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true ) &
exec python -m webui.server
