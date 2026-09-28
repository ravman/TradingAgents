# TradingAgents Control Center: project handoff

A summary of the chat that produced this code (Sept 26–28, 2026), written so you, or Claude Code, can pick it up and keep maintaining it in your own repository.

---

## 1. What the upstream repo is

**TauricResearch/TradingAgents**, v0.5.1 (upstream commit `35543d0`, Sept 24, 2026), is a LangGraph multi-agent LLM framework. For one ticker and one date it produces a rating from a five-step scale: **Buy / Overweight / Hold / Underweight / Sell** (or `REVIEW` when no rating can be parsed).

Pipeline (`tradingagents/graph/setup.py`):

1. **Analysts** (quick-think model) gather data and write reports:
   - Market / technical, with tools (`get_stock_data`, `get_indicators`, `get_verified_market_snapshot`)
   - Sentiment, with no tools: it pre-fetches StockTwits, Reddit and news
   - News & macro, with tools (news, global news, FRED macro, Polymarket)
   - Fundamentals, with tools (fundamentals, balance sheet, cash flow, income statement, insider transactions)
2. **Bull and Bear Researchers** debate, and the **Research Manager** (deep-think model) rules.
3. The **Trader** proposes Buy, Hold or Sell.
4. **Aggressive, Conservative and Neutral** risk analysts debate, and the **Portfolio Manager** (deep-think model) gives the final rating.

Other upstream features:
- A decision log at `~/.tradingagents/memory/trading_memory.md`. Each decision is settled on the next same-ticker run (realised return and alpha against a regional benchmark), and the reflections are fed back into later runs.
- Point-in-time data integrity, including SEC EDGAR fundamentals "as filed".
- `run_backtest` over a ticker × date grid.
- Portfolio-aware runs (`PortfolioContext`).
- Checkpoint resume.
- A long list of LLM providers.
- Optional Jev screening of social posts: `TYPESAFE_API_KEY` switches it on, and it only affects the Sentiment Analyst.

**Assessment:** well engineered, and the test suite is strong (1,004 tests passed here). It is a **research and decision tool with no broker or order execution**. Output varies from run to run, and a real run costs about 15–40 LLM calls. Upstream says it is not financial advice.

## 2. What was built: `webui/` (the "Control Center")

It's a local FastAPI + WebSocket server with a vanilla-JS single-page UI. It drives `TradingAgentsGraph` and streams every step live. The custom code is **only** the files below. **No upstream file was modified.**

```
webui/__init__.py
webui/server.py        FastAPI app: REST + /ws WebSocket hub; 15 s quote/1-minute-bar poller; key management (.env)
webui/engine.py        Engine: run queue + single worker thread, EventCapture (LangChain callback), graph streaming,
                       backtest batches, daily scheduler, settings persistence
webui/paper.py         PaperBook: paper-trading ledger (ratings → simulated fills)
webui/market.py        Yahoo Finance OHLCV + indicators (SMA20/50, EMA, MACD, BB, RSI), quotes, close-on-date
webui/simulation.py    SimulationChatModel: scripted LLM for the no-cost demo mode (real graph, tools and data)
webui/static/index.html, app.js, app.css   UI (lightweight-charts 4.2, marked, DOMPurify from jsdelivr)
webui/requirements.txt fastapi, uvicorn[standard]
webui/README.md        user-facing docs
webui/HANDOFF.md       this file
start_dashboard.sh / start_dashboard.bat   one-command setup + launch (creates .venv, pip install, copies .env)
```

### UI screens
- **Live:**
  - Candlestick chart (1m–1W) with SMA and Bollinger overlays; RSI / MACD / volume pane below; markers for decisions and paper fills; live updates every 15 s.
  - Agent-activity swimlane timeline (SVG): active time, LLM calls and tool calls per agent.
  - 12-agent pipeline status; token, call and elapsed tiles plus a cumulative-token chart.
  - Live feed (reasoning, tool calls, raw tool output, debate), with filters.
  - Report tabs, including the Bull vs Bear and risk debates as chat bubbles.
- **Portfolio & Trades:** equity, cash and return tiles; equity curve; positions; trade ledger; execution rules; manual paper order; reset.
- **History:** every run (click one to replay it in Live), plus the upstream decision log with realised return and alpha.
- **Backtest:** ticker × date grid queued as normal runs into a separate log; progress grid; summary of hit rate and mean alpha per rating.
- **Settings:** provider and models (from the upstream model catalog, plus custom IDs), backend URL, reasoning effort, temperature, language, analysts, research depth (1/3/5 rounds), checkpoint, data vendors, simulation speed, API keys (written to `.env`), watchlist, daily auto-run schedule.

### Key design decisions (keep these unless there's a reason)
1. **Zero upstream edits.** The dashboard imports `tradingagents` and `cli` as a library, so upstream updates can be pulled without conflicts.
2. **Its own streaming loop that mirrors `cli/run.py`.** The sequence is:
   - `create_run_state`
   - `get_graph_args(callbacks=[EventCapture])`
   - `begin_checkpoint`
   - `graph.graph.stream(checkpoint_input(init), stream_mode="values")`
   - `record_decision`, then `clear_checkpoint_on_success`, then `end_checkpoint`
   - `_log_state` and `save_reports`
   - `process_signal`

   All of this runs inside `run_config(cfg)`.
3. **Event capture through a LangChain callback.** It reads `metadata["langgraph_node"]` to attribute LLM and tool calls to agents. Node starts are detected when `on_chain_start` has `name == langgraph_node`. Reports and debates are diffed from the streamed state (bull/bear/aggressive/conservative/neutral histories, `judge_decision`).
4. **One worker thread; runs are sequential.** Upstream `set_config` is process-global, and provider rate limits apply anyway.
5. **Cancellation is cooperative.** It is checked on every callback and stream chunk, so an LLM call already in flight finishes first.
6. **Simulation provider.** It temporarily monkeypatches `trading_graph.create_llm_client`, under a lock, only while the graph is being built. The ratings are weighted by 20-day momentum and every text is tagged `[SIMULATED — not real analysis]`. Simulated runs are flagged `simulated: true` and show a yellow **SIM** badge.
7. **Paper ledger only.** Fills happen at the close on the analysis date (or the live quote if the date is today).

   | Rating | Sizing rule (target weight = `max_weight` × equity) |
   |---|---|
   | Buy | target `max_weight` |
   | Overweight | half of `max_weight`, adds only |
   | Hold | no change |
   | Underweight | halve the position |
   | Sell | close the position |

   Stocks trade in whole shares; tickers ending in `-USD` (crypto) trade in 0.0001 units. Cash is never allowed below zero. When "portfolio-aware" is on, the book is passed to the agents as `PortfolioContext`.
8. **Backtests** run as ordinary queued runs, with `results_dir` and `memory_log_path` overridden to `~/.tradingagents/logs/backtest/<bt_id>/`. When the last cell finishes, the engine calls `settle_pending` per ticker, then upstream `summarize()`. Backtest runs never touch the paper ledger (`_no_paper`).
9. **Security:** the server binds `127.0.0.1` by default and has no auth. Keys are stored in `.env` via `python-dotenv`'s `set_key`; only known key names are allowed.

### Upstream APIs this code depends on
Re-check these after pulling upstream; they are where breakage would come from:
- `tradingagents.graph.trading_graph`:
  - `TradingAgentsGraph` and its methods `create_run_state`, `begin_checkpoint`, `checkpoint_input`, `end_checkpoint`, `record_decision`, `clear_checkpoint_on_success`, `save_reports`, `process_signal`, `settle_pending`
  - the attributes `propagator`, `graph`, `_resuming` (private)
  - `_log_state` (private) and `_validate_trade_date` (private)
  - the module-level `create_llm_client`, which the simulation monkeypatches
- `tradingagents.dataflows.config.run_config`; `tradingagents.default_config.DEFAULT_CONFIG`
- `tradingagents.agents.rating.is_review`; `tradingagents.agents.schemas` (ResearchPlan, TraderProposal, PortfolioDecision, SentimentReport, used by the simulation)
- `tradingagents.backtest.iter_grid`, `summarize`; `tradingagents.decision_log.TradingMemoryLog.load_entries`
- `tradingagents.portfolio.PortfolioContext`
- `tradingagents.llm_clients.api_key_env.PROVIDER_API_KEY_ENV` / `get_api_key_env`; `tradingagents.llm_clients.model_catalog.get_model_options`
- `cli.prompts._llm_provider_table` (private), `provider_default_url`
- Agent node names (`Market Analyst` … `Portfolio Manager`, `tools_<key>`) and the `AgentState` / debate-state keys

### Event schema (WebSocket `/ws`, JSON)
Every run event carries `run_id` and `ts`. The types:
- **Run lifecycle:** `run_queued`, `run_started` (with `pipeline`), `run_finished`, `run_failed` (with `trace`), `run_cancelled`
- **Agent activity:** `node_start` (agent), `llm_start`, `llm_end` (tokens, duration, `stats`), `llm_error`, `tool_start` (tool, args), `tool_end` (output ≤6,000 chars), `tool_error`
- **Content:** `message` (reasoning or tool request), `report` (key, content), `debate` (team: research/risk; speaker; content), `context`, `memory`, `log`
- **Outcome:** `decision` (signal, text), `trade`

Global events have no `run_id`: `quote`, `bar` (1-minute bars for the subscribed ticker), `portfolio`, `batch`, `settings`. The client sends `{"type":"subscribe","tickers":[…]}`.

### REST endpoints
- **Setup:** `GET /api/meta`, `POST /api/settings`, `POST /api/keys`
- **Runs:** `POST /api/runs {tickers,date,overrides}`, `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`, `POST /api/runs/cancel_all`, `DELETE /api/runs/{id}`
- **Backtests:** `POST/GET /api/backtest`
- **Market data:** `GET /api/prices/{ticker}?interval=`, `GET /api/quotes?tickers=`
- **Portfolio:** `GET/POST /api/portfolio`, `POST /api/portfolio/apply`
- **Decision log:** `GET /api/decisions`

### Where data is stored
- **Dashboard:** `~/.tradingagents/webui/`, holding `settings.json`, `runs/<id>.json` (the full event log per run), `paper_portfolio.json` and `batches.json`. Override with `TRADINGAGENTS_WEBUI_HOME`.
- **Upstream (unchanged):** reports and state logs in `~/.tradingagents/logs`; decision log in `~/.tradingagents/memory/trading_memory.md`.
- **Server:** `WEBUI_HOST` (default `127.0.0.1`) and `WEBUI_PORT` (default `8765`).

## 3. Running it

```bash
git clone <your fork of TradingAgents> && cd TradingAgents   # webui/ + start scripts included on your branch
./start_dashboard.sh            # macOS/Linux
start_dashboard.bat             # Windows
# manual: python -m venv .venv && . .venv/bin/activate && pip install . -r webui/requirements.txt && python -m webui.server
```

Open http://127.0.0.1:8765. It starts in **Simulation** mode. Then, in **Settings**, choose a provider and model, save the API key and run a ticker. Optional keys:
- `FRED_API_KEY` (macro data)
- `ALPHA_VANTAGE_API_KEY`
- `TYPESAFE_API_KEY` (Jev)
- `SEC_EDGAR_USER_AGENT`

## 4. What was verified (in a Linux cloud sandbox, Python 3.11)
- The upstream test suite: 1,004 passed, 2 skipped. It still passes with `webui/` added.
- Simulated end-to-end runs using real Yahoo Finance data and real tool calls. All 12 agents, 7 reports, 7 debate turns, the decision, the paper trade and the saved reports were checked.
- Cancel (mid-run and queued); the missing-API-key error path; crypto (`BTC-USD`, where fundamentals are skipped and fills are fractional).
- A backtest (2 tickers × 3 dates): settlement and summary work, and the batch survives a server restart.
- `start_dashboard.sh` from a clean copy; headless Chromium screenshots of every screen, in dark and light and at 390 px mobile width, with no console errors.
- The OpenAI, Anthropic and OpenRouter APIs were reachable. **No real-LLM run was done**, because no API key was provided.

## 5. Known limitations and gotchas
- There are **no real trades**. Connecting a broker would be new work and is not advisable to automate, given how much the output varies.
- Reddit or StockTwits may rate-limit (HTTP 429). The Sentiment Analyst then backs off for about 60 s, which shows up as a long bar on the timeline.
- `stats.llm_calls` does not count structured-output calls in simulation mode (the `RunnableLambda` path). Real providers are counted normally.
- `llm_start.model` can be `None` for some providers (it reads `ls_model_name`).
- Run history and events are kept in memory as well as on disk. Delete old runs in History if the store grows large.
- The daily schedule only fires while the server is running.
- The front end loads its chart and markdown libraries from the jsdelivr CDN and fonts from Google Fonts, so it needs internet access; vendor them to run fully offline.
- `pyflakes` reports one expected warning: `import tradingagents  # noqa` in `server.py`, which is there to load `.env`.

## 6. Git state at handoff
- The local branch `webui-control-center` holds commit `0d53fd6` ("Add TradingAgents Control Center web dashboard"), on top of upstream `35543d0` (v0.5.1).
- It was **not pushed**. The only remote was upstream TauricResearch, which is not yours. The code was delivered as `tradingagents-control-center.zip` (unzip it into the repo root).
- **Recommended setup for managing the custom code:**
  1. Fork TauricResearch/TradingAgents to your GitHub account and clone the fork.
  2. `git remote add upstream https://github.com/TauricResearch/TradingAgents`
  3. `git checkout -b webui-control-center`, unzip the files into the repo root, then commit and push to your fork.
  4. To update: `git fetch upstream && git rebase upstream/main`. Conflicts should be rare because only new files were added. Then run `pytest -q` and one simulated run from the dashboard.

## 7. Possible next steps
- Do a first real-LLM run and check the event attribution (the model name, and the structured-output calls counted).
- Vendor the CDN libraries for offline use; add optional basic auth if it is ever exposed beyond localhost.
- Add tests for `webui/` (engine event reduction, paper sizing, API) using upstream's `ScriptedModel` pattern from `tests/test_graph_end_to_end.py`.
- Add CSV export of the ledger, runs and backtests; add per-run cost estimates by model.
- A `docker-compose` service for the dashboard (upstream already has a Dockerfile).
