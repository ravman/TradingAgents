"""Price data and indicators for the dashboard charts (Yahoo Finance)."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

_cache: dict = {}
_lock = threading.Lock()

# interval -> (default period, cache seconds)
INTERVALS = {
    "1m": ("1d", 15), "5m": ("5d", 30), "15m": ("1mo", 60), "1h": ("3mo", 120),
    "1d": ("2y", 600), "1wk": ("10y", 3600),
}


def _num(x):
    try:
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else round(f, 4)
    except (TypeError, ValueError):
        return None


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


INDICES = {"^NSEI": "Nifty 50", "^BSESN": "Sensex"}


def normalize_ticker(t: str, allow_index: bool = False) -> str:
    """NSE/BSE only: bare symbols default to NSE (.NS); .NS/.BO kept; indices only for charts/quotes."""
    t = (t or "").strip().upper()
    if allow_index and t in INDICES:
        return t
    if t.endswith((".NS", ".BO")) and len(t) > 3:
        return t
    if t and t.replace("-", "").replace("&", "").isalnum() and "." not in t and not t.startswith("^"):
        return t + ".NS"
    raise ValueError(f"'{t}': only NSE (.NS) and BSE (.BO) symbols are supported, e.g. RELIANCE.NS or RELIANCE.BO")


def require_listed(t: str) -> str:
    """normalize_ticker + confirm Yahoo has price data for it, so typos/non-Indian symbols fail before an LLM run is queued."""
    t = normalize_ticker(t)
    try:
        ok = bool(history(t, "1d", "1mo")["bars"])
    except Exception:
        ok = False
    if not ok:
        raise ValueError(f"No NSE/BSE price data found for {t}. Check the symbol (BSE uses names, e.g. RELIANCE.BO).")
    return t


def history(ticker: str, interval: str = "1d", period: str | None = None) -> dict:
    interval = interval if interval in INTERVALS else "1d"
    period = period or INTERVALS[interval][0]
    key = (ticker.upper(), interval, period)
    ttl = INTERVALS[interval][1]
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"No price data for {ticker}")
    df = df.dropna(subset=["Close"])
    close = df["Close"]
    ind = pd.DataFrame(index=df.index)
    ind["sma20"] = close.rolling(20).mean()
    ind["sma50"] = close.rolling(50).mean()
    ind["ema12"] = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    ind["macd"] = ind["ema12"] - ema26
    ind["signal"] = ind["macd"].ewm(span=9, adjust=False).mean()
    ind["hist"] = ind["macd"] - ind["signal"]
    std = close.rolling(20).std()
    ind["bb_up"] = ind["sma20"] + 2 * std
    ind["bb_lo"] = ind["sma20"] - 2 * std
    ind["rsi"] = _rsi(close)

    intraday = interval not in ("1d", "1wk")
    bars = []
    for ts, row in df.iterrows():
        t = int(ts.timestamp()) if intraday else ts.strftime("%Y-%m-%d")
        i = ind.loc[ts]
        bars.append({
            "time": t, "open": _num(row["Open"]), "high": _num(row["High"]), "low": _num(row["Low"]),
            "close": _num(row["Close"]), "volume": _num(row.get("Volume", 0)) or 0,
            **{k: _num(i[k]) for k in ind.columns},
        })
    out = {"ticker": ticker.upper(), "interval": interval, "intraday": intraday, "bars": bars}
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def quote(ticker: str) -> dict:
    """Last price and change vs previous close."""
    key = ("quote", ticker.upper())
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 15:
            return hit[1]
    t = yf.Ticker(ticker)
    last = prev = None
    try:
        fi = t.fast_info
        last, prev = _num(fi.get("lastPrice")), _num(fi.get("previousClose"))
    except Exception:
        pass
    if last is None:
        h = t.history(period="5d")
        if not h.empty:
            last = _num(h["Close"].iloc[-1])
            prev = _num(h["Close"].iloc[-2]) if len(h) > 1 else last
    out = {"ticker": ticker.upper(), "price": last, "prev_close": prev,
           "change_pct": (last / prev - 1) * 100 if last and prev else None, "ts": time.time()}
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def close_on(ticker: str, date: str) -> float | None:
    """Close on the trade date, or the last close before it (the paper fill price)."""
    d = datetime.strptime(date, "%Y-%m-%d")
    if d.date() >= datetime.now().date():
        q = quote(ticker)
        if q["price"]:
            return q["price"]
    h = yf.Ticker(ticker).history(start=(d - timedelta(days=10)).strftime("%Y-%m-%d"),
                                  end=(d + timedelta(days=1)).strftime("%Y-%m-%d"))
    if h.empty:
        return None
    return _num(h["Close"].iloc[-1])
