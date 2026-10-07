"""Google News RSS vendor for Indian (NSE/BSE) stocks and Indian market news.

Yahoo's news feed returns nothing for NSE tickers, so this fills get_news / get_global_news. Registered into the
upstream router at startup (``register()``). RSS only serves recent items, so a window that ended more than
``MAX_LAG_DAYS`` ago is refused (NoMarketDataError) rather than served current news, which keeps backtests
look-ahead safe and lets the router fall through to the next vendor.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from tradingagents.dataflows.errors import NoMarketDataError

from . import screener

log = logging.getLogger("webui.gnews")
RSS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
UA = "Mozilla/5.0 (compatible; TradingAgents-ControlCenter personal research)"
MAX_LAG_DAYS = 3
_cache: dict[str, tuple[float, list]] = {}
TTL = 1800
GLOBAL_QUERIES = ["Sensex Nifty India stock market", "RBI repo rate India economy", "India markets FII flows rupee"]


def _fetch(query: str, days: int) -> list[dict]:
    q = f"{query} when:{max(1, days)}d"
    hit = _cache.get(q)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    r = requests.get(RSS.format(q=quote_plus(q)), headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        try:
            when = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            when = None
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        items.append({"title": title, "source": src or "Google News", "link": it.findtext("link") or "", "when": when})
    _cache[q] = (time.time(), items)
    time.sleep(0.5)
    return items


def _fresh_window(end_date: str):
    end = datetime.strptime(end_date, "%Y-%m-%d")
    return (datetime.now() - end).days <= MAX_LAG_DAYS


def _format(items, header, start_dt, end_dt, limit):
    seen, out = set(), []
    for x in sorted(items, key=lambda x: x["when"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        w = x["when"].replace(tzinfo=None) if x["when"] else None
        if w and not (start_dt <= w < end_dt + timedelta(days=1)):
            continue
        key = re.sub(r"\W+", "", x["title"].lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        stamp = x["when"].astimezone().strftime("%d %b %H:%M") if x["when"] else "undated"
        out.append(f"### {x['title']} (source: {x['source']}, {stamp})\n")  # links omitted: long redirect URLs just burn tokens
        if len(out) >= limit:
            break
    return header, out


def _company_name(ticker: str) -> str:
    try:
        soup = screener._page(ticker)
        name = (soup.select_one("h1") or soup).get_text(strip=True)
        return re.sub(r"\s+(Ltd\.?|Limited)$", "", name, flags=re.I)
    except Exception:
        return screener._symbol(ticker)


def get_news(ticker, start_date, end_date):
    if not _fresh_window(end_date):
        raise NoMarketDataError(ticker, ticker, "Google News RSS only serves recent items; window is historic")
    try:
        sym = screener._symbol(ticker)
        name = _company_name(ticker)
        days = (datetime.now() - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
        items = _fetch(f'"{name}" OR "{sym}" NSE', min(days, 14))
        header, out = _format(items, f"## {ticker} News ({name}), from {start_date} to {end_date} (Google News India):\n\n",
                              datetime.strptime(start_date, "%Y-%m-%d"), datetime.strptime(end_date, "%Y-%m-%d"), 15)
    except Exception as e:
        raise NoMarketDataError(ticker, ticker, f"Google News unavailable: {e}") from e
    if not out:
        raise NoMarketDataError(ticker, ticker, f"no Google News items for {name} between {start_date} and {end_date}")
    return header + "\n".join(out)


def get_global_news(curr_date, look_back_days=None, limit=None):
    if not _fresh_window(curr_date):
        raise NoMarketDataError("India", "India", "Google News RSS only serves recent items; date is historic")
    days = int(look_back_days or 7)
    end = datetime.strptime(curr_date, "%Y-%m-%d")
    start = end - timedelta(days=days)
    try:
        items = [x for q in GLOBAL_QUERIES for x in _fetch(q, min(days, 14))]
        header, out = _format(items, f"## Indian market and macro news, {start:%Y-%m-%d} to {curr_date} (Google News India):\n\n",
                              start, end, int(limit or 15))
    except Exception as e:
        raise NoMarketDataError("India", "India", f"Google News unavailable: {e}") from e
    if not out:
        raise NoMarketDataError("India", "India", "no Indian market news items found")
    return header + "\n".join(out)


def register():
    from tradingagents.dataflows import router
    router.VENDOR_METHODS["get_news"]["gnews"] = get_news
    router.VENDOR_METHODS["get_global_news"]["gnews"] = get_global_news
