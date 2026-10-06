"""Screener.in fundamentals vendor for Indian (NSE/BSE) tickers.

Registered into the upstream data router at startup (``register()``) so no upstream file is edited.
Scrapes the public company page, so keep it polite: one request at a time, ~1.5 s apart, cached 6 h.
Pages show *current* data, so to avoid look-ahead bias statements are cut at the run date and the
current-ratio snapshot refuses historic dates (the router then falls back to the next vendor).
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
from bs4 import BeautifulSoup

from tradingagents.dataflows.errors import NoMarketDataError, VendorError

log = logging.getLogger("webui.screener")

BASE = "https://www.screener.in/company/{sym}/{mode}"
UA = "Mozilla/5.0 (compatible; TradingAgents-ControlCenter personal research)"
TTL = 6 * 3600
MIN_GAP = 1.5

_lock = threading.Lock()
_last = 0.0
_cache: dict[str, tuple[float, BeautifulSoup]] = {}


def _symbol(ticker: str) -> str:
    return re.sub(r"\.(NS|BO)$", "", ticker.strip().upper())


def _page(ticker: str) -> BeautifulSoup:
    global _last
    sym = _symbol(ticker)
    with _lock:
        hit = _cache.get(sym)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
        for mode in ("consolidated/", ""):
            time.sleep(max(0.0, MIN_GAP - (time.time() - _last)))
            r = requests.get(BASE.format(sym=sym, mode=mode), headers={"User-Agent": UA}, timeout=20)
            _last = time.time()
            if r.status_code == 200 and 'id="top-ratios"' in r.text:
                soup = BeautifulSoup(r.text, "lxml")
                # a consolidated page with no P&L rows falls through to the standalone one
                if soup.select_one("#profit-loss table tbody tr") or not mode:
                    _cache[sym] = (time.time(), soup)
                    return soup
        raise NoMarketDataError(ticker, sym, "company not found on screener.in")


def _clean(x):
    if isinstance(x, str):
        x = x.replace(",", "").replace("%", "").strip()
        try:
            return float(x)
        except ValueError:
            return x or None
    return x


def _table(soup: BeautifulSoup, section: str) -> pd.DataFrame:
    sec = soup.select_one(f"#{section}")
    tbl = sec.select_one("table.data-table") if sec else None
    if tbl is None:
        return pd.DataFrame()
    df = pd.read_html(io.StringIO(str(tbl)))[0]
    df = df.rename(columns={df.columns[0]: "item"})
    df["item"] = df["item"].astype(str).str.replace(r"\s*\+$", "", regex=True).str.strip()
    df = df.set_index("item")
    return df.apply(lambda col: col.map(_clean))


def _period_end(label: str):
    try:
        d = datetime.strptime(label.strip(), "%b %Y")
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    except ValueError:
        return None  # e.g. "TTM"


def _cut(df: pd.DataFrame, curr_date: str | None) -> pd.DataFrame:
    if df.empty or not curr_date:
        return df
    limit = datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=45)  # results are filed ~45 days after period end
    keep = [c for c in df.columns if (pe := _period_end(str(c))) is not None and pe <= limit]
    return df[keep]


def _statement(ticker, section, title, curr_date, note=""):
    try:
        df = _cut(_table(_page(ticker), section), curr_date)
        if df.empty:
            raise NoMarketDataError(ticker, _symbol(ticker), f"screener.in has no {title.lower()} before {curr_date}")
        return (f"# {title} for {_symbol(ticker)} (Screener.in, INR crore)\n"
                f"# Periods are cut 45 days after period end to approximate filing dates.{note}\n\n" + df.to_csv())
    except VendorError:
        raise
    except Exception as e:
        raise NoMarketDataError(ticker, _symbol(ticker), f"screener.in {title.lower()} unavailable: {e}") from e


def get_income_statement(ticker, freq="quarterly", curr_date=None):
    if freq.lower() == "quarterly":
        return _statement(ticker, "quarters", "Quarterly results (income statement)", curr_date)
    return _statement(ticker, "profit-loss", "Profit & loss (annual)", curr_date)


def get_balance_sheet(ticker, freq="quarterly", curr_date=None):
    return _statement(ticker, "balance-sheet", "Balance sheet (annual)", curr_date,
                      " Screener publishes balance sheets annually only.")


def get_cashflow(ticker, freq="quarterly", curr_date=None):
    return _statement(ticker, "cash-flow", "Cash flow (annual)", curr_date,
                      " Screener publishes cash flows annually only.")


def get_fundamentals(ticker, curr_date=None):
    """Current ratio snapshot + pros/cons + shareholding. Refused for historic dates (no point-in-time data)."""
    if curr_date and datetime.strptime(curr_date, "%Y-%m-%d").date() < (datetime.now() - timedelta(days=7)).date():
        raise NoMarketDataError(ticker, _symbol(ticker), "screener.in snapshot is current-only; not valid for a historic date")
    try:
        soup = _page(ticker)
        name = (soup.select_one("h1") or soup).get_text(strip=True)
        lines = []
        for li in soup.select("#top-ratios li"):
            k, v = li.select_one(".name"), li.select_one(".value")
            if k and v:
                lines.append(f"{k.get_text(strip=True)}: {' '.join(v.get_text().split())}")
        if not lines:
            raise NoMarketDataError(ticker, _symbol(ticker), "no ratios on screener.in page")
        out = [f"# Company Fundamentals for {_symbol(ticker)} - {name} (Screener.in, INR; Cr = crore = 10 million)", "", *lines]
        for cls, label in (("pros", "Strengths"), ("cons", "Weaknesses")):
            items = [li.get_text(" ", strip=True) for li in soup.select(f"#analysis .{cls} li")]
            if items:
                out += ["", f"## Screener {label}", *[f"- {i}" for i in items]]
        ratios = _table(soup, "ratios")
        if not ratios.empty:
            out += ["", "## Ratios (annual)", ratios.iloc[:, -5:].to_csv()]
        sh = _table(soup, "shareholding")
        if not sh.empty:
            out += ["", "## Shareholding pattern (% , latest quarters)", sh.iloc[:, -4:].to_csv()]
        return "\n".join(out)
    except VendorError:
        raise
    except Exception as e:
        raise NoMarketDataError(ticker, _symbol(ticker), f"screener.in fundamentals unavailable: {e}") from e


def register():
    """Add 'screener' as a vendor for the fundamental_data tools in the upstream router."""
    from tradingagents.dataflows import router
    for method, fn in (("get_fundamentals", get_fundamentals), ("get_balance_sheet", get_balance_sheet),
                       ("get_cashflow", get_cashflow), ("get_income_statement", get_income_statement)):
        router.VENDOR_METHODS[method]["screener"] = fn
