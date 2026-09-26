# TradingAgents Control Center

A local web dashboard that drives [TradingAgents](https://github.com/TauricResearch/TradingAgents) and streams everything it does, live.

It is an add-on: it imports the `tradingagents` package unchanged and adds a `webui/` folder plus two launcher scripts.

## Quick start

From the TradingAgents repository root (the folder that contains `pyproject.toml`):

```bash
# macOS / Linux
./start_dashboard.sh

# Windows
start_dashboard.bat
```

The script creates `.venv`, installs TradingAgents and the dashboard's two extra packages (FastAPI and Uvicorn), copies `.env.example` to `.env` if needed, and opens **http://127.0.0.1:8765**.

To do it by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate      # Python 3.10+ (3.12 recommended)
pip install . -r webui/requirements.txt
python -m webui.server
```

## First run

1. The dashboard starts in **Simulation** mode. Click **Run analysis** to watch the whole pipeline work. The graph, tool calls and market data are real, but the reasoning is scripted, so nothing costs money. Simulated runs carry a yellow **SIM** badge and must not be traded on.
2. Open **Settings**, pick your LLM provider (OpenAI, Anthropic, Google, xAI, DeepSeek, OpenRouter, Ollama, …), choose the quick-think and deep-think models, paste the API key under **API keys** and click **Save settings**.
3. Run a ticker. A full real run makes roughly 15–40 LLM calls, depending on the research depth.

## What each screen does

| Screen | What you get |
|---|---|
| **Live** | Candlestick chart with SMA / Bollinger overlays, RSI / MACD / volume pane, and markers for every decision and paper fill. It updates every 15 s. You also get a live agent-activity timeline (each agent's active time, LLM calls and tool calls), the 12-agent pipeline status, token and call counters, a live feed of reasoning, tool calls and raw data returned, and tabs for each report plus the Bull vs Bear and risk debates. |
| **Portfolio & Trades** | A paper ledger that auto-applies each rating using a visible sizing rule, with an equity curve, positions, the trade ledger, manual paper orders and a reset. "Portfolio-aware" passes your paper holdings to the agents. |
| **History** | Every run (click one to replay it in Live), plus TradingAgents' own decision log with realised return and alpha once each holding window has passed. |
| **Backtest** | Runs a ticker × date grid through the full pipeline into a separate log, then scores hit rate and mean alpha for each rating. |
| **Settings** | Provider, models, reasoning effort, temperature, analysts, debate depth, data vendors, checkpoint resume, API keys (written to `.env`), watchlist, and a daily auto-run schedule. |

## Important limits

* **No real trading.** TradingAgents outputs a rating (Buy / Overweight / Hold / Underweight / Sell), not orders. The dashboard's "trades" are simulated fills at the close on the analysis date. Nothing here connects to a broker. The project itself says it is a research tool and not financial advice.
* **Stop / Cancel** takes effect at the next step. An LLM call already in flight finishes first.
* Runs execute one at a time. The framework's data config is process-global, and the provider's rate limits apply anyway.
* The server listens on `127.0.0.1` only. It has no authentication, so don't expose it (`WEBUI_HOST=0.0.0.0`) on a network you don't trust.
* Reddit and StockTwits sometimes rate-limit anonymous requests. When that happens the Sentiment Analyst waits about a minute before retrying, and the timeline shows the wait.

## Files

```
webui/server.py      FastAPI app: REST + WebSocket, live quote/bar poller
webui/engine.py      run queue, streaming of graph/LLM/tool events, backtests, scheduler
webui/paper.py       paper-trading ledger
webui/market.py      Yahoo Finance prices + indicators
webui/simulation.py  scripted LLM for the no-cost demo mode
webui/static/        the UI (index.html, app.js, app.css)
```

Data lives in `~/.tradingagents/webui/` (settings, run history, paper ledger). Set `TRADINGAGENTS_WEBUI_HOME` to move it. TradingAgents' own reports and decision log stay in `~/.tradingagents/logs` and `~/.tradingagents/memory`, as they do from the CLI.

Environment: `WEBUI_HOST` (default `127.0.0.1`) and `WEBUI_PORT` (default `8765`).
