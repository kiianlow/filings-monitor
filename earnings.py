#!/usr/bin/env python3
"""
earnings.py - Weekly earnings calendar digest

Sends one message every Monday morning listing which large cap companies
report during the coming week, plus the scheduled macro events.

You will not beat anyone to an earnings release. The value here is never
being caught holding a position into one you did not know about.

Run modes:
    python earnings.py            send the weekly digest, once per week
    python earnings.py --force    send it even if it already went out
    python earnings.py test       connectivity check, sends nothing
"""

import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

from monitor import (box, log, telegram, load_json, stamp, flush_outbox,
                     digest_already_sent, STATE_DIR)

USER_AGENT = os.environ.get("SEC_USER_AGENT", "FilingsMonitor")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

# Companies big enough that their results move the index, not just the stock.
# Edit freely. Anything not on this list is ignored by the digest.
WATCHLIST = {
    # Mega cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "ORCL", "CRM", "AMD", "ADBE", "INTC", "CSCO", "QCOM", "TXN", "MU", "AMAT",
    "LRCX", "KLAC", "ARM", "PLTR", "SNOW", "NOW", "PANW", "CRWD", "DDOG",
    "MDB", "NET", "SHOP", "UBER", "ABNB", "COIN", "XYZ", "PYPL", "SMCI",
    # Financials
    "BRK.B", "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP",
    "V", "MA", "SPGI", "CB", "PGR",
    # Healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN",
    "BMY", "GILD", "VRTX", "REGN", "ISRG", "MDT", "CVS",
    # Consumer and industrial
    "WMT", "COST", "HD", "PG", "KO", "PEP", "MCD", "NKE", "SBUX", "TGT",
    "LOW", "DIS", "NFLX", "CMCSA", "T", "VZ", "TMUS",
    "CAT", "DE", "BA", "GE", "HON", "UNP", "UPS", "FDX", "LMT", "RTX", "NOC",
    # Energy and materials
    "XOM", "CVX", "COP", "SLB", "EOG", "PSX", "MPC", "OXY", "LIN", "FCX",
}

# "XYZ" above is Block Inc, which traded as SQ until January 2025. SQ sat
# in this list unnoticed until 18 Nov 2026, so Block's results were never
# flagged. tests/test_earnings.py now fails if any ticker here stops being a
# listed company, which is what a rename looks like.


def norm_symbol(sym):
    """BRK.B, BRK-B and BRK/B are one share class written three ways."""
    return re.sub(r"[./\-\s]", "", str(sym or "").upper())


_WATCH = {norm_symbol(s): s for s in WATCHLIST}

STATE_FILE = STATE_DIR / "earnings_state.json"

NASDAQ_CAL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
FINNHUB_CAL = ("https://finnhub.io/api/v1/calendar/earnings"
               "?from={start}&to={end}")

session = requests.Session()


# ---------------------------------------------------------------
# Sources
# ---------------------------------------------------------------

def fetch_nasdaq(day, debug=False):
    """Nasdaq's public calendar. No key, but it is picky about headers."""
    url = NASDAQ_CAL.format(date=day.strftime("%Y-%m-%d"))
    try:
        r = session.get(url, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nasdaq.com/market-activity/earnings",
            "Origin": "https://www.nasdaq.com",
        })
    except requests.RequestException as e:
        log(f"nasdaq calendar unreachable :: {e}")
        return None
    if r.status_code != 200:
        log(f"nasdaq calendar HTTP {r.status_code}")
        return None
    try:
        rows = (r.json().get("data") or {}).get("rows") or []
    except ValueError:
        return None

    if debug:
        allsyms = [(r.get("symbol") or "").upper() for r in rows]
        print(f"    total companies reporting: {len(rows)}")
        print(f"    sample: {', '.join(allsyms[:12])}")

    out = []
    for row in rows:
        sym = _WATCH.get(norm_symbol(row.get("symbol")))
        if sym:
            out.append({
                "date": day.strftime("%Y-%m-%d"),
                "symbol": sym,
                "name": (row.get("name") or "")[:28],
                "when": row.get("time") or "",
            })
    return out


def fetch_finnhub(start, end):
    if not FINNHUB_KEY:
        return None
    # The key travels in a header, not the URL, so a logged error can never
    # carry it into the public Actions log.
    url = FINNHUB_CAL.format(start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"))
    try:
        r = session.get(url, timeout=30, headers={
            "User-Agent": USER_AGENT, "X-Finnhub-Token": FINNHUB_KEY})
    except requests.RequestException as e:
        log(f"finnhub calendar unreachable :: {type(e).__name__}")
        return None
    if r.status_code != 200:
        log(f"finnhub calendar HTTP {r.status_code}")
        return None
    try:
        rows = r.json().get("earningsCalendar") or []
    except ValueError:
        return None

    out = []
    for row in rows:
        sym = _WATCH.get(norm_symbol(row.get("symbol")))
        if sym:
            out.append({
                "date": row.get("date", ""),
                "symbol": sym,
                "name": "",
                "when": {"bmo": "pre", "amc": "post"}.get(
                    (row.get("hour") or "").lower(), ""),
            })
    return out


def collect_week(start, end):
    """
    Returns (rows, source_ok, days_missing).

    source_ok is False only when every source failed. An empty rows list with
    source_ok True is a genuinely quiet week, not a fault. The old version
    returned [] for both, so every quiet week in early September was reported
    as "Nasdaq blocked the request".

    days_missing counts weekdays Nasdaq did not answer for while answering
    for others. The digest says so, because a day that could not be read
    looks exactly like a day with no earnings.
    """
    found = []
    day = start
    nasdaq_ok = False
    missing = 0
    while day <= end:
        if day.weekday() < 5:               # weekdays only
            rows = fetch_nasdaq(day)
            if rows is not None:
                nasdaq_ok = True
                found.extend(rows)
            else:
                missing += 1
            time.sleep(0.6)
        day += timedelta(days=1)

    if nasdaq_ok:
        return found, True, missing

    log("nasdaq unavailable, trying finnhub")
    rows = fetch_finnhub(start, end)
    if rows is not None:
        return rows, True, 0
    return [], False, 0


# ---------------------------------------------------------------
# Digest
# ---------------------------------------------------------------

def build_digest(start, end, rows):
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)

    out = []
    day = start
    while day <= end:
        key = day.strftime("%Y-%m-%d")
        if day.weekday() < 5:
            names = sorted({r["symbol"] for r in by_day.get(key, [])})
            if names:
                label = day.strftime("%a %d %b")
                shown = ", ".join(names[:9])
                if len(names) > 9:
                    shown += f"  +{len(names) - 9}"
                out.append((label, shown))
        day += timedelta(days=1)
    return out


def coming_week(today):
    """Monday and Friday of the week the digest covers: this week if today
    is a Monday, otherwise the next one."""
    start = today + timedelta(days=(7 - today.weekday()) % 7)
    return start, start + timedelta(days=4)


def run_digest(force=False):
    flush_outbox()          # anything an earlier run could not deliver
    today = datetime.now(timezone.utc).date()
    start, end = coming_week(today)
    week = start.isoformat()
    span = f"{start.strftime('%d %b')} to {end.strftime('%d %b')}"

    # Send once per week. This job is now started by Kick.gs at 07:00
    # Singapore as well as by cron, with later cron times as a backup, so it
    # can run several times on a Monday.
    state = load_json(STATE_FILE, {})
    if not force and digest_already_sent(state, week):
        log(f"earnings: the digest for the week of {week} already went out, "
            "nothing to do (use --force to send it again)")
        stamp("earnings_state.json")
        return

    rows, source_ok, days_missing = collect_week(start, end)

    if not source_ok:
        stamp("earnings_state.json", error="Nasdaq and Finnhub both failed",
              failed_week=week)
        # Say so once. The backup runs try again quietly, and the digest
        # goes out as soon as one of them gets through.
        if force or state.get("failed_week") != week:
            telegram(box("WEEK AHEAD - EARNINGS UNAVAILABLE", [
                ("PRIORITY", "MEDIUM"),
                ("WEEK", span),
                ("PROBLEM", "Earnings calendar could not be reached"),
                ("NEXT", "Trying again automatically over the next hours"),
                ("ACTION", "If nothing follows: Actions tab, run Diagnostics"),
            ]))
        return

    digest = build_digest(start, end, rows)
    sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    note = (f"{days_missing} of 5 days could not be read, may be incomplete"
            if days_missing else "")

    if not digest:
        # A real quiet week. Say so plainly and count it as a healthy run.
        telegram(box("WEEK AHEAD - EARNINGS", [
            ("WEEK", span),
            ("STATUS", f"None of the {len(WATCHLIST)} large caps report this week"),
            ("NOTE", note),
        ]))
        stamp("earnings_state.json", count=0, sent_week=week, sent_at=sent_at)
        log("earnings: quiet week, no watchlist companies reporting")
        return

    body = [("WEEK", span)]
    body += digest
    body.append(("WATCHING", f"{len(WATCHLIST)} large caps"))
    body.append(("NOTE", note))

    telegram(box("WEEK AHEAD - EARNINGS", body,
                 footer="You cannot beat the market to a release. "
                        "This is so you are not holding into one blind."))

    stamp("earnings_state.json", count=len(rows), sent_week=week,
          sent_at=sent_at)
    log(f"earnings digest sent, {len(rows)} entries")


def connectivity_test():
    print("=== EARNINGS CONNECTIVITY TEST ===")
    day = datetime.now(timezone.utc).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)

    print(f"\n[Nasdaq] calendar for {day}")
    rows = fetch_nasdaq(day, debug=True)
    if rows is None:
        print("  FAILED. Nasdaq blocked the request or changed its format.")
        print("  Fallback: add a free FINNHUB_KEY secret.")
    else:
        print(f"  OK. {len(rows)} watchlist companies reporting that day.")
        for r in rows[:5]:
            print(f"    {r['symbol']:6} {r['name']}")

    print("\n[Nasdaq] full week probe")
    d, _ = coming_week(datetime.now(timezone.utc).date())
    total = 0
    for i in range(5):
        day_i = d + timedelta(days=i)
        got = fetch_nasdaq(day_i)
        n = len(got) if got is not None else 0
        total += n
        print(f"    {day_i.strftime('%a %d %b')}: {n} watchlist companies")
        time.sleep(0.6)
    print(f"    week total: {total}")

    print(f"\n[Finnhub] key present: {'yes' if FINNHUB_KEY else 'no'}")
    if FINNHUB_KEY:
        fh = fetch_finnhub(day, day + timedelta(days=5))
        print("  FAILED." if fh is None else f"  OK. {len(fh)} entries.")

    print("\n=== END ===")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return
    run_digest(force="--force" in sys.argv[1:])


if __name__ == "__main__":
    main()
