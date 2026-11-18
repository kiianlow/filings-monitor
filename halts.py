#!/usr/bin/env python3
"""
halts.py - Nasdaq trading halt monitor

Moved off Google Apps Script on 17 Sep 2026. Apps Script gives no way to set
a request timeout, so when the Nasdaq feed stalled under load UrlFetchApp
blocked until Google killed the execution at the 6 minute ceiling. The
MAX_ALERTS_PER_RUN and SOFT_DEADLINE_MS caps never helped because the time
was burned before the item loop started. Three failures in one session, all
during US market hours.

Python sets timeout=15 and a stalled connection dies cleanly.

The trade-off accepted in the move: GitHub cron drifts 5 to 20 minutes, so a
T1 halt alert can arrive late. This is the only real-time signal in the
system, so that cost is real.

Run modes:
    python halts.py           normal run
    python halts.py test      connectivity check, sends nothing
"""

import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

from monitor import (box, log, telegram, load_json, save_json, stamp,
                     flush_outbox, find_all, find_one, prune_csv,
                     STATE_DIR, DATA_DIR, NY_TZ, SGT)

FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

SEEN_FILE = STATE_DIR / "halts_seen.json"
STATE_FILE = STATE_DIR / "halts_state.json"

# Added 18 Nov 2026. A history of halts, so the later analysis pass (price
# move after each alert type) has halts to work with. Written only by this
# job, so no other workflow ever commits the same file. alerts.csv stays
# monitor.py's alone.
#
# Every new halt is recorded except the ordinary volatility pauses (LUDP,
# LUDS), which can run to hundreds on a busy day and say nothing on their
# own. "alerted" says whether it buzzed. Kept for a rolling year.
HALTS_CSV = DATA_DIR / "halts.csv"
HALTS_HEADER = ["timestamp", "symbol", "name", "code", "reason",
                "halted", "resumes", "market", "alerted"]
HALTS_CSV_SKIP = {"LUDP", "LUDS"}
HALTS_CSV_DAYS = 365

SEEN_MAX = 4000
MAX_ALERTS_PER_RUN = 10

# Every reason code the feed uses, kept for labelling. Wording follows
# Nasdaq's own table (nasdaqtrader.com/trader.aspx?id=tradehaltcodes),
# checked 18 Nov 2026.
HALT_CODES = {
    "T1":   "News pending",
    "T2":   "News released",
    "T3":   "News and resumption times",
    "T5":   "Single stock pause, 10% move in 5 minutes",
    "T6":   "Extraordinary market activity",
    "T7":   "Single stock pause, quotation only",
    "T8":   "ETF halt",
    "T12":  "Additional information requested",
    "H4":   "Non-compliance with listing requirements",
    "H9":   "Not current in required filings",
    "H10":  "SEC trading suspension",
    "H11":  "Regulatory concern",
    "O1":   "Operations halt",
    "IPO1": "IPO not yet trading",
    "M1":   "Corporate action",
    "M2":   "Quotation not available",
    "D":    "Security deletion or delisting",
    "LUDP": "Volatility pause, single stock",
    "LUDS": "Volatility pause, straddle condition",
    "M":    "Volatility pause, single stock (NYSE or other exchange listed)",
    "MWC1": "MARKET-WIDE CIRCUIT BREAKER, LEVEL 1",
    "MWC2": "MARKET-WIDE CIRCUIT BREAKER, LEVEL 2",
    "MWC3": "MARKET-WIDE CIRCUIT BREAKER, LEVEL 3",
    "MWC0": "MARKET-WIDE CIRCUIT BREAKER, carried over from the previous day",
    "MWCQ": "Market-wide circuit breaker ended, trading resuming",
}

# The whole market stopping. These rows are sent as one message of their
# own, however many securities the feed lists under the code.
MARKET_WIDE = {"MWC0", "MWC1", "MWC2", "MWC3", "MWCQ"}

# Only these produce an alert. Trimmed 18 Sep 2026 because volume was too
# high: one run had 25 alertable items out of 68.
#
# T2 was the biggest offender. It means news released, which is the
# resumption of a T1, so every halt was arriving twice for no extra
# information. D fires on routine delistings most days. T12, H4, H9 and H11
# are compliance and filing issues that show up steadily and rarely matter.
#
# Add a code back to this set to widen it again.
ALERT_ON = {
    "T1",    # news pending, the only one that says something is coming
    "H10",   # SEC trading suspension, rare and serious
} | MARKET_WIDE

# Corrected 18 Nov 2026. "M" used to be in ALERT_ON, labelled "Volatility
# trading pause, market wide". That label was wrong. Nasdaq defines M as a
# volatility pause in one exchange-listed stock: the same event as LUDP,
# which is excluded below as noise, only for NYSE-listed names. It made up
# 389 of the 601 alertable halts in the ledger (about 7 pings a day, 50 on
# the worst day), each one announcing a market-wide halt that had not
# happened. The real market-wide codes, MWC0 to MWC3, were not watched at
# all, so the event M was added for would never have alerted.
#
# LUDP and LUDS, ordinary volatility pauses, were never included. On a
# volatile day those alone produce hundreds.

# Of the alerting codes, these are HIGH priority.
HIGH_CODES = {"T1", "H10"} | MARKET_WIDE

# Referenced by fetch_feed but was never defined in the deployed file, so
# every run from the morning of 24 Sep 2026 died with a NameError before
# fetching anything. That is what stalled halts for most of a day.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("application/rss+xml, application/xml;q=0.9, "
               "text/xml;q=0.9, */*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
}

session = requests.Session()


def looks_like_feed(text):
    return "<rss" in (text or "")[:500]


def fetch_feed(tries=3):
    """
    A stalled connection dies at 15 seconds instead of eating the run.

    Updated 18 Nov 2026. Nasdaq's bot filter (Incapsula) now and then answers
    with a 200 and a tiny HTML challenge page instead of the feed: 18 of
    1,500 runs since late October. That used to end the run, so the halt
    waited for the next one, about 5 minutes later. A challenge page is now
    treated like a failed request: wait a moment, drop the cookies the
    filter set, try again. The last answer is still returned so run() can
    report what came back.
    """
    last = None
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(2 * attempt)
        try:
            r = session.get(FEED_URL, timeout=15, headers=BROWSER_HEADERS)
        except requests.RequestException as e:
            log(f"halts: request failed ({attempt}/{tries}) :: {e}")
            continue
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200:
            log(f"halts: HTTP {r.status_code} ({ctype})")
            continue
        log(f"halts: HTTP 200, {len(r.text)} chars, content-type: {ctype}")
        if looks_like_feed(r.text):
            return r.text
        last = r.text
        session.cookies.clear()
        log(f"halts: not the feed (bot check?), try {attempt}/{tries}")
    return last


def txt(el, name):
    """The feed namespaces its fields as ndm:*, so match on local name."""
    found = find_one(el, name)
    return (found.text or "").strip() if found is not None else ""


def parse(xml_text):
    # Accept bytes or str. Bytes are preferred: ElementTree then honours the
    # encoding declared in the document itself.
    if isinstance(xml_text, bytes):
        xml_text = xml_text.lstrip(b"\xef\xbb\xbf")      # strip UTF-8 BOM
    else:
        # A BOM that was mis-decoded arrives as three literal characters, so
        # lstrip of \ufeff alone will not clear it. Cut anything before the
        # first "<" instead, which handles every variant.
        cut = xml_text.find("<")
        if 0 < cut <= 8:
            xml_text = xml_text[cut:]
        xml_text = xml_text.lstrip("\ufeff").lstrip()

    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError) as e:
        # A 200 that is not XML means the server sent something else, usually
        # a block page or a redirect. Show the head of the body so the cause
        # is visible instead of guessed at.
        raw = (xml_text.decode("utf-8", "replace")
               if isinstance(xml_text, bytes) else (xml_text or ""))
        head = raw[:300].replace("\n", " ").strip()
        log(f"halts: unparseable feed :: {e}")
        log(f"halts: body starts with: {head}")
        return None

    out = []
    for item in find_all(root, "item"):
        symbol = txt(item, "IssueSymbol")
        # A market-wide halt may be published without a symbol: it is not
        # about one stock. Keep those; drop only symbol-less single halts.
        if not symbol and txt(item, "ReasonCode").upper() not in MARKET_WIDE:
            continue
        out.append({
            "symbol": symbol,
            "name": txt(item, "IssueName"),
            "code": txt(item, "ReasonCode").upper(),
            "halt_date": txt(item, "HaltDate"),
            "halt_time": txt(item, "HaltTime"),
            "market": txt(item, "Market"),
            "resume_date": txt(item, "ResumptionDate"),
            "resume_time": txt(item, "ResumptionTradeTime"),
            "threshold": txt(item, "PauseThresholdPrice"),
        })
    return out


def key_of(h):
    return f"{h['symbol']}|{h['halt_date']}|{h['halt_time']}|{h['code']}"


def market_key(h):
    """One key per market-wide code per day."""
    return f"MW|{h['code']}|{h['halt_date']}"


def when_label(date_str, time_str):
    """
    The feed gives New York wall-clock time with no zone on it, e.g.
    "11/17/2026" and "09:41:23". Shown bare it reads as Singapore time, which
    it is not. Returns "09:41 ET 17 Nov (22:41 SGT)". Anything that does not
    parse is passed through as the feed wrote it.
    """
    raw = " ".join(x for x in (time_str, date_str) if x)
    try:
        ny = datetime.strptime(f"{date_str} {time_str}",
                               "%m/%d/%Y %H:%M:%S").replace(tzinfo=NY_TZ)
    except (ValueError, TypeError):
        return raw
    sg = ny.astimezone(SGT)
    out = f"{ny:%H:%M} ET {ny.day} {ny:%b} ({sg:%H:%M} SGT"
    if sg.date() != ny.date():
        out += f" {sg.day} {sg:%b}"
    return out + ")"


def send_one(h):
    code = h["code"]
    priority = "HIGH" if code in HIGH_CODES else "MEDIUM"
    resume = when_label(h["resume_date"], h["resume_time"])

    rows = [
        ("PRIORITY", priority),
        ("TICKER", h["symbol"]),
        ("COMPANY", h["name"][:38]),
        ("EVENT", f"{code} - {HALT_CODES.get(code, 'Halt')}"),
        ("HALTED", when_label(h["halt_date"], h["halt_time"])),
        ("MARKET", h["market"]),
        ("THRESHOLD", h["threshold"]),
        ("RESUME", resume or "not yet announced"),
    ]

    return telegram(
        box("TRADING HALT", rows,
            link="https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
            footer=("T1 means news is pending. Watch for the release."
                    if code == "T1" else "")),
        silent=(priority != "HIGH"))


def send_market_wide(halts):
    """
    One message for a market-wide circuit breaker, whatever number of rows
    the feed carries for it. The feed's layout for this event has never been
    observed (the last one was March 2020), so nothing here depends on it:
    the message is built from the code alone.
    """
    codes = sorted({h["code"] for h in halts})
    first = halts[0]
    rows = [("PRIORITY", "HIGH")]
    rows += [("EVENT", f"{c} - {HALT_CODES.get(c, 'Market-wide halt')}")
             for c in codes]
    rows += [
        ("HALTED", when_label(first["halt_date"], first["halt_time"])),
        ("RESUME", when_label(first["resume_date"], first["resume_time"])
                   or "not yet announced"),
        ("ROWS", f"{sum(h.get('rows', 1) for h in halts)} in the feed "
                 "under this code"),
    ]
    resumed = codes == ["MWCQ"]
    return telegram(
        box("MARKET-WIDE HALT ENDED" if resumed else "MARKET-WIDE TRADING HALT",
            rows,
            link="https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
            footer=("" if resumed else
                    "Level 1 and 2 pause all US stock trading for 15 minutes. "
                    "Level 3 closes the market for the day.")),
        silent=False)


def send_batch(halts):
    """Several at once usually means one issuer with multiple securities."""
    rows = [(h["symbol"], f"{h['code']}  {h['name'][:26]}")
            for h in halts[:20]]
    if len(halts) > 20:
        rows.append(("...", f"+{len(halts) - 20} more"))

    return telegram(
        box(f"{len(halts)} TRADING HALTS", rows,
            link="https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
            footer="Grouped because several halted at once. Multiple "
                   "tickers of one issuer halt together."),
        silent=not any(h["code"] in HIGH_CODES for h in halts))


def run():
    flush_outbox()          # anything an earlier run could not deliver
    xml_text = fetch_feed()
    if xml_text is None:
        log("halts: feed unreachable this run")
        stamp("halts_state.json", error="feed unreachable")
        return

    halts = parse(xml_text)
    if halts is None:
        stamp("halts_state.json", error="feed not parseable as XML")
        return

    stamp("halts_state.json", items_in_feed=len(halts))

    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)

    fresh, new_keys, new_halts = [], [], []
    market_rows = {}
    for h in halts:
        if h["code"] in MARKET_WIDE:
            market_rows.setdefault(market_key(h), []).append(h)
            continue
        k = key_of(h)
        if k in seen_set:
            continue
        seen_set.add(k)
        new_keys.append(k)
        new_halts.append(h)
        if h["code"] in ALERT_ON:
            fresh.append(h)

    # One key per circuit-breaker level per day, however many rows the feed
    # lists under it. Keyed per row, a halt of the whole market (thousands
    # of rows) would overflow the 4,000-key seen list and alert again on
    # every run, pushing real T1 keys out as it went. Added 18 Nov 2026.
    for k, rows in market_rows.items():
        if k in seen_set:
            continue
        seen_set.add(k)
        new_keys.append(k)
        first = dict(rows[0], rows=len(rows))
        new_halts.append(first)
        if first["code"] in ALERT_ON:
            fresh.append(first)

    market = [h for h in fresh if h["code"] in MARKET_WIDE]
    single = [h for h in fresh if h["code"] not in MARKET_WIDE]

    if market:
        send_market_wide(market)
    if single:
        if len(single) <= 3:
            for h in single[:MAX_ALERTS_PER_RUN]:
                send_one(h)
        else:
            send_batch(single[:MAX_ALERTS_PER_RUN * 5])

    # The history must never stop the seen list being saved: a crash here
    # would re-send every halt above on the next run.
    try:
        record_history(new_halts)
    except Exception as e:
        log(f"halts: history not written :: {e}")

    seen.extend(new_keys)
    save_json(SEEN_FILE, seen[-SEEN_MAX:])

    log(f"halts: {len(halts)} in feed, {len(new_keys)} new, "
        f"{len(fresh)} alertable")


def record_history(new_halts):
    rows = [h for h in new_halts if h["code"] not in HALTS_CSV_SKIP]
    if not rows:
        return
    DATA_DIR.mkdir(exist_ok=True)
    fresh_file = not HALTS_CSV.exists()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with HALTS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if fresh_file:
            w.writerow(HALTS_HEADER)
        for h in rows:
            w.writerow([
                now, h["symbol"], h["name"][:60], h["code"],
                HALT_CODES.get(h["code"], ""),
                when_label(h["halt_date"], h["halt_time"]),
                when_label(h["resume_date"], h["resume_time"]),
                h["market"], "yes" if h["code"] in ALERT_ON else "no",
            ])
    prune_history()


def prune_history():
    """Trim to a rolling year, but only rewrite the file when the oldest row
    has actually expired, so most runs leave it untouched."""
    with HALTS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        first = next(reader, None)
    if not first:
        return
    try:
        oldest = datetime.fromisoformat(first[0])
    except ValueError:
        return
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    if oldest < datetime.now(timezone.utc) - timedelta(days=HALTS_CSV_DAYS):
        prune_csv(HALTS_CSV, HALTS_HEADER, HALTS_CSV_DAYS)


def connectivity_test():
    print("=== NASDAQ HALTS CONNECTIVITY TEST ===")
    et = datetime.now(NY_TZ).hour
    print(f"  US Eastern: {et:02d}:xx  "
          f"({'market hours' if 9 <= et < 16 else 'outside market hours'})")

    print("\n[variants] what each URL and header set returns")
    variants = [
        ("default headers", FEED_URL, {}),
        ("browser UA", FEED_URL, {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
        }),
        ("http not https", FEED_URL.replace("https://", "http://"), {}),
        ("no www", FEED_URL.replace("www.", ""), {}),
        ("rss.aspx alt feed", "https://www.nasdaqtrader.com/rss.aspx"
                              "?feed=currenthalts", {}),
    ]
    for label, url, hdrs in variants:
        base = {"User-Agent": os.environ.get("SEC_USER_AGENT",
                                             "FilingsMonitor")}
        base.update(hdrs)
        try:
            r = session.get(url, timeout=15, headers=base,
                            allow_redirects=True)
        except requests.RequestException as e:
            print(f"    {label:20} ERROR {str(e)[:60]}")
            continue
        body = r.text or ""
        looks_xml = body.lstrip().startswith("<?xml") or \
            body.lstrip().startswith("<rss")
        print(f"    {label:20} HTTP {r.status_code}  {len(body):7} chars  "
              f"xml={looks_xml}  ct={r.headers.get('Content-Type','')[:28]}")
        if not looks_xml and body:
            print(f"        starts: {body[:110].replace(chr(10), ' ').strip()}")
        if r.history:
            print(f"        redirected via {len(r.history)} hop(s) to {r.url}")

    xml_text = fetch_feed()
    if xml_text is None:
        print("\n  FAILED. Feed unreachable from this runner.")
        print("\n=== END ===")
        return

    halts = parse(xml_text)
    if halts is None:
        print("  FAILED to parse.")
        print("\n=== END ===")
        return
    print(f"  OK. {len(halts)} items in feed ({len(xml_text)} bytes)")

    codes = {}
    for h in halts:
        codes[h["code"]] = codes.get(h["code"], 0) + 1
    print(f"  reason codes: {codes}")

    alertable = [h for h in halts if h["code"] in ALERT_ON]
    print(f"  would alert on {len(alertable)} of them")
    for h in alertable[:5]:
        print(f"      {h['symbol']:8} {h['code']:4} "
              f"{h['halt_time']:10} {h['name'][:34]}")

    state = load_json(STATE_FILE, {})
    print(f"  last successful run: {state.get('last_ok', 'never')}")
    print(f"  seen ledger: {len(load_json(SEEN_FILE, []))} keys")
    print("\n=== END ===")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return
    run()


if __name__ == "__main__":
    main()
