#!/usr/bin/env python3
"""
events.py - US economic calendar, weekly digest

Sends one message every Monday morning listing the US economic events for the
coming week: CPI, PPI, PCE, payrolls, FOMC, jobless claims and the rest.

Source is the JSON feed behind the Forex Factory calendar widget rather than
the calendar page itself, which sits behind Cloudflare and would need a
browser to read.

You will not beat anyone to a data release. The value is knowing what is
scheduled so you are not holding a position into a print you forgot about.

Run modes:
    python events.py           send the weekly digest, once per week
    python events.py --force   send it even if it already went out
    python events.py test      connectivity check, sends nothing
"""

import os
import re
import sys
from datetime import date, datetime, timezone, timedelta

import requests

from monitor import (card, log, telegram, load_json, stamp, flush_outbox,
                     digest_already_sent, STATE_DIR)

USER_AGENT = os.environ.get("SEC_USER_AGENT", "FilingsMonitor")

# Forex Factory's widget feed. Same data as the calendar page, without the
# Cloudflare challenge.
THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

COUNTRY = "USD"          # US only, as requested

# "High" alone is roughly 8-12 events a week. Add "Medium" for around 25.
IMPACTS = {"High"}

# Always include these regardless of the impact rating the feed assigns.
ALWAYS = [
    "fomc", "federal funds", "interest rate", "cpi", "core cpi", "ppi",
    "core pce", "non-farm", "nonfarm", "unemployment rate", "gdp",
    "retail sales", "ism ", "powell", "fed chair",
    "jobless claims", "unemployment claims", "employment change",
    "consumer sentiment", "durable goods", "treasury", "beige book",
]

SGT = timezone(timedelta(hours=8))
STATE_FILE = STATE_DIR / "events_state.json"

session = requests.Session()


def fetch_week(url):
    try:
        r = session.get(url, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
        })
    except requests.RequestException as e:
        log(f"events: unreachable :: {e}")
        return None
    if r.status_code != 200:
        log(f"events: HTTP {r.status_code}")
        return None
    try:
        data = r.json()
    except ValueError:
        log("events: non-JSON response")
        return None
    return data if isinstance(data, list) else None


def wanted(ev):
    if (ev.get("country") or "").upper() != COUNTRY:
        return False
    title = (ev.get("title") or "").lower()
    if any(k in title for k in ALWAYS):
        return True
    return (ev.get("impact") or "") in IMPACTS


def to_sgt(ev):
    """
    Feed dates are ISO with an offset. Returns (datetime_sgt, all_day).

    The feed marks an all-day or tentative item with midnight in ITS OWN
    time zone (New York). This used to test for midnight after converting to
    Singapore time, which is a different moment: a real event at 11:00 New
    York in winter (12:00 in summer) is 00:00 Singapore, so it was printed
    as "all day" with its time thrown away, while true all-day items showed
    a made-up 13:00. Fixed 18 Nov 2026. All-day items keep the feed's date.
    """
    raw = ev.get("date") or ""
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, True
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    if d.hour == 0 and d.minute == 0:
        return d.replace(tzinfo=SGT), True
    return d.astimezone(SGT), False


def clean_title(t):
    t = re.sub(r"\s+", " ", str(t)).strip()
    t = re.sub(r"\s*\(.*?\)\s*$", "", t)        # trailing (MoM), (YoY)
    return t


def build(events):
    """One line per event: day date time - event name."""
    rows = []
    for ev in sorted(events, key=lambda x: to_sgt(x)[0] or datetime.max
                     .replace(tzinfo=SGT)):
        when, all_day = to_sgt(ev)
        if when is None:
            continue
        label = when.strftime("%a %d %b").replace(" 0", " ")
        time_ = "all day" if all_day else when.strftime("%H:%M")
        rows.append((label, f"{time_}  {clean_title(ev.get('title'))[:38]}"))
    return rows


def digest_week(now_sgt):
    """The Monday (Singapore date) of the week a digest sent now belongs to."""
    return (now_sgt - timedelta(days=now_sgt.weekday())).date().isoformat()


def covers_week(data, week):
    """
    True if the feed holds the week whose Monday is `week`. The feed's week
    runs Sunday to Saturday in its own time zone, so the right file has
    events dated from the Sunday before that Monday to the Saturday after.
    An empty list proves nothing either way and is accepted.
    """
    monday = date.fromisoformat(week)
    first, last = monday - timedelta(days=1), monday + timedelta(days=5)
    days = []
    for ev in data:
        try:
            days.append(datetime.fromisoformat(
                str(ev.get("date") or "").replace("Z", "+00:00")).date())
        except ValueError:
            continue
    if not days:
        return True
    return any(first <= d <= last for d in days)


def run_digest(force=False):
    flush_outbox()          # anything an earlier run could not deliver
    week = digest_week(datetime.now(SGT))

    # Send once per week. This job is now started by Kick.gs at 07:00
    # Singapore as well as by cron, with later cron times as a backup, so it
    # can run several times on a Monday.
    state = load_json(STATE_FILE, {})
    if not force and digest_already_sent(state, week):
        log(f"events: the digest for the week of {week} already went out, "
            "nothing to do (use --force to send it again)")
        stamp("events_state.json")
        return

    # Sent Monday morning SGT, so "this week" is the week ahead. A feed only
    # counts if it holds the digest's week. Until 18 Nov 2026 a failed
    # this-week fetch fell back to next week's feed, which on a Monday is the
    # FOLLOWING week: it went out as "this week" and was recorded as sent, so
    # the backup runs could not put it right. Now that is a failed fetch,
    # and the backup runs try again.
    feeds = [(url, fetch_week(url)) for url in (THIS_WEEK, NEXT_WEEK)]
    # A forced run on a Sunday afternoon in Singapore (after the feed rolls
    # to the new week, Sunday 00:00 New York) is asking for the coming week,
    # not the one ending. Scheduled runs are Monday only and never get here.
    weeks = [week]
    if force:
        weeks.append((date.fromisoformat(week) + timedelta(days=7)).isoformat())
    data = None
    for wanted_week in weeks:
        for url, got in feeds:
            if got is not None and covers_week(got, wanted_week):
                data, week = got, wanted_week
                break
        if data is not None:
            break
    if data is None:
        for url, got in feeds:
            if got is not None:
                log(f"events: {url.rsplit('/', 1)[-1]} does not cover the "
                    f"week of {week}, not using it")

    if data is None:
        stamp("events_state.json", error="calendar feed unreachable",
              failed_week=week)
        # Say so once. The backup runs try again quietly.
        if force or state.get("failed_week") != week:
            telegram(card("US EVENTS - NO DATA", [
                ("Problem", "calendar feed returned nothing"),
                ("Likely", "feed moved or blocked"),
                ("Next", "retrying over the next hours"),
            ], tag="MEDIUM", sub="This week's events digest is delayed",
                do="If nothing follows, open the Actions tab and run "
                   "Diagnostics."), silent=False)
        return

    sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    events = [e for e in data if wanted(e)]
    rows = build(events)

    if not rows:
        # A quiet week is still a healthy run. Previously this returned
        # before saving state, so the state file aged out and a quiet week
        # looked identical to a broken module.
        log("events: no US events matched this week")
        telegram(card("WEEK AHEAD - US EVENTS", [
            ("Status", "No high impact US events scheduled this week"),
        ], sub="All times Singapore"), silent=False)
        stamp("events_state.json", count=0, sent_week=week, sent_at=sent_at)
        return

    # One entry per event: day, date, time, event, in that order, as asked.
    # A long event name continues under its own column.
    telegram(card("WEEK AHEAD - US EVENTS", rows, sub="All times Singapore"),
             silent=False)

    stamp("events_state.json", count=len(events), sent_week=week,
          sent_at=sent_at)
    log(f"events digest sent, {len(events)} events")


def connectivity_test():
    print("=== US EVENTS CONNECTIVITY TEST ===")
    for name, url in (("this week", THIS_WEEK), ("next week", NEXT_WEEK)):
        data = fetch_week(url)
        if data is None:
            print(f"  {name:10} FAILED")
            continue
        us = [e for e in data if (e.get("country") or "").upper() == COUNTRY]
        high = [e for e in us if wanted(e)]
        print(f"  {name:10} OK. {len(data)} total, {len(us)} US, "
              f"{len(high)} matching filter")
        for e in high[:6]:
            when, all_day = to_sgt(e)
            t = "all day" if all_day else (when.strftime("%a %H:%M")
                                           if when else "?")
            print(f"      {t:12} {clean_title(e.get('title'))[:40]:42} "
                  f"impact={e.get('impact')}")
    print("\n=== END ===")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return
    run_digest(force="--force" in sys.argv[1:])


if __name__ == "__main__":
    main()
