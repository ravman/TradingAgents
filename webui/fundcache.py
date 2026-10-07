"""Local cache of the Fundamentals analyst's finished report, one per stock.

Fundamentals only change when a company reports (quarterly), so the nightly run reuses the cached report while
Screener's latest quarter is unchanged (and it is under MAX_AGE_DAYS old), and skips the Fundamentals analyst.
Valuation ratios in the cached text can drift with price, so the report is prefixed with its date.
"""

from __future__ import annotations

import json
import time

from . import engine as eng

PATH = eng.HOME / "fund_reports.json"
MAX_AGE_DAYS = 14


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {}


def get(ticker: str, quarter: str | None):
    """Cached entry if still valid for the current quarter, else None."""
    rec = _load().get(ticker)
    if not rec or not quarter or rec.get("quarter") != quarter:
        return None
    if (time.time() - rec["saved"]) / 86400 > MAX_AGE_DAYS:
        return None
    return rec


def put(ticker: str, report: str, quarter: str | None, date: str):
    if not report or not quarter:
        return
    d = _load()
    d[ticker] = {"report": report, "quarter": quarter, "date": date, "saved": time.time()}
    PATH.write_text(json.dumps(d))


def as_prefixed(rec: dict) -> str:
    return (f"[Cached fundamentals analysis from {rec['date']} (latest reported quarter: {rec['quarter']}). "
            f"Fundamentals are unchanged since; valuation ratios quoted below may have moved with price.]\n\n{rec['report']}")
