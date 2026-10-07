"""Rating-confirmation rule for scheduled runs.

A single LLM rating is noisy (identical reruns agree on only ~5 of 8 stocks), so a scheduled run only *acts*
(paper trade + buy/sell in the digest) on a change that is
  * big: two or more steps on the five-step scale from the last confirmed rating, or
  * confirmed: the same new rating on two consecutive evaluations.
A one-step change seen once is held as "pending" and the stock is re-analysed the next night to confirm it.
With no confirmed rating yet, Hold (no position change) is the baseline. Unchanged ratings never re-trade.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import engine as eng

PATH = eng.HOME / "rating_state.json"
RANK = {"Sell": 1, "Underweight": 2, "Hold": 3, "Overweight": 4, "Buy": 5}
DEFAULTS = {"enabled": True, "immediate_steps": 2, "pending_days": 7}


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {}


def _save(d: dict):
    PATH.write_text(json.dumps(d, indent=1))


def evaluate(ticker: str, rating: str, date: str, cfg: dict | None = None) -> dict:
    """Decide whether a new rating is actionable. Returns {status: act|pending|same, confirmed, pending, why}."""
    cfg = {**DEFAULTS, **(cfg or {})}
    state = _load()
    st = state.setdefault(ticker, {"confirmed": None, "pending": None})
    base = st["confirmed"] or "Hold"
    pend = st.get("pending")
    if pend and (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(pend["date"], "%Y-%m-%d")).days > cfg["pending_days"]:
        pend = st["pending"] = None  # too old to count as a confirmation
    out = {"confirmed_before": st["confirmed"], "rating": rating}
    if rating not in RANK:
        out.update(status="act", why="unranked rating")
    elif rating == base:
        st["pending"] = None
        out.update(status="same", why=f"unchanged from confirmed {base}")
    elif abs(RANK[rating] - RANK[base]) >= cfg["immediate_steps"]:
        st.update(confirmed=rating, pending=None)
        out.update(status="act", why=f"{base} -> {rating} is a {abs(RANK[rating] - RANK[base])}-step move")
    elif pend and pend["rating"] == rating:
        st.update(confirmed=rating, pending=None)
        out.update(status="act", why=f"{base} -> {rating} confirmed by a second consecutive run")
    else:
        st["pending"] = {"rating": rating, "date": date}
        out.update(status="pending", why=f"{base} -> {rating} seen once; re-checking next run to confirm")
    out["pending"] = st["pending"]
    _save(state)
    return out


def force(ticker: str, rating: str):
    """A user-initiated run traded immediately: record that rating as confirmed."""
    state = _load()
    state[ticker] = {"confirmed": rating, "pending": None}
    _save(state)


def pending() -> dict:
    return {t: s["pending"] for t, s in _load().items() if s.get("pending")}
