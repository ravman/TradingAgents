# CLAUDE.md

This is a fork of TauricResearch/TradingAgents. It adds a custom local web dashboard, the "Control Center", in `webui/`.

**Read `webui/HANDOFF.md` first.** It covers the architecture, the event schema, the upstream APIs we depend on, what has been verified, and the known limitations.

## Rules for this repo
- **The custom code lives only in `webui/`, `start_dashboard.sh`, `start_dashboard.bat` and this file.** Don't edit upstream files (`tradingagents/`, `cli/`, `tests/`) unless explicitly asked. Keeping them untouched lets upstream updates rebase cleanly.
- When upstream changes, check the "Upstream APIs this code depends on" list in `webui/HANDOFF.md`, especially the private members `_log_state`, `_resuming`, `_validate_trade_date`, `cli.prompts._llm_provider_table` and `trading_graph.create_llm_client`.
- Runs are sequential on one worker thread, because upstream config is process-global. Don't parallelise runs.
- The paper ledger is simulated. Never add real order execution without an explicit request.
- Simulated output must stay clearly labelled (`[SIMULATED …]` text, the `simulated` flag, the SIM badge).

## Commands
```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install . -r webui/requirements.txt pytest pytest-subtests
python -m webui.server                            # http://127.0.0.1:8765  (or ./start_dashboard.sh)
pytest -q                                         # upstream suite, should stay green
```
Quick smoke test with no API key:
1. `POST /api/settings {"llm_provider":"simulation","quick_think_llm":"simulated","deep_think_llm":"simulated","sim_delay":0.1}`
2. `POST /api/runs {"tickers":["NVDA"]}`
3. Poll `GET /api/runs` until the run's status is `done`.

## Git
- Remotes: `origin` is your fork; `upstream` is TauricResearch/TradingAgents.
- Update: `git fetch upstream && git rebase upstream/main`, then `pytest -q` and one simulated run.
