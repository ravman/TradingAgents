"""Paper-trading ledger driven by TradingAgents decisions.

TradingAgents produces a rating, not an order. This module turns a rating into a
SIMULATED fill using a simple, visible sizing rule, so you can track how the
agents' calls would have played out. No broker is involved and nothing here
places real orders.

Sizing rule (target weight of total equity for the ticker):
  Buy          -> max_weight
  Overweight   -> max_weight / 2 (only ever adds)
  Hold         -> no change
  Underweight  -> halve the current position
  Sell         -> close the position
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import market


class PaperBook:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.data = {"starting_cash": 1000000.0, "cash": 1000000.0, "currency": "INR",
                     "positions": {}, "trades": [], "equity": [],
                     "auto_execute": True, "max_weight": 0.10, "portfolio_aware": True}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except Exception:
                pass

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)

    # ---- settings -------------------------------------------------------
    def configure(self, **kw):
        with self._lock:
            for k in ("auto_execute", "max_weight", "portfolio_aware", "currency"):
                if k in kw and kw[k] is not None:
                    self.data[k] = kw[k]
            self.save()

    def reset(self, starting_cash: float):
        with self._lock:
            self.data.update(starting_cash=float(starting_cash), cash=float(starting_cash),
                             positions={}, trades=[], equity=[])
            self.save()

    # ---- valuation ------------------------------------------------------
    def snapshot(self, live: bool = True) -> dict:
        with self._lock:
            positions = []
            mv = 0.0
            for t, p in self.data["positions"].items():
                price = None
                if live:
                    try:
                        price = market.quote(t)["price"]
                    except Exception:
                        price = None
                price = price or p.get("last_price") or p["avg_price"]
                value = price * p["qty"]
                mv += value
                positions.append({"ticker": t, "qty": p["qty"], "avg_price": p["avg_price"],
                                  "price": price, "value": value,
                                  "pnl": (price - p["avg_price"]) * p["qty"],
                                  "pnl_pct": (price / p["avg_price"] - 1) * 100 if p["avg_price"] else 0})
            equity = self.data["cash"] + mv
            out = {**{k: self.data[k] for k in ("starting_cash", "cash", "currency", "auto_execute",
                                                 "max_weight", "portfolio_aware")},
                   "positions": positions, "market_value": mv, "equity": equity,
                   "return_pct": (equity / self.data["starting_cash"] - 1) * 100,
                   "trades": list(reversed(self.data["trades"][-500:])),
                   "equity_curve": self.data["equity"][-2000:]}
            return out

    def mark(self):
        """Append an equity point (called on trades and periodically)."""
        with self._lock:
            snap = self.snapshot(live=True)
            self.data["equity"].append({"time": int(time.time()), "equity": round(snap["equity"], 2)})
            self.save()
            return snap

    def portfolio_context(self):
        """The book in the shape TradingAgents' portfolio-aware runs take."""
        from tradingagents.portfolio import PortfolioContext
        with self._lock:
            return PortfolioContext.model_validate({
                "cash": round(self.data["cash"], 2), "currency": self.data["currency"],
                "positions": [{"ticker": t, "quantity": p["qty"], "average_price": p["avg_price"]}
                              for t, p in self.data["positions"].items()],
            })

    # ---- execution ------------------------------------------------------
    def apply_rating(self, ticker: str, date: str, rating: str, run_id: str) -> dict | None:
        ticker = market.normalize_ticker(ticker)
        price = market.close_on(ticker, date)
        if not price:
            return None
        with self._lock:
            pos = self.data["positions"].get(ticker, {"qty": 0.0, "avg_price": price})
            equity = self.snapshot(live=False)["equity"]
            cur_qty = pos["qty"]
            mw = float(self.data["max_weight"])
            unit = 1.0  # NSE/BSE equities trade in whole shares
            fl = lambda x: round((x // unit) * unit, 6)
            target_qty = cur_qty
            if rating == "Buy":
                target_qty = max(cur_qty, fl(equity * mw / price))
            elif rating == "Overweight":
                target_qty = max(cur_qty, fl(equity * mw / 2 / price))
            elif rating == "Underweight":
                target_qty = fl(cur_qty / 2)
            elif rating == "Sell":
                target_qty = 0
            delta = round(target_qty - cur_qty, 6)
            if delta > 0:  # cannot spend more cash than we have
                delta = min(delta, fl(self.data["cash"] / price))
            if delta == 0:
                trade = {"id": uuid.uuid4().hex[:8], "time": int(time.time()), "date": date, "ticker": ticker,
                         "side": "NONE", "qty": 0, "price": price, "rating": rating, "run_id": run_id,
                         "note": "No change (Hold, already at target, or no cash)"}
            else:
                side = "BUY" if delta > 0 else "SELL"
                self.data["cash"] -= delta * price
                new_qty = cur_qty + delta
                realized = 0.0
                if delta > 0:
                    pos["avg_price"] = (pos["avg_price"] * cur_qty + price * delta) / new_qty
                else:
                    realized = (price - pos["avg_price"]) * (-delta)
                pos["qty"] = round(new_qty, 6)
                pos["last_price"] = price
                if new_qty == 0:
                    self.data["positions"].pop(ticker, None)
                else:
                    self.data["positions"][ticker] = pos
                trade = {"id": uuid.uuid4().hex[:8], "time": int(time.time()), "date": date, "ticker": ticker,
                         "side": side, "qty": abs(delta), "price": price, "rating": rating, "run_id": run_id,
                         "realized_pnl": round(realized, 2), "note": "Simulated fill at close"}
            self.data["trades"].append(trade)
            self.save()
        self.mark()
        return trade
