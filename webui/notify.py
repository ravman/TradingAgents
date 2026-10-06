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
    subject = (f"[{label}] {len(buys)} buy · {len(sells)} sell · {len(holds)} hold"
               f" · {datetime.now().strftime('%d %b %H:%M')}")

    def line(x):
        extra = f" (was {x['prev']})" if x["changed"] else ""
        held_note = " · you hold it" if x["held"] else ""
        tr = x["trade"]
        paper = f" · paper {tr['side'].lower()} {tr['qty']:g} @ {tr['price']:g}" if tr and tr["side"] != "NONE" else ""
        return f"{x['t']}: {x['sig']}{extra}{held_note}{paper}\n    {x['gist']}"

    def block(title, xs):
        return f"{title} ({len(xs)})\n" + ("\n".join(line(x) for x in xs) if xs else "  none") + "\n"

    text = (f"{label} · analysts: {', '.join(batch.get('analysts', []))}\n"
            f"{len(done)} of {len(runs)} stocks analysed" + (f", {len(failed)} failed" if failed else "") + "\n\n"
            + block("BUY / ADD", sorted(buys, key=lambda x: x["sig"] != "Buy"))
            + "\n" + block("SELL / TRIM", sorted(sells, key=lambda x: x["sig"] != "Sell"))
            + "\nRating changes: " + (", ".join(f"{x['t']} {x['prev']}→{x['sig']}" for x in rows if x["changed"]) or "none")
            + "\nHold: " + (", ".join(x["t"] for x in holds) or "none")
            + ("\nFailed: " + ", ".join(r["ticker"] for r in failed) if failed else "")
            + "\n\nAI-generated research on a simulated paper book. Not financial advice; verify before trading.\n")
    page = f"<pre style='font:14px/1.5 ui-monospace,monospace;white-space:pre-wrap'>{html.escape(text)}</pre>"
    return subject, text, page


def send_digest(engine, batch):
    subject, text, page = build(engine, batch)
    DIGESTS.mkdir(parents=True, exist_ok=True)
    path = DIGESTS / f"{datetime.now().strftime('%Y%m%d_%H%M')}_{batch['id']}.html"
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
