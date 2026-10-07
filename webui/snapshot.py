"""Daily per-stock snapshot + change detection for the nightly run.

The snapshot is cheap (Yahoo prices + the cached Screener page, no LLM). The nightly run only pays for stocks that
changed; the rest carry their last rating forward. Stored in ~/.tradingagents/webui/snapshots.json.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from . import engine as eng
from . import market, screener

log = logging.getLogger("webui.snapshot")
PATH = eng.HOME / "snapshots.json"
DEFAULTS = {"move_pct": 3.0, "vol_ratio": 2.0, "max_age_days": 5}


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {}


def _save(d: dict):
    PATH.write_text(json.dumps(d, indent=1))


def latest_quarter(ticker: str) -> str | None:
    """Most recent reported quarter on Screener (e.g. 'Jun 2026'); None if unavailable."""
    try:
        q = screener._table(screener._page(ticker), "quarters")
        return str(q.columns[-1]) if not q.empty else None
    except Exception as e:
        log.warning("quarter for %s unavailable: %s", ticker, e)
        return None


def metrics(ticker: str) -> dict:
    bars = market.history(ticker, "1d", "3mo")["bars"]
    if len(bars) < 3:
        raise ValueError("not enough price history")
    last, prev = bars[-1], bars[-2]
    vols = [b["volume"] for b in bars[-21:-1] if b.get("volume")]
    return {"date": str(last["time"]), "close": last["close"],
            "chg1d": round((last["close"] / prev["close"] - 1) * 100, 2),
            "vol_ratio": round(last["volume"] / (sum(vols) / len(vols)), 2) if vols and last.get("volume") else None}


def last_analysis(engine, ticker: str) -> dict | None:
    """Latest completed full (4-analyst) run for the ticker, from the run store."""
    runs = [r for r in engine.runs.values() if r["ticker"] == ticker and r["status"] == "done" and r.get("signal")
            and (len(r.get("analysts") or []) == 4 or {"market", "news"} <= set(r.get("analysts") or []))]
    if not runs:
        return None
    r = max(runs, key=lambda r: r["created"])
    return {"signal": r["signal"], "date": r["date"], "created": r["created"], "run_id": r["id"]}


def _stagger(ticker: str) -> int:
    return sum(map(ord, ticker)) % 3  # spreads the weekly refresh over three nights


def select(engine, tickers: list[str], cfg: dict | None = None):
    """-> (to_run {ticker: reason}, carried {ticker: {signal, date}}). Refreshes and stores the snapshot."""
    cfg = {**DEFAULTS, **(cfg or {})}
    snap = _load()
    held = {t for t, p in engine.book.data["positions"].items() if p.get("qty", 0) > 0}
    to_run, carried = {}, {}
    for t in tickers:
        try:
            m = metrics(t)
        except Exception as e:
            to_run[t] = f"no price snapshot ({e}); running to be safe"
            continue
        q = latest_quarter(t)
        prior = last_analysis(engine, t)
        rec = snap.get(t, {})
        rec.update(m, quarter=q, updated=time.time())
        reasons = []
        from . import confirm
        pend = confirm.pending().get(t)
        if pend:
            reasons.append(f"rating change to {pend['rating']} awaiting confirmation")
        if t in held:
            reasons.append("held position")
        if prior is None:
            reasons.append("no previous full analysis")
        else:
            age = (time.time() - prior["created"]) / 86400
            if age >= cfg["max_age_days"] + _stagger(t):
                reasons.append(f"last analysed {age:.0f} days ago")
            base = rec.get("quarter_at_analysis")
            if q and base and q != base:
                reasons.append(f"new quarterly results ({base} -> {q})")
            if base is None and q:
                rec["quarter_at_analysis"] = q  # seed the baseline from the existing analysis
        if abs(m["chg1d"]) >= cfg["move_pct"]:
            reasons.append(f"moved {m['chg1d']:+.1f}% today")
        if m["vol_ratio"] and m["vol_ratio"] >= cfg["vol_ratio"]:
            reasons.append(f"volume {m['vol_ratio']:.1f}x average")
        if reasons:
            to_run[t] = "; ".join(reasons)
            if q:
                rec["quarter_at_analysis"] = q
        else:
            carried[t] = {"signal": prior["signal"], "date": prior["date"]}
        snap[t] = rec
    _save(snap)
    return to_run, carried
