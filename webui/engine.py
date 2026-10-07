"""Run engine: queues TradingAgents runs, streams every step as an event."""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents.agents.rating import is_review
from tradingagents.dataflows.config import run_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.graph.trading_graph import TradingAgentsGraph, _validate_trade_date

from . import market, simulation
from .paper import PaperBook

log = logging.getLogger("webui")

HOME = Path(os.environ.get("TRADINGAGENTS_WEBUI_HOME") or Path.home() / ".tradingagents" / "webui")
RUNS_DIR = HOME / "runs"
SETTINGS_PATH = HOME / "settings.json"

ANALYST_NODES = {"market": "Market Analyst", "social": "Sentiment Analyst",
                 "news": "News Analyst", "fundamentals": "Fundamentals Analyst"}
TOOL_NODE_AGENT = {f"tools_{k}": v for k, v in ANALYST_NODES.items()}
PIPELINE = ["Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
            "Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
            "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"]
REPORT_KEYS = ["market_report", "sentiment_report", "news_report", "fundamentals_report",
               "investment_plan", "trader_investment_plan", "final_trade_decision"]

DEFAULT_SETTINGS = {
    "llm_provider": DEFAULT_CONFIG["llm_provider"],
    "deep_think_llm": DEFAULT_CONFIG["deep_think_llm"],
    "quick_think_llm": DEFAULT_CONFIG["quick_think_llm"],
    "backend_url": DEFAULT_CONFIG.get("backend_url") or "",
    "output_language": DEFAULT_CONFIG["output_language"],
    "research_depth": 1,
    "analysts": ["market", "social", "news", "fundamentals"],
    "openai_reasoning_effort": "", "anthropic_effort": "", "google_thinking_level": "",
    "temperature": "", "checkpoint_enabled": False,
    "data_vendors": {**DEFAULT_CONFIG["data_vendors"], "fundamental_data": "screener,yfinance",
                     "news_data": "gnews,yfinance"},
    "watchlist": ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "^NSEI", "^BSESN"],
    "schedule": {"enabled": False, "times": ["09:25", "12:20", "15:15"], "weekdays_only": True,
                 "analysts": ["market", "social"], "first_slot_news": True, "intraday_top": 10, "last_fired": {},
                 "weekly_full": {"enabled": False, "day": 5, "time": "10:00"},
                 "nightly_full": {"enabled": True, "time": "18:00"},
                 "portfolio_digest": {"enabled": True, "time": "16:00"},
                 "change_detection": True, "fundamentals_cache": True,
                 "nightly_analysts": ["market", "news", "fundamentals"],
                 "change_thresholds": {"move_pct": 3.0, "vol_ratio": 2.0, "max_age_days": 5}},
    "sim_delay": 0.6,
}


def _agent_for(node: str | None) -> str | None:
    if not node:
        return None
    if node in PIPELINE:
        return node
    if node in TOOL_NODE_AGENT:
        return TOOL_NODE_AGENT[node]
    return None


def load_settings() -> dict:
    s = copy.deepcopy(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text())
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k].update(v)
                else:
                    s[k] = v
        except Exception:
            pass
    return s


def save_settings(s: dict):
    HOME.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s, indent=2))


def build_config(s: dict, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    o = {**s, **(overrides or {})}
    cfg["llm_provider"] = o["llm_provider"].lower()
    cfg["deep_think_llm"] = o["deep_think_llm"]
    cfg["quick_think_llm"] = o["quick_think_llm"]
    url = (o.get("backend_url") or "").strip()
    if not url:
        try:
            from cli.prompts import provider_default_url
            url = provider_default_url(cfg["llm_provider"].split("-cn")[0]) if not cfg["llm_provider"].endswith("-cn") else None
        except Exception:
            url = None
    cfg["backend_url"] = url or None
    cfg["output_language"] = o.get("output_language") or "English"
    depth = int(o.get("research_depth") or 1)
    cfg["max_debate_rounds"] = depth
    cfg["max_risk_discuss_rounds"] = depth
    for k in ("openai_reasoning_effort", "anthropic_effort", "google_thinking_level"):
        cfg[k] = o.get(k) or None
    if str(o.get("temperature", "")).strip() != "":
        cfg["temperature"] = float(o["temperature"])
    cfg["checkpoint_enabled"] = bool(o.get("checkpoint_enabled"))
    cfg["data_vendors"].update(o.get("data_vendors") or {})
    for k in ("results_dir", "memory_log_path"):
        if o.get(k):
            cfg[k] = o[k]
    return cfg


class EventCapture(BaseCallbackHandler):
    """LangChain callback that turns node/LLM/tool activity into dashboard events."""

    raise_error = False

    def __init__(self, emit: Callable[[dict], None], cancelled: Callable[[], bool]):
        self.emit = emit
        self.cancelled = cancelled
        self.stats = {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
        self._llm_t: dict = {}
        self._tool: dict = {}
        self._lock = threading.Lock()

    def _node(self, kw):
        return (kw.get("metadata") or {}).get("langgraph_node")

    def _check(self):
        if self.cancelled():
            raise RunCancelled()

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw):
        node = self._node(kw)
        if node and kw.get("name") == node:
            self._check()
            agent = _agent_for(node)
            if agent:
                self.emit({"type": "node_start", "node": node, "agent": agent})

    def on_chain_end(self, outputs, *, run_id, **kw):
        pass

    def on_chat_model_start(self, serialized, messages, *, run_id, **kw):
        self._check()
        node = self._node(kw)
        model = (kw.get("metadata") or {}).get("ls_model_name") or (kw.get("invocation_params") or {}).get("model")
        with self._lock:
            self.stats["llm_calls"] += 1
            self._llm_t[run_id] = (time.time(), node)
        self.emit({"type": "llm_start", "node": node, "agent": _agent_for(node), "model": model,
                   "stats": dict(self.stats)})

    def on_llm_end(self, response, *, run_id, **kw):
        t0, node = self._llm_t.pop(run_id, (time.time(), None))
        tin = tout = 0
        try:
            gen = response.generations[0][0]
            msg = getattr(gen, "message", None)
            um = getattr(msg, "usage_metadata", None) or {}
            tin, tout = um.get("input_tokens", 0) or 0, um.get("output_tokens", 0) or 0
        except Exception:
            pass
        with self._lock:
            self.stats["tokens_in"] += tin
            self.stats["tokens_out"] += tout
        self.emit({"type": "llm_end", "node": node, "agent": _agent_for(node),
                   "duration": round(time.time() - t0, 2), "tokens_in": tin, "tokens_out": tout,
                   "stats": dict(self.stats)})

    def on_llm_error(self, error, *, run_id, **kw):
        t0, node = self._llm_t.pop(run_id, (time.time(), None))
        self.emit({"type": "llm_error", "node": node, "agent": _agent_for(node), "error": str(error)[:500]})

    def on_tool_start(self, serialized, input_str, *, run_id, **kw):
        self._check()
        node = self._node(kw)
        name = (serialized or {}).get("name") or kw.get("name")
        with self._lock:
            self.stats["tool_calls"] += 1
            self._tool[run_id] = (time.time(), name, node)
        args = kw.get("inputs") or input_str
        self.emit({"type": "tool_start", "node": node, "agent": _agent_for(node), "tool": name,
                   "args": args if isinstance(args, dict) else str(args)[:500], "stats": dict(self.stats)})

    def on_tool_end(self, output, *, run_id, **kw):
        t0, name, node = self._tool.pop(run_id, (time.time(), None, None))
        content = getattr(output, "content", output)
        text = content if isinstance(content, str) else str(content)
        self.emit({"type": "tool_end", "node": node, "agent": _agent_for(node), "tool": name,
                   "duration": round(time.time() - t0, 2), "chars": len(text), "output": text[:6000]})

    def on_tool_error(self, error, *, run_id, **kw):
        t0, name, node = self._tool.pop(run_id, (time.time(), None, None))
        self.emit({"type": "tool_error", "node": node, "agent": _agent_for(node), "tool": name,
                   "error": str(error)[:500]})


class RunCancelled(Exception):
    pass


class Engine:
    def __init__(self, broadcast: Callable[[dict], None]):
        self.broadcast = broadcast
        self.settings = load_settings()
        self.book = PaperBook(HOME / "paper_portfolio.json")
        self.runs: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.q: queue.Queue = queue.Queue()
        self.current: str | None = None
        self._sim_lock = threading.Lock()
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self._load_history()
        try:
            self.batches = json.loads((HOME / "batches.json").read_text())
        except Exception:
            self.batches = {}
        threading.Thread(target=self._worker, daemon=True, name="ta-worker").start()
        threading.Thread(target=self._scheduler, daemon=True, name="ta-scheduler").start()

    # ---- persistence ----------------------------------------------------
    def _load_history(self):
        for f in sorted(RUNS_DIR.glob("*.json")):
            try:
                r = json.loads(f.read_text())
                if r.get("status") in ("running", "queued"):
                    r["status"] = "interrupted"
                self.runs[r["id"]] = r
            except Exception:
                pass

    def _save_batches(self):
        try:
            (HOME / "batches.json").write_text(json.dumps(self.batches, default=str))
        except Exception:
            log.exception("saving batches")

    def _persist(self, run: dict):
        (RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, default=str))

    def summary(self, run: dict) -> dict:
        return {k: run.get(k) for k in ("id", "ticker", "date", "status", "signal", "created", "started",
                                        "finished", "error", "provider", "models", "batch_id", "stats",
                                        "trade", "analysts", "simulated")}

    # ---- events ---------------------------------------------------------
    def _emit(self, run: dict, ev: dict):
        ev = {"run_id": run["id"], "ts": time.time(), **ev}
        run.setdefault("events", []).append(ev)
        if "stats" in ev:
            run["stats"] = ev["stats"]
        self.broadcast(ev)

    # ---- public API -----------------------------------------------------
    def submit(self, ticker: str, date: str | None = None, overrides: dict | None = None,
               batch_id: str | None = None) -> dict:
        ticker = market.require_listed(ticker)
        date = date or datetime.now().strftime("%Y-%m-%d")
        _validate_trade_date(date)
        s = {**self.settings, **(overrides or {})}
        run = {"id": uuid.uuid4().hex[:10], "ticker": ticker, "date": date, "status": "queued",
               "created": time.time(), "overrides": overrides or {}, "batch_id": batch_id,
               "provider": s["llm_provider"], "models": [s["quick_think_llm"], s["deep_think_llm"]],
               "analysts": s["analysts"], "simulated": s["llm_provider"] == "simulation",
               "events": [], "reports": {}, "debate": [], "cancel": False}
        self.runs[run["id"]] = run
        self._persist(run)
        self._emit(run, {"type": "run_queued", "run": self.summary(run)})
        self.q.put(run["id"])
        return self.summary(run)

    def cancel(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if not run or run["status"] not in ("queued", "running"):
            return False
        run["cancel"] = True
        if run["status"] == "queued":
            run["status"] = "cancelled"
            self._persist(run)
            self._emit(run, {"type": "run_cancelled", "run": self.summary(run)})
        return True

    def cancel_all(self):
        for rid, r in list(self.runs.items()):
            if r["status"] in ("queued", "running"):
                self.cancel(rid)

    def backtest(self, tickers: list[str], start: str, end: str, every: int, overrides=None) -> dict:
        from tradingagents.backtest import iter_grid
        dates = iter_grid(start, end, every)
        bid = "bt_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path(DEFAULT_CONFIG["results_dir"]) / "backtest" / bid
        run_dir.mkdir(parents=True, exist_ok=True)
        ov = {**(overrides or {}), "results_dir": str(run_dir), "memory_log_path": str(run_dir / "trading_memory.md")}
        batch = {"id": bid, "tickers": tickers, "dates": dates, "log_path": ov["memory_log_path"],
                 "overrides": ov, "run_ids": [], "status": "running", "summary": None, "created": time.time()}
        self.batches[bid] = batch
        for t in tickers:
            for d in dates:
                batch["run_ids"].append(self.submit(t, d, {**ov, "_no_paper": True}, batch_id=bid)["id"])
        self._save_batches()
        self.broadcast({"type": "batch", "batch": self._batch_view(batch)})
        return self._batch_view(batch)

    def _batch_view(self, b):
        runs = [self.runs[r] for r in b["run_ids"] if r in self.runs]
        return {**{k: b[k] for k in ("id", "tickers", "dates", "status", "summary", "created", "log_path")},
                "total": len(runs), "done": sum(r["status"] in ("done", "failed", "cancelled") for r in runs),
                "cells": [{"ticker": r["ticker"], "date": r["date"], "status": r["status"],
                           "signal": r.get("signal"), "id": r["id"]} for r in runs]}

    # ---- worker ---------------------------------------------------------
    def _worker(self):
        while True:
            rid = self.q.get()
            run = self.runs.get(rid)
            if not run or run["status"] != "queued":
                continue
            self.current = rid
            try:
                self._execute(run)
            except Exception:
                log.exception("worker crashed on %s", rid)
            finally:
                self.current = None
                self._persist(run)
                if run.get("batch_id"):
                    self._maybe_finish_batch(run["batch_id"])

    def _make_graph(self, cfg, analysts, capture):
        if cfg["llm_provider"] == "simulation":
            with self._sim_lock:
                orig = trading_graph.create_llm_client
                trading_graph.create_llm_client = lambda **k: simulation.SimulationClient()
                try:
                    return TradingAgentsGraph(analysts, config=cfg, debug=False)
                finally:
                    trading_graph.create_llm_client = orig
        if cfg["llm_provider"] == "anthropic":
            with self._sim_lock:  # Haiku rejects the `effort` parameter that Sonnet/Opus take
                orig = trading_graph.create_llm_client

                def wrapped(**k):
                    if "haiku" in str(k.get("model", "")).lower():
                        k.pop("effort", None)
                    return orig(**k)
                trading_graph.create_llm_client = wrapped
                try:
                    return TradingAgentsGraph(analysts, config=cfg, debug=False)
                finally:
                    trading_graph.create_llm_client = orig
        return TradingAgentsGraph(analysts, config=cfg, debug=False)

    def _execute(self, run: dict):
        s = {**self.settings, **run["overrides"]}
        analysts = [a for a in ["market", "social", "news", "fundamentals"] if a in s["analysts"]]
        if run["ticker"].endswith("-USD") and "fundamentals" in analysts and len(analysts) > 1:
            analysts.remove("fundamentals")  # crypto has no company fundamentals
        asset_type = "crypto" if run["ticker"].endswith("-USD") else "stock"
        cfg = build_config(s)
        run.update(status="running", started=time.time())
        self._emit(run, {"type": "run_started", "run": self.summary(run), "pipeline":
                         [ANALYST_NODES[a] for a in analysts] + PIPELINE[4:]})
        capture = EventCapture(lambda ev: self._emit(run, ev), lambda: run["cancel"])
        final_state: dict = {}
        graph = None
        try:
            if cfg["llm_provider"] == "simulation":
                simulation.RUN_CTX.update(ticker=run["ticker"], date=run["date"],
                                          delay=float(s.get("sim_delay", 0.6)),
                                          bias=simulation.momentum_bias(run["ticker"], run["date"]))
            else:
                from tradingagents.llm_clients.api_key_env import get_api_key_env
                env = get_api_key_env(cfg["llm_provider"])
                if env and not os.environ.get(env) and cfg["llm_provider"] != "openai_compatible":
                    raise RuntimeError(f"{env} is not set. Add it under Settings → API keys.")
            graph = self._make_graph(cfg, analysts, capture)
            portfolio = None
            if self.book.data.get("portfolio_aware") and not run["overrides"].get("_no_paper"):
                portfolio = self.book.portfolio_context()
            self._emit(run, {"type": "log", "level": "info",
                             "text": f"Graph built · {cfg['llm_provider']} · quick={cfg['quick_think_llm']} "
                                     f"deep={cfg['deep_think_llm']} · analysts={', '.join(analysts)}"})
            with run_config(cfg):
                init = graph.create_run_state(run["ticker"], run["date"], asset_type, portfolio)
                fc = run["overrides"].get("_fund_cache")
                if fc and "fundamentals" not in analysts:
                    from . import fundcache
                    init["fundamentals_report"] = fundcache.as_prefixed(fc)
                if init.get("instrument_context"):
                    self._emit(run, {"type": "context", "text": init["instrument_context"][:2000]})
                if init.get("past_context"):
                    self._emit(run, {"type": "memory", "text": init["past_context"][:4000]})
                args = graph.propagator.get_graph_args(callbacks=[capture])
                tid = graph.begin_checkpoint(run["ticker"], run["date"], asset_type, portfolio)
                if tid:
                    args["config"].setdefault("configurable", {})["thread_id"] = tid
                    if graph._resuming:
                        self._emit(run, {"type": "log", "level": "info", "text": "Resuming from checkpoint"})
                try:
                    self._stream(run, graph, graph.checkpoint_input(init), args, final_state)
                    graph.record_decision(run["ticker"], run["date"], final_state)
                    graph.clear_checkpoint_on_success(run["ticker"], run["date"], asset_type, portfolio)
                finally:
                    graph.end_checkpoint()
            try:
                graph._log_state(run["date"], final_state)
                path = graph.save_reports(final_state, run["ticker"])
                run["report_path"] = str(path)
            except Exception as e:
                self._emit(run, {"type": "log", "level": "warn", "text": f"Report save failed: {e}"})
            if "fundamentals" in analysts and final_state.get("fundamentals_report"):
                try:
                    from . import fundcache, snapshot
                    fundcache.put(run["ticker"], final_state["fundamentals_report"],
                                  snapshot.latest_quarter(run["ticker"]), run["date"])
                except Exception:
                    log.exception("fundamentals cache save")
            signal = graph.process_signal(final_state.get("final_trade_decision", ""))
            run.update(status="done", finished=time.time(), signal=signal,
                       decision=final_state.get("final_trade_decision", ""))
            self._emit(run, {"type": "decision", "signal": signal, "ticker": run["ticker"], "date": run["date"],
                             "text": run["decision"], "review": is_review(signal)})
            if (self.book.data.get("auto_execute") and not run["overrides"].get("_no_paper")
                    and not is_review(signal)):
                trade = self.book.apply_rating(run["ticker"], run["date"], signal, run["id"])
                if trade:
                    run["trade"] = trade
                    self._emit(run, {"type": "trade", "trade": trade, "portfolio": self.book.snapshot(False)})
            self._emit(run, {"type": "run_finished", "run": self.summary(run)})
        except RunCancelled:
            run.update(status="cancelled", finished=time.time())
            self._emit(run, {"type": "run_cancelled", "run": self.summary(run)})
        except Exception as e:
            if run["cancel"]:
                run.update(status="cancelled", finished=time.time())
                self._emit(run, {"type": "run_cancelled", "run": self.summary(run)})
            else:
                run.update(status="failed", finished=time.time(), error=str(e)[:1000])
                self._emit(run, {"type": "run_failed", "run": self.summary(run),
                                 "trace": traceback.format_exc()[-3000:]})

    def _stream(self, run, graph, graph_input, args, final_state):
        prev: dict[str, Any] = {k: "" for k in REPORT_KEYS}
        prev.update(bull="", bear="", inv_judge="", agg="", con="", neu="", risk_judge="")
        seen_msgs: set = set()
        for chunk in graph.graph.stream(graph_input, **args):
            if run["cancel"]:
                raise RunCancelled()
            final_state.update(chunk)
            for m in chunk.get("messages", []) or []:
                mid = getattr(m, "id", None)
                if mid in seen_msgs:
                    continue
                if mid:
                    seen_msgs.add(mid)
                kind = type(m).__name__
                content = m.content if isinstance(m.content, str) else json.dumps(m.content, default=str)
                if kind == "AIMessage" and content.strip():
                    self._emit(run, {"type": "message", "kind": "reasoning", "content": content[:20000]})
                elif kind == "AIMessage" and getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        self._emit(run, {"type": "message", "kind": "tool_request",
                                         "content": f"{tc['name']}({json.dumps(tc.get('args', {}), default=str)})"})
            for k in REPORT_KEYS:
                v = chunk.get(k) or ""
                if v and v != prev[k]:
                    prev[k] = v
                    run["reports"][k] = v
                    self._emit(run, {"type": "report", "key": k, "content": v})
            inv = chunk.get("investment_debate_state") or {}
            for key, speaker in (("bull_history", "Bull Researcher"), ("bear_history", "Bear Researcher")):
                h = inv.get(key) or ""
                tag = "bull" if key.startswith("bull") else "bear"
                if len(h) > len(prev[tag]):
                    new = h[len(prev[tag]):].strip()
                    prev[tag] = h
                    self._debate(run, "research", speaker, new)
            j = inv.get("judge_decision") or ""
            if j and j != prev["inv_judge"]:
                prev["inv_judge"] = j
                self._debate(run, "research", "Research Manager", j)
            risk = chunk.get("risk_debate_state") or {}
            for key, tag, speaker in (("aggressive_history", "agg", "Aggressive Analyst"),
                                      ("conservative_history", "con", "Conservative Analyst"),
                                      ("neutral_history", "neu", "Neutral Analyst")):
                h = risk.get(key) or ""
                if len(h) > len(prev[tag]):
                    new = h[len(prev[tag]):].strip()
                    prev[tag] = h
                    self._debate(run, "risk", speaker, new)
            j = risk.get("judge_decision") or ""
            if j and j != prev["risk_judge"]:
                prev["risk_judge"] = j
                self._debate(run, "risk", "Portfolio Manager", j)

    def _debate(self, run, team, speaker, text):
        if not text:
            return
        item = {"team": team, "speaker": speaker, "content": text[:20000], "ts": time.time()}
        run["debate"].append(item)
        self._emit(run, {"type": "debate", **item})

    def _maybe_finish_batch(self, bid):
        b = self.batches.get(bid)
        if not b:
            return
        if b.get("kind") == "slot" and not b.get("aborted"):
            runs = [self.runs[r] for r in b["run_ids"] if r in self.runs]
            if sum(r["status"] == "failed" and "credit balance" in (r.get("error") or "") for r in runs) >= 3:
                b["aborted"] = "Anthropic credit balance too low"
                for r in runs:
                    if r["status"] == "queued":
                        self.cancel(r["id"])
                self._sched_log("Batch aborted: Anthropic credit balance too low. Add credit and re-run.")
        view = self._batch_view(b)
        if view["done"] < view["total"]:
            self.broadcast({"type": "batch", "batch": view})
            return
        if b.get("kind") == "slot":
            b["status"] = "done"
            self._save_batches()
            try:
                from . import notify
                notify.send_digest(self, b)
            except Exception:
                log.exception("digest")
            self.broadcast({"type": "batch", "batch": self._batch_view(b)})
            return
        b["status"] = "settling"
        self.broadcast({"type": "batch", "batch": self._batch_view(b)})
        try:
            from tradingagents.backtest import summarize
            s = {**self.settings, **b["overrides"]}
            cfg = build_config(s)
            graph = self._make_graph(cfg, ["market"], None)
            for t in b["tickers"]:
                try:
                    graph.settle_pending(t)
                except Exception as e:
                    log.warning("settle %s failed: %s", t, e)
            summ = summarize(b["log_path"])
            b["summary"] = {"text": summ.render(), "resolved": summ.resolved, "pending": summ.pending,
                            "by_rating": {k: vars(v) for k, v in summ.by_rating.items()}}
        except Exception as e:
            b["summary"] = {"text": f"Summary unavailable: {e}"}
        b["status"] = "done"
        self._save_batches()
        self.broadcast({"type": "batch", "batch": self._batch_view(b)})

    # ---- scheduler ------------------------------------------------------
    def _fire_slot(self, slot, today, first, sch, analysts=None, label=None):
        full = analysts is not None
        busy = [b for b in self.batches.values() if b.get("kind") == "slot" and b["status"] == "running"]
        if busy or self.current:
            self._sched_log(f"Slot {slot} skipped: the previous batch is still running")
            return
        analysts = list(analysts or sch.get("analysts") or ["market", "social"])
        if first and sch.get("first_slot_news", True) and "news" not in analysts:
            analysts.append("news")
        tickers = [t for t in self.settings.get("watchlist", []) if not t.startswith("^")]
        if not full and sch.get("intraday_top"):
            tickers = tickers[:int(sch["intraday_top"])]  # watchlist is ordered best-first
        reasons, carried, cached_n = {}, {}, 0
        if slot == "nightly" and sch.get("change_detection", True):
            from . import snapshot
            try:
                reasons, carried = snapshot.select(self, tickers, sch.get("change_thresholds"))
                tickers = [t for t in tickers if t in reasons]
            except Exception:
                log.exception("change detection failed; running every stock")
        bid = f"slot_{today}_{slot.replace(':', '')}"
        batch = {"id": bid, "kind": "slot", "slot": slot, "label": label or f"Intraday {slot}", "analysts": analysts, "tickers": tickers, "dates": [today], "log_path": "",
                 "overrides": {}, "run_ids": [], "status": "running", "summary": None, "created": time.time(),
                 "reasons": reasons, "carried": carried}
        self.batches[bid] = batch
        for t in tickers:
            ov = {"analysts": list(analysts)}
            if slot == "nightly" and "fundamentals" in analysts and sch.get("fundamentals_cache", True):
                try:
                    from . import fundcache, snapshot
                    rec = fundcache.get(t, snapshot.latest_quarter(t))
                    if rec:  # reuse the stored analysis; skip the Fundamentals analyst
                        ov = {"analysts": [a for a in analysts if a != "fundamentals"], "_fund_cache": rec}
                        cached_n += 1
                except Exception:
                    log.exception("fundamentals cache lookup")
            try:
                batch["run_ids"].append(self.submit(t, today, ov, batch_id=bid)["id"])
            except ValueError as e:
                log.warning("slot %s: skipped %s (%s)", slot, t, e)
        self._save_batches()
        self._sched_log(f"Scheduled slot {slot}: {len(batch['run_ids'])} stocks to analyse"
                        + (f", {len(carried)} unchanged (rating carried forward)" if carried else "")
                        + (f", {cached_n} using cached fundamentals" if cached_n else "")
                        + f", analysts={', '.join(analysts)}")
        if not batch["run_ids"]:  # nothing changed: still send the digest
            self._maybe_finish_batch(bid)

    def _sched_log(self, text):
        log.info(text)
        self.broadcast({"type": "log", "level": "info", "run_id": None, "ts": time.time(), "text": text})

    def _scheduler(self):
        while True:
            time.sleep(20)
            try:
                sch = self.settings.get("schedule") or {}
                if not sch.get("enabled"):
                    continue
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                fired = sch.get("last_fired")
                if not isinstance(fired, dict):
                    fired = sch["last_fired"] = {}
                wk = sch.get("weekly_full") or {}
                if wk.get("enabled") and now.weekday() == int(wk.get("day", 5)) and fired.get("weekly") != today:
                    start = datetime.strptime(f"{today} {wk.get('time', '10:00')}", "%Y-%m-%d %H:%M")
                    if start <= now < start + timedelta(minutes=30):
                        fired["weekly"] = today
                        save_settings(self.settings)
                        last_trading = now - timedelta(days=max(0, now.weekday() - 4))  # Sat/Sun -> Friday
                        self._fire_slot("weekly", last_trading.strftime("%Y-%m-%d"), False, sch,
                                        analysts=["market", "social", "news", "fundamentals"], label="Weekly full analysis")
                pd = sch.get("portfolio_digest") or {}
                if pd.get("enabled") and now.weekday() < 5 and fired.get("portfolio") != today:
                    start = datetime.strptime(f"{today} {pd.get('time', '16:00')}", "%Y-%m-%d %H:%M")
                    if start <= now < start + timedelta(minutes=60):
                        fired["portfolio"] = today
                        save_settings(self.settings)
                        try:
                            from . import notify
                            notify.send_portfolio(self)
                        except Exception:
                            log.exception("portfolio digest")
                nf = sch.get("nightly_full") or {}
                if nf.get("enabled") and now.weekday() < 5 and fired.get("nightly") != today:
                    start = datetime.strptime(f"{today} {nf.get('time', '18:00')}", "%Y-%m-%d %H:%M")
                    if start <= now < start + timedelta(minutes=30):
                        fired["nightly"] = today
                        save_settings(self.settings)
                        self._fire_slot("nightly", today, False, sch,
                                        analysts=list(sch.get("nightly_analysts") or ["market", "news", "fundamentals"]), label="Nightly full analysis")
                if sch.get("weekdays_only", True) and now.weekday() >= 5:
                    continue
                times = sorted(sch["times"] if "times" in sch else ["09:25", "12:20", "15:15"])  # [] = intraday off
                for idx, slot in enumerate(times):
                    start = datetime.strptime(f"{today} {slot}", "%Y-%m-%d %H:%M")
                    # fire within 30 min of the slot; a missed slot (server down) is skipped, not run late
                    if fired.get(slot) == today or not (start <= now < start + timedelta(minutes=30)):
                        continue
                    fired[slot] = today
                    save_settings(self.settings)
                    self._fire_slot(slot, today, idx == 0, sch)
            except Exception:
                log.exception("scheduler")
