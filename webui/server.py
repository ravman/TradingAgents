"""TradingAgents Control Center — FastAPI backend.

Run from the repository root:
    python -m webui.server            # http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from dotenv import find_dotenv, set_key
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import tradingagents  # noqa: F401  (loads .env)
from tradingagents.decision_log import TradingMemoryLog
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.model_catalog import get_model_options

from . import auth, screener
from . import engine as eng
from . import market

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webui")

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
ENV_PATH = Path(find_dotenv(usecwd=True) or (ROOT / ".env"))

EXTRA_KEYS = ["FRED_API_KEY", "ALPHA_VANTAGE_API_KEY", "TYPESAFE_API_KEY", "SEC_EDGAR_USER_AGENT",
              "OLLAMA_BASE_URL", "AZURE_OPENAI_API_KEY",
              "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "DIGEST_TO"]

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    global engine
    screener.register()
    hub.loop = asyncio.get_running_loop()
    engine = eng.Engine(hub.broadcast)
    threading.Thread(target=_price_poller, daemon=True, name="prices").start()
    yield


app = FastAPI(title="TradingAgents Control Center", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(auth.AuthMiddleware, static_dir=STATIC)


# ---------------------------------------------------------------- websocket hub
class Hub:
    def __init__(self):
        self.clients: dict[WebSocket, dict] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def broadcast(self, ev: dict):
        """Thread-safe: called from the worker thread."""
        if not self.loop:
            return
        data = json.dumps(ev, default=str)
        for ws, meta in list(self.clients.items()):
            self.loop.call_soon_threadsafe(meta["q"].put_nowait, data)


hub = Hub()
engine: eng.Engine | None = None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    meta = {"q": asyncio.Queue(), "tickers": set()}
    hub.clients[ws] = meta

    async def sender():
        while True:
            await ws.send_text(await meta["q"].get())

    task = asyncio.create_task(sender())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "subscribe":
                meta["tickers"] = set()
                for t in msg.get("tickers", []):
                    try:
                        meta["tickers"].add(market.normalize_ticker(t, allow_index=True))
                    except ValueError:
                        pass
                meta["interval"] = msg.get("interval", "1m")
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        hub.clients.pop(ws, None)


def _price_poller():
    """Push live quotes for the watchlist + subscribed tickers every ~15s."""
    last_equity = 0.0
    while True:
        try:
            wanted = set(engine.settings.get("watchlist", [])) if engine else set()
            for meta in list(hub.clients.values()):
                wanted |= meta.get("tickers", set())
            if hub.clients:
                for t in sorted(wanted):
                    try:
                        q = market.quote(t)
                        hub.broadcast({"type": "quote", **q})
                    except Exception:
                        pass
                subs = {t for m in hub.clients.values() for t in m.get("tickers", set())}
                for t in subs:
                    try:
                        h = market.history(t, "1m", "1d")
                        if h["bars"]:
                            hub.broadcast({"type": "bar", "ticker": t, "interval": "1m", "bar": h["bars"][-1]})
                    except Exception:
                        pass
            if engine and time.time() - last_equity > 600 and engine.book.data["positions"]:
                last_equity = time.time()
                snap = engine.book.mark()
                hub.broadcast({"type": "portfolio", "portfolio": snap})
        except Exception:
            log.exception("poller")
        time.sleep(15)


# ---------------------------------------------------------------- meta / settings
def _providers():
    try:
        from cli.prompts import _llm_provider_table
        table = [(d, k) for d, k, _ in _llm_provider_table()]
    except Exception:
        table = [(k, k) for k in PROVIDER_API_KEY_ENV]
    extra = [("GLM (China)", "glm-cn"), ("Qwen (China)", "qwen-cn"), ("MiniMax (China)", "minimax-cn")]
    out = [{"key": "simulation", "label": "Simulation (no API key, scripted reasoning)", "env": None, "models": {
        "quick": [["Simulated", "simulated"]], "deep": [["Simulated", "simulated"]]}}]
    for label, key in table + extra:
        models = {}
        for mode in ("quick", "deep"):
            try:
                models[mode] = [[a, b] for a, b in get_model_options(key, mode)]
            except Exception:
                models[mode] = [["Custom model ID", "custom"]]
        out.append({"key": key, "label": label, "env": PROVIDER_API_KEY_ENV.get(key), "models": models})
    return out


def _key_status():
    keys = sorted({v for v in PROVIDER_API_KEY_ENV.values() if v} | set(EXTRA_KEYS))
    def mask(v):
        return (v[:4] + "…" + v[-4:]) if len(v) > 12 else ("set" if v else "")
    return [{"name": k, "set": bool(os.environ.get(k)), "masked": mask(os.environ.get(k, ""))} for k in keys]


@app.get("/api/meta")
def meta():
    return {"providers": _providers(), "keys": _key_status(), "settings": engine.settings,
            "pipeline": eng.PIPELINE, "env_path": str(ENV_PATH),
            "results_dir": DEFAULT_CONFIG["results_dir"], "memory_log": DEFAULT_CONFIG["memory_log_path"],
            "vendor_options": {"core_stock_apis": ["yfinance", "alpha_vantage", "yfinance,alpha_vantage"],
                               "technical_indicators": ["yfinance", "alpha_vantage"],
                               "fundamental_data": ["yfinance", "alpha_vantage", "sec_edgar,yfinance"],
                               "news_data": ["yfinance", "alpha_vantage", "yfinance,alpha_vantage"],
                               "macro_data": ["fred"], "prediction_markets": ["polymarket"]}}


@app.post("/api/settings")
def update_settings(body: dict):
    s = engine.settings
    if "watchlist" in body:
        try:
            body["watchlist"] = [market.normalize_ticker(t, allow_index=True) for t in body["watchlist"] if t.strip()]
        except ValueError as e:
            raise HTTPException(400, str(e))
    for k, v in body.items():
        if k in eng.DEFAULT_SETTINGS:
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
    eng.save_settings(s)
    hub.broadcast({"type": "settings", "settings": s})
    return s


class KeyBody(BaseModel):
    name: str
    value: str


@app.post("/api/keys")
def set_api_key(body: KeyBody):
    allowed = {v for v in PROVIDER_API_KEY_ENV.values() if v} | set(EXTRA_KEYS)
    if body.name not in allowed:
        raise HTTPException(400, "Unknown key name")
    val = body.value.strip()
    if not ENV_PATH.exists():
        ENV_PATH.touch()
    set_key(str(ENV_PATH), body.name, val, quote_mode="never")
    if val:
        os.environ[body.name] = val
    else:
        os.environ.pop(body.name, None)
    return {"keys": _key_status()}


# ---------------------------------------------------------------- runs
class RunBody(BaseModel):
    tickers: list[str]
    date: str | None = None
    overrides: dict | None = None


@app.post("/api/runs")
def start_runs(body: RunBody):
    out = []
    for t in body.tickers:
        if t.strip():
            try:
                out.append(engine.submit(t, body.date, body.overrides))
            except ValueError as e:
                raise HTTPException(400, str(e))
    return out


@app.get("/api/runs")
def list_runs():
    runs = sorted(engine.runs.values(), key=lambda r: r["created"], reverse=True)
    return {"current": engine.current, "runs": [engine.summary(r) for r in runs[:300]]}


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    r = engine.runs.get(rid)
    if not r:
        raise HTTPException(404)
    return {**engine.summary(r), "events": r.get("events", []), "reports": r.get("reports", {}),
            "debate": r.get("debate", []), "decision": r.get("decision"), "report_path": r.get("report_path")}


@app.post("/api/runs/{rid}/cancel")
def cancel_run(rid: str):
    return {"ok": engine.cancel(rid)}


@app.post("/api/runs/cancel_all")
def cancel_all():
    engine.cancel_all()
    return {"ok": True}


@app.delete("/api/runs/{rid}")
def delete_run(rid: str):
    r = engine.runs.get(rid)
    if r and r["status"] not in ("queued", "running"):
        engine.runs.pop(rid)
        (eng.RUNS_DIR / f"{rid}.json").unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------- backtest
class BacktestBody(BaseModel):
    tickers: list[str]
    start: str
    end: str
    every: int = 7
    overrides: dict | None = None


@app.post("/api/backtest")
def backtest(body: BacktestBody):
    try:
        return engine.backtest([market.normalize_ticker(t) for t in body.tickers if t.strip()],
                               body.start, body.end, body.every, body.overrides)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/backtest")
def list_backtests():
    return [engine._batch_view(b) for b in sorted(engine.batches.values(), key=lambda b: b["created"], reverse=True)]


# ---------------------------------------------------------------- market / portfolio / log
@app.get("/api/prices/{ticker}")
def prices(ticker: str, interval: str = "1d", period: str | None = None):
    try:
        return market.history(market.normalize_ticker(ticker, allow_index=True), interval, period)
    except Exception as e:
        raise HTTPException(404, str(e))


@app.get("/api/quotes")
def quotes(tickers: str):
    out = []
    for t in tickers.split(","):
        try:
            out.append(market.quote(market.normalize_ticker(t, allow_index=True)))
        except Exception:
            out.append({"ticker": t.strip().upper(), "price": None})
    return out


@app.get("/api/portfolio")
def portfolio():
    return engine.book.snapshot(live=True)


@app.post("/api/portfolio")
def portfolio_update(body: dict):
    if "reset_cash" in body:
        engine.book.reset(float(body["reset_cash"]))
    engine.book.configure(**{k: body.get(k) for k in ("auto_execute", "max_weight", "portfolio_aware", "currency")})
    snap = engine.book.snapshot(live=True)
    hub.broadcast({"type": "portfolio", "portfolio": snap})
    return snap


class ManualTrade(BaseModel):
    ticker: str
    rating: str
    date: str | None = None


@app.post("/api/portfolio/apply")
def portfolio_apply(body: ManualTrade):
    from datetime import datetime
    try:
        trade = engine.book.apply_rating(body.ticker, body.date or datetime.now().strftime("%Y-%m-%d"),
                                         body.rating, "manual")
    except ValueError as e:
        raise HTTPException(400, str(e))
    snap = engine.book.snapshot(live=False)
    hub.broadcast({"type": "trade", "trade": trade, "portfolio": snap, "run_id": None, "ts": time.time()})
    return {"trade": trade, "portfolio": snap}


@app.get("/api/decisions")
def decisions(path: str | None = None):
    p = path or DEFAULT_CONFIG["memory_log_path"]
    entries = TradingMemoryLog({"memory_log_path": p}).load_entries()
    return list(reversed(entries))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    host = os.environ.get("WEBUI_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBUI_PORT", "8765"))
    if host not in ("127.0.0.1", "localhost", "::1") and not auth.enabled():
        raise SystemExit("Refusing to bind a non-loopback address without a login: run `python -m webui.auth set-password`.")
    print(f"\n  TradingAgents Control Center → http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
