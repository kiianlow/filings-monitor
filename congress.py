#!/usr/bin/env python3
"""
congress.py - Congressional stock trade monitor

Watches periodic transaction reports (PTRs) filed under the STOCK Act.

  House  - disclosures-clerk.house.gov publishes an annual ZIP containing an
           XML index of every filing. Trade details live in linked PDFs.
  Senate - efdsearch.senate.gov requires accepting a terms page to obtain a
           session cookie before it will return any search results.

Members must file within 45 days of a trade, so every alert here is weeks
old by definition. The filing becoming public IS the event. This puts you
level with everyone else watching, not ahead of them.

Run modes:
    python congress.py            normal run
    python congress.py test       connectivity check, sends nothing
"""

import io
import os
import re
import csv
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

import requests

from monitor import (
    box, log, telegram, money, dmy, days_between, flush_outbox, Ledger,
    load_json, save_json, append_csv, STATE_DIR, DATA_DIR,
)

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------

USER_AGENT = os.environ.get("SEC_USER_AGENT", "FilingsMonitor")

# Disclosure bands. Members report ranges, never exact figures.
PUSH_MIN_AMOUNT = 50_001       # below this, log only
HIGH_MIN_AMOUNT = 500_001      # at or above this, always push

MAX_FILINGS_PER_RUN = 25
MAX_ALERTS_PER_RUN = 15

# Stop starting new filings after this many seconds; the job is killed at 20
# minutes and a killed job saves nothing, so its alerts repeat. 25 PDFs at a
# 60-second timeout could overrun. Unread filings are not marked seen, so
# the next run takes them. Added 18 Nov 2026.
RUN_BUDGET_SECONDS = 14 * 60
RUN_STARTED = time.monotonic()
SEEN_MAX = 30_000

# A PTR whose PDF could not be downloaded is tried again on later runs, this
# many times, before it is reported as "open manually". Before 18 Nov 2026 a
# single failed download sent that alert straight away and the trades inside
# the filing were never read or logged.
MAX_PDF_TRIES = 6
# How many pages of a PTR are read. Was 12, which silently cut off the end
# of the occasional very long filing.
MAX_PDF_PAGES = 40

# Only alert on filings published recently. The House index holds the whole
# year, so without this the first run works through January and alerts you
# about trades disclosed eight months ago. Older filings are still logged.
MAX_FILING_AGE_DAYS = 10

# eFD returns 503 to GitHub's IP range regardless of headers. House coverage
# is unaffected and covers the whole lower chamber. Flip this to True to retry
# Senate later, or if you ever move this to a different host.
SENATE_ENABLED = os.environ.get("SENATE_ENABLED", "").lower() in ("1", "true", "yes")

# Alert on tradeable equities only. Congress must disclose everything, so the
# raw feed is mostly municipal bonds, treasury notes, private LLCs and real
# estate partnerships. None of that is actionable and it buries the stock
# trades. Everything still lands in the CSV, this only governs what is pushed.
EQUITY_ONLY = True

# Members whose trades always push at HIGH, even below the dollar floor.
#
# Matched on SURNAME, because the House writes legal names: "Daniel
# Crenshaw", "Michael Patrick Guest", "Terri A. Sewell". Full-name matching
# missed Crenshaw and Meuser for that reason, and plain substring matching let
# "scott" match any Scott. Where a surname is common, the allowed first names
# follow it, so "Julie Johnson" does not match the Speaker and "Scott Peters"
# does not match Senator Rick Scott.
#
# Reviewed 25 Sep 2026 against the Unusual Whales yearly rankings. Those
# measure change in portfolio value over the year, not trade win rate, and
# they do not persist: none of the 2025 top 10 were in the 2024 top 10.
# Several top performers barely trade (Norcross logged no trades in 2026,
# Davidson one), so watching them costs almost nothing in noise.
#
#   surname             allowed first names (None = any)
NOTABLE_MEMBERS = {
    # Long-watched
    "pelosi":            None,
    "greene":            {"marjorie"},
    "khanna":            None,
    "crenshaw":          None,
    "gottheimer":        None,
    "mccaul":            None,
    "boebert":           None,
    "massie":            None,
    "waters":            {"maxine"},
    "jeffries":          None,
    "johnson":           {"mike", "michael", "james"},     # the Speaker
    "hern":              {"kevin"},
    "meuser":            None,
    "garbarino":         None,
    "kustoff":           None,
    # 2025 top ten, House
    "davidson":          {"warren"},
    "norcross":          None,
    "sewell":            {"terri"},
    "steil":             None,
    "lalota":            None,
    "guest":             {"michael"},
    "evans":             {"dwight"},
    # 2024 top ten, House
    "williams":          {"roger"},
    "rouzer":            None,
    "wasserman schultz": None,
    "mcgarvey":          None,
    "sessions":          {"pete", "peter"},
    # Requested, sitting member
    "babin":             None,
    # Senate. Only takes effect if SENATE_ENABLED is switched on.
    "tuberville":        None,
    "wyden":             None,
    "schumer":           None,
    "mcconnell":         None,
    "scott":             {"rick", "richard", "tim", "timothy"},
    "warner":            {"mark"},
    "moreno":            {"bernie", "bernardo"},
    "banks":             {"jim", "james"},
    "gallego":           {"ruben"},
    "padilla":           {"alex", "alejandro"},
    "collins":           {"susan"},
    "fetterman":         None,
}

_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd"}


def _member_tokens(name):
    s = str(name or "").lower().replace("-", " ")
    s = re.sub(r"[^a-z ]", " ", s)
    toks = [t for t in s.split() if len(t) > 1 and t not in _NAME_SUFFIXES]
    # "Scott Scott Franklin": drop an immediately repeated word.
    out = []
    for t in toks:
        if not out or out[-1] != t:
            out.append(t)
    return out


def is_notable_member(name):
    """Surname match, with a first-name check where the surname is common."""
    toks = _member_tokens(name)
    if len(toks) < 2:
        return False
    joined = " ".join(toks)
    for surname, firsts in NOTABLE_MEMBERS.items():
        if joined.endswith(" " + surname):
            if firsts is None or toks[0] in firsts:
                return True
    return False


# Asset names that mean debt or an insurance product rather than a stock.
#
# Until 18 Nov 2026 this list also held "LLC", "L.P.", "limited partnership"
# and "partners", to keep private vehicles out. But a private vehicle has no
# ticker and is already excluded by the ticker test below, so those words
# only ever blocked LISTED partnerships: 13 real trades in the ledger,
# including Brookfield Renewable Partners (BEP), Energy Transfer (ET),
# Enterprise Products Partners (EPD) and Perella Weinberg Partners (PWP).
NON_EQUITY_RE = re.compile(
    r"\b(bond|bonds|note|notes|treasury|municipal|muni|revenue|debenture|"
    r"certificate of deposit|money market|mortgage|obligation|"
    r"variable annuity|whole life|529|savings plan|"
    r"corporate credit|fixed income)\b", re.IGNORECASE)


# The filer's own asset-type code, printed after each House trade as "[ST]".
# Codes per fd.house.gov/reference/asset-type-codes.aspx. GS (government
# securities) and CS (corporate bonds and notes) are debt whatever the name
# says. Added 18 Nov 2026.
NON_EQUITY_TAGS = {"GS", "CS"}


def is_equity(trade):
    """A listed stock or ETF has a ticker and does not read like debt."""
    if not trade.get("ticker"):
        return False
    if trade.get("asset_type") in NON_EQUITY_TAGS:
        return False
    if NON_EQUITY_RE.search(trade.get("asset", "")):
        return False
    return True


def kind_label(trade):
    """'calls', 'puts' or 'options' for an option trade, else ''."""
    if trade.get("asset_type") != "OP":
        return ""
    return trade.get("option_side") or "options"

SEEN_FILE = STATE_DIR / "congress_seen.json"
HEALTH_FILE = STATE_DIR / "congress_health.json"
RETRY_FILE = STATE_DIR / "congress_retry.json"
TRADES_CSV = DATA_DIR / "congress_trades.csv"

# asset_type added 18 Nov 2026 as the last column, so every older row stays
# as it is: a reader sees it empty for trades logged before then.
TRADES_HEADER = ["timestamp", "chamber", "member", "ticker", "asset",
                 "action", "amount_low", "amount_high", "trade_date",
                 "filed_date", "lag_days", "priority", "link", "asset_type"]

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
SENATE_HOME = "https://efdsearch.senate.gov/search/home/"
SENATE_SEARCH = "https://efdsearch.senate.gov/search/report/data/"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# eFD returns 503 to non-browser clients. The House Clerk does not care, so
# this header set is applied to Senate requests only.
SENATE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

alerts_sent = 0


# ---------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------

AMOUNT_RE = re.compile(r"\$([\d,]+)\s*(?:-|–|—|to)\s*\$([\d,]+)")


def parse_amount(text):
    """'$1,001 - $15,000' -> (1001, 15000). Returns (0, 0) if unreadable."""
    if not text:
        return 0, 0
    m = AMOUNT_RE.search(str(text))
    if not m:
        single = re.search(r"\$([\d,]+)", str(text))
        if single:
            v = int(single.group(1).replace(",", ""))
            return v, v
        return 0, 0
    return (int(m.group(1).replace(",", "")),
            int(m.group(2).replace(",", "")))


def amount_label(low, high):
    if not low and not high:
        return "not disclosed"
    if low == high:
        return money(low)
    return f"{money(low)} - {money(high)}"


def classify(low):
    if low >= HIGH_MIN_AMOUNT:
        return "HIGH"
    if low >= PUSH_MIN_AMOUNT:
        return "MEDIUM"
    return "LOW"


def action_label(code):
    """PTR transaction codes."""
    c = str(code).strip().upper()
    return {
        "P": "BUY",
        "S": "SELL",
        "S (PARTIAL)": "SELL (partial)",
        "E": "EXCHANGE",
    }.get(c, c or "unknown")


# ---------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------

def log_trade(chamber, member, ticker, asset, action, low, high,
              trade_date, filed_date, priority, link, asset_type=""):
    """Every trade goes to the CSV, regardless of whether it is alerted."""
    lag = days_between(trade_date, filed_date) if trade_date and filed_date else ""
    append_csv(TRADES_CSV, [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        chamber, member, ticker, asset, action, low, high,
        trade_date, filed_date, lag, priority, link, asset_type,
    ])
    return lag


def ensure_trades_csv():
    """
    Create the trade log, or bring an older file's header up to date. Only
    the first line is replaced; every row below it is kept byte for byte.
    """
    if not TRADES_CSV.exists():
        with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADES_HEADER)
        return
    with TRADES_CSV.open(newline="", encoding="utf-8") as f:
        first = f.readline()
        rest = f.read()
    current = next(csv.reader([first]), [])
    if current == TRADES_HEADER or current != TRADES_HEADER[:len(current)]:
        return          # up to date, or not a layout this code knows
    buf = io.StringIO()
    csv.writer(buf).writerow(TRADES_HEADER)
    tmp = TRADES_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        f.write(buf.getvalue() + rest)
    os.replace(tmp, TRADES_CSV)


def send_filing(chamber, member, party_state, filed_date, link, trades):
    """
    One message per FILING, not per trade.

    A single PTR routinely discloses a dozen trades. Sending each as its own
    alert produced a wall of near identical messages all pointing at the same
    PDF, which is how a useful feed turns into one you mute.
    """
    global alerts_sent

    notable_member = is_notable_member(member)
    above = [t for t in trades
             if t["priority"] != "LOW" or notable_member]
    pushable = [t for t in above if is_equity(t)] if EQUITY_ONLY else above
    skipped_non_equity = len(above) - len(pushable)

    if not pushable or alerts_sent >= MAX_ALERTS_PER_RUN:
        return False

    # A filing is only HIGH if something inside it is.
    notable = is_notable_member(member)
    priority = ("HIGH" if (notable or any(t["priority"] == "HIGH"
                                          for t in pushable)) else "MEDIUM")

    lags = [t["lag"] for t in pushable if t["lag"] != ""]
    lag_txt = (f"{min(lags)} to {max(lags)} days" if lags and min(lags) != max(lags)
               else (f"{lags[0]} days" if lags else "unknown"))

    header = [
        ("PRIORITY", priority + ("  \u2605 WATCHLIST" if notable else "")),
        ("MEMBER", member),
        ("CHAMBER", chamber + (f"  ({party_state})" if party_state else "")),
        ("FILED", dmy(filed_date)),
        ("LAG", lag_txt),
        ("STOCKS", f"{len(pushable)} above threshold"),
        ("SKIPPED", ", ".join(filter(None, [
            f"{skipped_non_equity} bonds/funds" if skipped_non_equity else "",
            f"{len(trades) - len(above)} small" if len(trades) > len(above) else "",
        ]))),
    ]

    lines = []
    for t in sorted(pushable, key=lambda x: -x["low"])[:12]:
        tick = t["ticker"] or "--"
        kind = kind_label(t)
        lines.append((f"{t['action'][:4]} {tick}",
                      f"{amount_label(t['low'], t['high'])}   {dmy(t['trade_date'])}"
                      + (f"  {kind}" if kind else "")))
    if len(pushable) > 12:
        lines.append(("...", f"+{len(pushable) - 12} more in the filing"))

    telegram(box("CONGRESS FILING", header + [("", "")] + lines, link=link,
                 footer="Disclosed under the STOCK Act. These trades already "
                        "happened weeks ago."))
    alerts_sent += 1
    return True


def send_parse_failure(chamber, member, filed_date, link):
    """PDF unreadable. Still tell them a filing landed."""
    global alerts_sent
    if alerts_sent >= MAX_ALERTS_PER_RUN:
        return
    rows = [
        ("PRIORITY", "MEDIUM"),
        ("MEMBER", member),
        ("CHAMBER", chamber),
        ("FILED", dmy(filed_date)),
        ("NOTE", "Trade details could not be read automatically"),
    ]
    telegram(box("CONGRESS FILING - open manually", rows, link=link,
                 footer="Usually a scanned or handwritten PTR."))
    alerts_sent += 1


# ---------------------------------------------------------------
# House
# ---------------------------------------------------------------

def fetch_house_index(year):
    """Returns a list of filing dicts from the Clerk's annual ZIP."""
    url = HOUSE_ZIP.format(year=year)
    try:
        r = session.get(url, timeout=90)
    except requests.RequestException as e:
        log(f"house: zip unreachable :: {e}")
        return None
    if r.status_code != 200:
        log(f"house: zip HTTP {r.status_code}")
        return None

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name))
    except Exception as e:
        log(f"house: zip unreadable :: {e}")
        return None

    out = []
    for m in root.findall(".//Member"):
        def g(tag):
            el = m.find(tag)
            return (el.text or "").strip() if el is not None else ""

        # Only periodic transaction reports. Annual reports are a different beast.
        if g("FilingType") != "P":
            continue

        doc_id = g("DocID")
        if not doc_id:
            continue

        name = " ".join(x for x in [g("First"), g("Last")] if x).strip()
        # Some records repeat the first name inside Last ("Scott Scott
        # Franklin"). Collapse an immediately repeated word.
        name = re.sub(r"\b(\w+) \1\b", r"\1", name, flags=re.IGNORECASE)
        if g("Suffix"):
            name += " " + g("Suffix")

        out.append({
            "doc_id": doc_id,
            "member": name or g("Last"),
            "state": g("StateDst"),
            "filed": g("FilingDate"),
            "year": g("Year") or str(year),
        })
    return out


# House PTR rows WRAP across lines in the extracted text. A single trade
# frequently renders as:
#
#   Berkshire Hathaway Inc. New P 07/28/2026 08/01/2026 $15,001 -
#   Common Stock (BRK.B) [ST] $50,000
#
# So the ticker and the upper amount bound sit on the following line. Parsing
# line by line loses both. Instead we anchor on the one thing that is always
# contiguous, the transaction code followed by two dates, then read outward.

ACTION_DATE_RE = re.compile(
    r"(?<![A-Za-z])(?P<action>S \(partial\)|[PSE])\s+"
    r"(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<ndate>\d{1,2}/\d{1,2}/\d{4})")

# Allow up to 80 characters of wrapped text between the two dollar figures.
AMOUNT_SPAN_RE = re.compile(r"\$([\d,]+)\s*[-\u2013\u2014]\s*[^$]{0,80}?\$([\d,]+)")

TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)")

# The filer's asset-type code, "[ST]" for stock, "[OP]" for options.
TYPE_TAG_RE = re.compile(r"\[([A-Z0-9]{2})\]")

# The column heading the form repeats at the top of every page. When a page
# break falls inside a trade, it lands between the two halves of the amount,
# and its "$200?" was read as the upper bound: 11 logged trades showed
# "$50,001 - $200" and the like. Removed before parsing. Fixed 18 Nov 2026.
PAGE_HEADER_RE = re.compile(
    r"ID\s+Owner\s+Asset\s+Transaction.{0,120}?\$200\?", re.DOTALL)

# An option trade's description line names the side. Used only to say
# "calls" or "puts" instead of "options"; a row saying both stays "options".
CALL_RE = re.compile(r"\bcalls?\b", re.IGNORECASE)
PUT_RE = re.compile(r"\bputs?\b", re.IGNORECASE)


def _clean(s):
    return re.sub(r"\s+", " ", s).strip(" .,-\u2013")


def extract_house_rows(text):
    """Pull trade rows out of raw PTR text. Wrap-tolerant."""
    text = PAGE_HEADER_RE.sub(" ", text)
    rows = []
    for m in ACTION_DATE_RE.finditer(text):
        before = text[max(0, m.start() - 160):m.start()]
        after = text[m.end():m.end() + 200]

        am = AMOUNT_SPAN_RE.search(after)
        if not am or am.start() > 55:
            continue          # no amount close enough, so not a real trade row
        low = int(am.group(1).replace(",", ""))
        high = int(am.group(2).replace(",", ""))

        # The ticker lands in one of three places depending on how the line
        # wrapped: between the dates and the amount, after the amount on the
        # continuation line, or just before the transaction code. Search all
        # three, but never far enough to reach the NEXT row, which is what
        # caused tickers to attach to the wrong company.
        tail = after[:am.end() + 50]
        # Stop at the start of the LINE that holds the next trade's anchor,
        # not at the anchor itself: that line opens with the next trade's
        # name, ticker and type code ("SP Apple Inc. (AAPL) [OP] P ..."),
        # which were otherwise read as this trade's. Searched beyond the
        # 50-character window, so an anchor just past it still counts.
        # Changed 18 Nov 2026.
        nxt = ACTION_DATE_RE.search(after, am.end())
        if nxt:
            line_start = after.rfind("\n", am.end(), nxt.start())
            tail = tail[:line_start if line_start != -1 else nxt.start()]

        tm = TICKER_RE.search(tail)
        if tm:
            ticker = tm.group(1)
        else:
            hits = TICKER_RE.findall(before[-45:])   # adjacent only
            ticker = hits[-1] if hits else ""

        # The type code sits after the amount on the wrapped line, or on the
        # same line just before the transaction code ("(DIS) [ST] S 12/30").
        # Only that one line is searched before, so the previous trade's
        # code can never be borrowed.
        ym = TYPE_TAG_RE.search(tail)
        if ym:
            asset_type = ym.group(1)
        else:
            hits = TYPE_TAG_RE.findall(before[before.rfind("\n") + 1:])
            asset_type = hits[-1] if hits else ""

        option_side = ""
        if asset_type == "OP":
            desc = after[am.end():am.end() + 260]
            nxt_row = ACTION_DATE_RE.search(desc)
            if nxt_row:
                desc = desc[:nxt_row.start()]
            calls, puts = bool(CALL_RE.search(desc)), bool(PUT_RE.search(desc))
            option_side = ("calls" if calls and not puts
                           else "puts" if puts and not calls else "")
        window_after = after[:am.end()]
        if ticker in ("ST", "OP", "PS", "RP", "SP", "JT", "DC", "SR"):
            ticker = ""       # ownership and asset-type tags, not tickers

        # Asset name spans the wrap too.
        tail = before.split("\n")[-1] if "\n" in before else before
        head = window_after[:tm.start()] if tm else ""
        raw_asset = tail + " " + head
        raw_asset = raw_asset.split("$")[0]              # drop wrapped amounts
        raw_asset = re.sub(r"\[[A-Za-z]{1,3}\]", "", raw_asset)
        raw_asset = re.sub(r"\([A-Z][A-Z0-9.\-]{0,6}\)", "", raw_asset)
        raw_asset = re.sub(r"\d{1,2}/\d{1,2}/\d{4}", "", raw_asset)
        asset = _clean(raw_asset)
        asset = re.sub(r"^(SP|JT|DC|ST)\s+", "", asset)   # ownership prefixes

        try:
            td = datetime.strptime(m.group("tdate"), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            td = ""

        rows.append({
            "asset": asset[:70],
            "ticker": ticker,
            "action": action_label(m.group("action")),
            "low": low, "high": high, "trade_date": td,
            "asset_type": asset_type, "option_side": option_side,
        })

    # Until 18 Nov 2026 rows were merged here when they shared an action, a
    # date and the first 18 letters of the asset name, on the belief that one
    # trade could produce two anchors. Checked against 411 real filings: it
    # never does. Every anchor is its own trade (one per "Filing Status"
    # line, in 405 of 411 filings exactly). The merge was only dropping real
    # trades, 263 of 3,768: the same stock bought in two accounts on one
    # day, or two Liberty tracking stocks whose names start alike.
    return rows


def parse_house_pdf(doc_id, year):
    """
    Extract trade rows from a House PTR PDF.

    Returns None if the PDF could not be downloaded (worth another try),
    [] if it downloaded but holds no readable text (a scan), else the rows.
    """
    text = house_pdf_text(doc_id, year)
    if text is None:
        return None
    if not text.strip():
        return []          # scanned document, would need OCR
    return extract_house_rows(text)


def index_years(now):
    """
    Which annual indexes to read. The Clerk files each PTR under the year it
    was filed in, and only the current year was ever read. So a filing made
    in the last days of December that this monitor had not got to before
    midnight on the 31st was never seen, and until the Clerk published the
    new year's file every run failed and the heartbeat called congress dead.
    Through January both years are read.
    """
    return [now.year, now.year - 1] if now.month == 1 else [now.year]


def run_house(seen, health):
    now = datetime.now(timezone.utc)
    index, got_one = [], False
    for year in index_years(now):
        part = fetch_house_index(year)
        if part is None:
            continue
        got_one = True
        index.extend(part)
    if not got_one:
        return
    health["house"] = now.isoformat(timespec="seconds")

    new = [f for f in index if f"H|{f['doc_id']}" not in seen]

    # Mark anything older than the window as seen without alerting, so the
    # backlog is absorbed silently instead of arriving on your phone.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_FILING_AGE_DAYS))
    fresh, stale_count = [], 0
    for f in new:
        try:
            fd = datetime.strptime(f["filed"], "%m/%d/%Y").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            fd = None
        if fd and fd < cutoff:
            seen.add(f"H|{f['doc_id']}")
            stale_count += 1
        else:
            fresh.append(f)
    new = fresh
    log(f"house: {len(index)} PTRs in index, {len(new)} recent, "
        f"{stale_count} older than {MAX_FILING_AGE_DAYS}d absorbed silently")

    # Newest first so a backlog does not bury today's filings. Sorted on the
    # real date: the index writes dates as MM/DD/YYYY, and sorting that text
    # put 12/31/2026 ahead of 01/05/2027.
    def filed_key(f):
        try:
            return datetime.strptime(f.get("filed", ""), "%m/%d/%Y")
        except (ValueError, TypeError):
            return datetime.min
    new.sort(key=filed_key, reverse=True)

    retries = load_json(RETRY_FILE, {})
    if not isinstance(retries, dict):
        retries = {}
    # Forget counts for filings that are no longer waiting.
    retries = {k: v for k, v in retries.items()
               if k in {f["doc_id"] for f in new}}

    for f in new[:MAX_FILINGS_PER_RUN]:
        if alerts_sent >= MAX_ALERTS_PER_RUN:
            # Stop here rather than carry on. The filings left over are not
            # marked seen, so the next run picks them up. Carrying on used to
            # mark them seen with their alert silently skipped.
            log(f"house: alert cap of {MAX_ALERTS_PER_RUN} reached, "
                "the rest wait for the next run")
            break
        if time.monotonic() - RUN_STARTED > RUN_BUDGET_SECONDS:
            log("house: out of time, the rest wait for the next run")
            break

        link = HOUSE_PDF.format(year=f["year"], doc=f["doc_id"])

        try:
            filed = datetime.strptime(f["filed"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            filed = f.get("filed", "")

        trades = parse_house_pdf(f["doc_id"], f["year"])
        time.sleep(0.4)

        if trades is None:
            tries = int(retries.get(f["doc_id"], 0)) + 1
            if tries < MAX_PDF_TRIES:
                retries[f["doc_id"]] = tries
                log(f"house: PDF {f['doc_id']} did not download "
                    f"(try {tries}/{MAX_PDF_TRIES}), will retry next run")
                continue
            log(f"house: PDF {f['doc_id']} failed {tries} downloads, giving up")
            trades = []
        retries.pop(f["doc_id"], None)
        seen.add(f"H|{f['doc_id']}")

        if not trades:
            send_parse_failure("House", f["member"], filed, link)
            continue

        for t in trades:
            t["priority"] = classify(t["low"])
            t["lag"] = log_trade("House", f["member"], t["ticker"], t["asset"],
                                 t["action"], t["low"], t["high"],
                                 t["trade_date"], filed, t["priority"], link,
                                 asset_type=t.get("asset_type", ""))

        send_filing("House", f["member"], f["state"], filed, link, trades)

    if retries:
        save_json(RETRY_FILE, retries)
    else:
        RETRY_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------
# Senate
# ---------------------------------------------------------------

def senate_session():
    """eFD makes you accept a terms page before it returns anything."""
    try:
        r = session.get(SENATE_HOME, headers=SENATE_HEADERS, timeout=30)
        if r.status_code != 200:
            log(f"senate: home HTTP {r.status_code}")
            return None
        token = session.cookies.get("csrftoken")
        if not token:
            m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.text)
            token = m.group(1) if m else None
        if not token:
            log("senate: no csrf token found")
            return None

        r2 = session.post(SENATE_HOME, data={
            "prohibition_agreement": "1",
            "csrfmiddlewaretoken": token,
        }, headers=dict(SENATE_HEADERS, Referer=SENATE_HOME), timeout=30)
        if r2.status_code not in (200, 302):
            log(f"senate: agreement HTTP {r2.status_code}")
            return None
        return session.cookies.get("csrftoken") or token
    except requests.RequestException as e:
        log(f"senate: session failed :: {e}")
        return None


def run_senate(seen, health):
    token = senate_session()
    if not token:
        return

    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%m/%d/%Y")
    r = senate_post({
        "start": "0",
        "length": "100",
        "report_types": "[11]",           # periodic transaction reports
        "filer_types": "[]",
        "submitted_start_date": start,
        "submitted_end_date": "",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
    })
    if r is None:
        return

    try:
        rows = r.json().get("data", [])
    except ValueError:
        log("senate: search returned non-JSON")
        return

    health["senate"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"senate: {len(rows)} PTRs in window")

    count = 0
    for row in rows:
        if count >= MAX_FILINGS_PER_RUN:
            break
        # Columns: first, last, office, report link (HTML anchor), date
        try:
            first, last, office, anchor, filed_raw = row[:5]
        except (ValueError, TypeError):
            continue

        m = re.search(r'href="([^"]+)"', str(anchor))
        href = m.group(1) if m else ""
        if not href:
            continue
        link = "https://efdsearch.senate.gov" + href
        key = "S|" + href
        if key in seen:
            continue
        seen.add(key)
        count += 1

        member = re.sub(r"<[^>]+>", "", f"{first} {last}").strip()
        try:
            filed = datetime.strptime(filed_raw.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            filed = str(filed_raw)

        # eFD renders trades as an HTML table on the report page.
        try:
            pr = session.get(link, headers=dict(SENATE_HEADERS,
                                                Referer=SENATE_HOME), timeout=30)
            html = pr.text if pr.status_code == 200 else ""
        except requests.RequestException:
            html = ""

        trades = parse_senate_html(html)
        time.sleep(0.4)

        if not trades:
            send_parse_failure("Senate", member, filed, link)
            continue

        office_txt = re.sub(r"<[^>]+>", "", str(office))[:24]
        for t in trades:
            t["priority"] = classify(t["low"])
            t["lag"] = log_trade("Senate", member, t["ticker"], t["asset"],
                                 t["action"], t["low"], t["high"],
                                 t["trade_date"], filed, t["priority"], link)

        send_filing("Senate", member, office_txt, filed, link, trades)


SEN_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
SEN_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)


def parse_senate_html(html):
    if not html:
        return []
    out = []
    for row_html in SEN_ROW_RE.findall(html):
        cells = [re.sub(r"<[^>]+>", " ", c) for c in SEN_CELL_RE.findall(row_html)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if len(cells) < 6:
            continue
        joined = " ".join(cells)
        if "$" not in joined:
            continue

        low, high = parse_amount(joined)
        if not low:
            continue

        ticker = ""
        for c in cells:
            tm2 = re.fullmatch(r"[A-Z]{1,5}", c.strip())
            if tm2:
                ticker = c.strip()
                break

        action = ""
        for c in cells:
            cl = c.strip().lower()
            if cl in ("purchase", "sale", "sale (partial)", "sale (full)", "exchange"):
                action = ("BUY" if cl == "purchase"
                          else "EXCHANGE" if cl == "exchange" else "SELL")
                break
        if not action:
            continue

        trade_date = ""
        dm = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", joined)
        if dm:
            try:
                trade_date = datetime.strptime(dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                pass

        asset = ""
        for c in cells:
            if len(c) > 8 and "$" not in c and "/" not in c:
                asset = c
                break

        out.append({"asset": asset, "ticker": ticker, "action": action,
                    "low": low, "high": high, "trade_date": trade_date})
    return out


# ---------------------------------------------------------------
# Connectivity test
# ---------------------------------------------------------------

def connectivity_test():
    """Verbose enough to tune the parser without another round trip."""
    print("=== CONGRESS CONNECTIVITY TEST ===")
    year = datetime.now(timezone.utc).year

    print(f"\n[House] annual index for {year}")
    idx = fetch_house_index(year)
    if idx is None:
        print("  FAILED. The ZIP URL or format may have changed.")
    else:
        print(f"  OK. {len(idx)} periodic transaction reports in the index.")
        newest = sorted(idx, key=lambda f: f.get("filed", ""))[-5:][::-1]

        for f in newest:
            print(f"\n  --- {f['member']} ({f['state']}) filed {f['filed']} "
                  f"doc {f['doc_id']} ---")
            raw = house_pdf_text(f["doc_id"], f["year"])
            if not raw:
                print("      no extractable text (scanned document)")
                continue

            money_lines = [ln.strip() for ln in raw.splitlines() if "$" in ln]
            rows = parse_house_pdf(f["doc_id"], f["year"]) or []
            print(f"      lines containing '$': {len(money_lines)}   "
                  f"rows parsed: {len(rows)}")

            for r in rows[:4]:
                print(f"        PARSED  {r['ticker'] or '(no ticker)':12} "
                      f"{r['action']:6} {amount_label(r['low'], r['high']):22} "
                      f"{r['trade_date']}  {r['asset'][:38]}")

            # Show money lines the regex did NOT claim, that is where bugs hide.
            claimed = {r["trade_date"] for r in rows}
            unmatched = [ln for ln in money_lines
                         if not any(d[8:10] + "/" in ln or d in ln for d in claimed)
                         and "$" in ln][:4]
            for ln in unmatched:
                print(f"        UNMATCHED  {ln[:110]}")

    print(f"\n[Senate] enabled: {SENATE_ENABLED}")
    print("[Senate] eFD session")
    tok = senate_session()
    if not tok:
        print("  FAILED to establish session.")
    else:
        print("  OK, session established.")
        rows = senate_probe()
        print(f"  Search returned {rows} rows in the last 30 days.")

    print("\n=== END ===")


def house_pdf_text(doc_id, year):
    """
    Raw text of a House PTR.

    Returns None when the PDF could not be downloaded, so the caller can try
    again later. Returns "" when it downloaded but nothing could be read
    from it, which is what a scanned or handwritten filing looks like.
    """
    try:
        import pdfplumber
    except ImportError:
        return ""
    url = HOUSE_PDF.format(year=year, doc=doc_id)
    try:
        r = session.get(url, timeout=60)
    except requests.RequestException as e:
        log(f"house: PDF {doc_id} unreachable :: {type(e).__name__}")
        return None
    if r.status_code != 200:
        log(f"house: PDF {doc_id} HTTP {r.status_code}")
        return None
    text = ""
    try:
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages[:MAX_PDF_PAGES]:
                text += (page.extract_text() or "") + "\n"
    except Exception:
        return ""
    return text


def senate_post(data, tries=3):
    """eFD returns 503 under load. Retry before treating it as broken."""
    token = session.cookies.get("csrftoken")
    payload = dict(data)
    payload["csrfmiddlewaretoken"] = token
    wait = 2.0
    for attempt in range(1, tries + 1):
        try:
            r = session.post(SENATE_SEARCH, data=payload, headers=dict(
                SENATE_HEADERS,
                Referer=SENATE_HOME,
                Accept="application/json, text/javascript, */*; q=0.01",
                **{"X-CSRFToken": token or "",
                   "X-Requested-With": "XMLHttpRequest"},
            ), timeout=45)
        except requests.RequestException as e:
            if attempt == tries:
                log(f"senate: post failed :: {e}")
                return None
            time.sleep(wait); wait *= 2; continue

        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            if attempt == tries:
                log(f"senate: HTTP {r.status_code} after {tries} tries")
                return None
            time.sleep(wait); wait *= 2; continue
        log(f"senate: HTTP {r.status_code}")
        return None
    return None


def senate_probe():
    """Row count only, for the diagnostic."""
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%m/%d/%Y")
    r = senate_post({
        "start": "0", "length": "100", "report_types": "[11]",
        "filer_types": "[]", "submitted_start_date": start,
        "submitted_end_date": "", "candidate_state": "", "senator_state": "",
        "office_id": "", "first_name": "", "last_name": "",
    })
    if r is None:
        return "unavailable (503 or timeout, often transient)"
    try:
        return len(r.json().get("data", []))
    except ValueError:
        return "non-JSON response"


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------

def main():
    STATE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    ensure_trades_csv()

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return

    flush_outbox()          # anything an earlier run could not deliver
    seen = Ledger(SEEN_FILE, SEEN_MAX)
    health = load_json(HEALTH_FILE, {})

    try:
        run_house(seen, health)
    except Exception as e:
        log(f"house run failed :: {e}")

    if SENATE_ENABLED:
        try:
            run_senate(seen, health)
        except Exception as e:
            log(f"senate run failed :: {e}")
    else:
        log("senate disabled (eFD blocks this host, set SENATE_ENABLED=1 to retry)")

    seen.save()
    save_json(HEALTH_FILE, health)
    log(f"congress done, alerts sent {alerts_sent}")


if __name__ == "__main__":
    main()
