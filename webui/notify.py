"""Email digest for scheduled batches: what to buy / sell after each slot.

Always saved to ~/.tradingagents/webui/digests/; emailed when SMTP_HOST, SMTP_USER, SMTP_PASSWORD and
DIGEST_TO are set (SMTP_PORT defaults to 587/STARTTLS, SMTP_FROM to SMTP_USER). Gmail works with an app password.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

from . import engine as eng

log = logging.getLogger("webui.notify")
BUY, SELL = ("Buy", "Overweight"), ("Sell", "Underweight")
DIGESTS = eng.HOME / "digests"


def _prev_signal(engine, run) -> str | None:
    older = [r for r in engine.runs.values() if r["ticker"] == run["ticker"] and r["status"] == "done"
             and r.get("signal") and r["created"] < run["created"] and r["id"] != run["id"]
             and not (r.get("overrides") or {}).get("_no_paper")]
    return max(older, key=lambda r: r["created"])["signal"] if older else None


def _gist(text: str, n=260) -> str:
    t = " ".join((text or "").replace("#", " ").replace("*", " ").split())
    return t[:n] + ("…" if len(t) > n else "")


def build(engine, batch) -> tuple[str, str, str]:
    runs = [engine.runs[r] for r in batch["run_ids"] if r in engine.runs]
    done = [r for r in runs if r["status"] == "done" and r.get("signal")]
    failed = [r for r in runs if r["status"] == "failed"]
    held = engine.book.data["positions"]
    rows = []
    for r in done:
        sig, prev = r["signal"], _prev_signal(engine, r)
        rows.append({"t": r["ticker"], "sig": sig, "prev": prev, "changed": bool(prev and prev != sig),
                     "held": r["ticker"] in held and held[r["ticker"]]["qty"] > 0, "trade": r.get("trade"),
                     "gist": _gist(r.get("decision", ""))})
    buys = [x for x in rows if x["sig"] in BUY]
    sells = [x for x in rows if x["sig"] in SELL]
    holds = [x for x in rows if x["sig"] not in BUY + SELL]
    label = batch.get("label") or f"Slot {batch.get('slot')}"
    subject = (f"[{label}]{' ABORTED' if batch.get('aborted') else ''} {len(buys)} buy · {len(sells)} sell · {len(holds)} hold"
               f" · {datetime.now().strftime('%d %b %H:%M')}")

    def line(x):
        extra = f" (was {x['prev']})" if x["changed"] else ""
        held_note = " · you hold it" if x["held"] else ""
        tr = x["trade"]
        paper = f" · paper {tr['side'].lower()} {tr['qty']:g} @ {tr['price']:g}" if tr and tr["side"] != "NONE" else ""
        return f"{x['t']}: {x['sig']}{extra}{held_note}{paper}\n    {x['gist']}"

    def block(title, xs):
        return f"{title} ({len(xs)})\n" + ("\n".join(line(x) for x in xs) if xs else "  none") + "\n"

    carried, reasons = batch.get("carried") or {}, batch.get("reasons") or {}
    n_watch = len(runs) + len(carried)
    head = ""
    if batch.get("aborted"):
        head = f"*** BATCH ABORTED: {batch['aborted']}. {len([r for r in runs if r['status'] != 'done'])} stocks were not analysed. ***\n"
    if carried:
        head += f"{len(carried)} of {n_watch} stocks unchanged since their last analysis (rating carried forward, no cost).\n"
    text = (head + f"{label} · analysts: {', '.join(batch.get('analysts', []))}\n"
            f"{len(done)} of {len(runs)} stocks analysed" + (f", {len(failed)} failed" if failed else "") + "\n\n"
            + block("BUY / ADD", sorted(buys, key=lambda x: x["sig"] != "Buy"))
            + "\n" + block("SELL / TRIM", sorted(sells, key=lambda x: x["sig"] != "Sell"))
            + "\nRating changes: " + (", ".join(f"{x['t']} {x['prev']}→{x['sig']}" for x in rows if x["changed"]) or "none")
            + "\nHold: " + (", ".join(x["t"] for x in holds) or "none")
            + ("\n\nWhy these were re-analysed:\n" + "\n".join(f"  {t}: {why}" for t, why in reasons.items()) if reasons else "")
            + ("\n\nUnchanged (last rating carried forward): " + ", ".join(f"{t} {v['signal']} ({v['date']})" for t, v in carried.items()) if carried else "")
            + ("\nFailed: " + ", ".join(r["ticker"] for r in failed) if failed else "")
            + "\n\nAI-generated research on a simulated paper book. Not financial advice; verify before trading.\n")
    page = f"<pre style='font:14px/1.5 ui-monospace,monospace;white-space:pre-wrap'>{html.escape(text)}</pre>"
    return subject, text, page


def _deliver(engine, subject, text, page, tag):
    DIGESTS.mkdir(parents=True, exist_ok=True)
    path = DIGESTS / f"{datetime.now().strftime('%Y%m%d_%H%M')}_{tag}.html"
    path.write_text(f"<h3>{html.escape(subject)}</h3>{page}")
    log.info("digest saved: %s", path)
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO"))
    if not (host and user and pw and to):
        engine._sched_log("Digest saved but not emailed: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and DIGEST_TO in Settings")
        return
    msg = EmailMessage()
    sender = os.environ.get("SMTP_FROM") or user
    if "@" not in sender:  # a bare display name: pair it with the login address
        sender = f"{sender} <{user}>"
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    msg.add_alternative(f"<html><body>{page}</body></html>", subtype="html")
    port = int(os.environ.get("SMTP_PORT") or 587)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, pw)
                s.send_message(msg)
        engine._sched_log(f"Digest emailed to {to}: {subject}")
    except Exception as e:
        engine._sched_log(f"Digest email failed ({e}); saved at {path}")


def send_digest(engine, batch):
    subject, text, page = build(engine, batch)
    _deliver(engine, subject, text, page, batch["id"])


def build_portfolio(engine) -> tuple[str, str, str]:
    book = engine.book
    snap = book.mark()  # live mark-to-market, also appends to the equity curve
    cur = snap["currency"]
    today = datetime.now().strftime("%Y-%m-%d")
    daily = book.data.setdefault("daily", {})
    prev_day = max((d for d in daily if d < today), default=None)
    prev_eq = daily[prev_day] if prev_day else snap["starting_cash"]
    since = prev_day or "start"
    day_chg = snap["equity"] - prev_eq
    daily[today] = round(snap["equity"], 2)
    book.save()
    new_trades = [t for t in book.data["trades"] if t.get("side") != "NONE" and t["date"] > (prev_day or "")]
    money = lambda v: f"{cur} {v:,.0f}"
    pos = sorted(snap["positions"], key=lambda p: -p["value"])
    lines = [f"Portfolio value: {money(snap['equity'])}",
             f"Change since {since}: {day_chg:+,.0f} ({day_chg / prev_eq * 100:+.2f}%)" if prev_eq else "",
             f"Total return: {snap['return_pct']:+.2f}% on {money(snap['starting_cash'])} starting capital",
             f"Cash: {money(snap['cash'])} ({snap['cash'] / snap['equity'] * 100:.0f}%) · Invested: {money(snap['market_value'])}",
             f"Rules: Buy up to {snap['max_weight'] * 100:.0f}% of equity per stock, Overweight half; auto-trading {'ON' if snap['auto_execute'] else 'OFF'}",
             "", f"POSITIONS ({len(pos)})"]
    lines += [f"{p['ticker']}: {p['qty']:g} @ {p['avg_price']:,.2f} → {p['price']:,.2f} · value {p['value']:,.0f}"
              f" · P&L {p['pnl']:+,.0f} ({p['pnl_pct']:+.1f}%)" for p in pos] or ["  none yet"]
    lines += ["", f"TRADES SINCE {since.upper()} ({len(new_trades)})"]
    lines += [f"{t['date']} {t['side']} {t['qty']:g} {t['ticker']} @ {t['price']:,.2f} ({t['rating']})" for t in new_trades] or ["  none"]
    lines += ["", "Simulated paper portfolio. Not financial advice."]
    text = "\n".join(l for l in lines if l is not None)
    subject = f"Portfolio {money(snap['equity'])} · {day_chg:+,.0f} ({snap['return_pct']:+.2f}% total) · {datetime.now().strftime('%d %b')}"
    page = f"<pre style='font:14px/1.5 ui-monospace,monospace;white-space:pre-wrap'>{html.escape(text)}</pre>"
    return subject, text, page


def send_portfolio(engine):
    subject, text, page = build_portfolio(engine)
    _deliver(engine, subject, text, page, "portfolio")
