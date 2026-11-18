"""The two Monday digests: earnings.py and events.py."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import earnings
import events
import monitor
from conftest import body_of, flat

ROOT = Path(__file__).resolve().parent.parent
SGT = timezone(timedelta(hours=8))


# =============================================================== earnings

def test_coming_week():
    assert earnings.coming_week(date(2026, 11, 22)) == (date(2026, 11, 23), date(2026, 11, 27))  # Sunday
    assert earnings.coming_week(date(2026, 11, 23)) == (date(2026, 11, 23), date(2026, 11, 27))  # Monday
    assert earnings.coming_week(date(2026, 11, 25)) == (date(2026, 11, 30), date(2026, 12, 4))   # Wednesday


def test_share_class_symbols_match_however_they_are_written():
    assert earnings.norm_symbol("BRK.B") == earnings.norm_symbol("brk/b") == earnings.norm_symbol("BRK-B")
    assert earnings._WATCH[earnings.norm_symbol("BRK/B")] == "BRK.B"


def test_every_watchlist_ticker_is_still_a_listed_company():
    """Catches a rename like SQ becoming XYZ, which went unnoticed for a year."""
    rows = json.loads((ROOT / "state" / "sec_companies.json").read_text())
    listed = {earnings.norm_symbol(t) for _, t, _ in rows}
    gone = sorted(s for s in earnings.WATCHLIST if earnings.norm_symbol(s) not in listed)
    assert gone == [], f"no longer listed under these tickers: {gone}"
    assert "XYZ" in earnings.WATCHLIST and "SQ" not in earnings.WATCHLIST


class Clock:
    """Lets a test say what time it is for earnings.py."""

    def __init__(self, monkeypatch):
        self.now = datetime(2026, 11, 22, 23, 5, tzinfo=timezone.utc)   # Sun 23:05 UTC
        clock = self

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)
        monkeypatch.setattr(earnings, "datetime", FixedDatetime)

    def set(self, *args):
        self.now = datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def cal(tg, monkeypatch):
    """Sunday 23:05 UTC, which is Monday 07:05 in Singapore: digest time."""
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "earnings")
    world = {"clock": Clock(monkeypatch), "rows": lambda day: [{"date": day.strftime("%Y-%m-%d"), "symbol": "NVDA",
                                   "name": "NVIDIA", "when": ""}] if day.weekday() == 2 else []}
    monkeypatch.setattr(earnings, "fetch_nasdaq", lambda day, debug=False: world["rows"](day))
    monkeypatch.setattr(earnings, "fetch_finnhub", lambda s, e: None)
    return world


def test_earnings_digest_is_sent_once_per_week(cal, tg):
    earnings.run_digest()                    # Kick.gs, Monday 07:05 Singapore
    earnings.run_digest()                    # the cron that fired at the same time
    cal["clock"].set(2026, 11, 23, 1, 0)     # backup cron, Monday 01:00 UTC
    earnings.run_digest()
    cal["clock"].set(2026, 11, 23, 4, 0)     # backup cron, Monday 04:00 UTC
    earnings.run_digest()
    assert len(tg.sent) == 1
    msg = body_of(tg.texts[0])
    assert msg.startswith("WEEK AHEAD - EARNINGS\n") and "NVDA" in msg
    assert "WEEK        23 Nov to 27 Nov" in msg
    assert f"WATCHING    {len(earnings.WATCHLIST)} large caps" in msg
    st = json.loads(earnings.STATE_FILE.read_text())
    assert st["sent_week"] == "2026-11-23"
    assert st["fail_count"] == 0 and "last_ok" in st


def test_next_weeks_digest_is_sent_the_following_sunday(cal, tg):
    earnings.run_digest()
    cal["clock"].set(2026, 11, 29, 23, 5)
    earnings.run_digest()
    assert len(tg.sent) == 2 and "30 Nov to 04 Dec" in tg.texts[1]


def test_a_copy_sent_by_hand_midweek_does_not_cancel_the_real_one(cal, tg):
    cal["clock"].set(2026, 11, 18, 5, 0)     # Wednesday, by hand: shows next week
    earnings.run_digest()
    assert "23 Nov to 27 Nov" in tg.texts[0]
    cal["clock"].set(2026, 11, 22, 23, 5)    # the scheduled Sunday run still sends
    earnings.run_digest()
    earnings.run_digest()
    assert len(tg.sent) == 2


def test_force_sends_again(cal, tg):
    earnings.run_digest()
    earnings.run_digest(force=True)
    assert len(tg.sent) == 2


def test_quiet_week_says_so_and_counts_as_sent(cal, tg):
    cal["rows"] = lambda day: []
    earnings.run_digest()
    earnings.run_digest()
    assert len(tg.sent) == 1
    assert "None of the" in body_of(tg.texts[0])


def test_source_failure_is_announced_once_then_retried_quietly_until_it_works(cal, tg):
    good = cal["rows"]
    cal["rows"] = lambda day: None
    earnings.run_digest()
    earnings.run_digest()
    assert len(tg.sent) == 1
    assert body_of(tg.texts[0]).startswith("WEEK AHEAD - EARNINGS UNAVAILABLE")
    st = json.loads(earnings.STATE_FILE.read_text())
    assert st["fail_count"] == 2 and "sent_week" not in st
    cal["rows"] = good                        # a backup run gets through
    earnings.run_digest()
    assert len(tg.sent) == 2 and body_of(tg.texts[1]).startswith("WEEK AHEAD - EARNINGS\n")
    earnings.run_digest()
    assert len(tg.sent) == 2


def test_days_that_could_not_be_read_are_admitted(cal, tg):
    base = cal["rows"]
    cal["rows"] = lambda day: None if day.weekday() in (0, 1) else base(day)
    earnings.run_digest()
    assert "2 of 5 days could not be read" in flat(body_of(tg.texts[0]))


def test_finnhub_key_is_sent_in_a_header_not_the_url(tg, monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, headers=None):
        seen.update(url=url, headers=headers)
        raise earnings.requests.ConnectionError("down")
    monkeypatch.setattr(earnings, "FINNHUB_KEY", "SECRETKEY")
    monkeypatch.setattr(earnings.session, "get", fake_get)
    assert earnings.fetch_finnhub(date(2026, 11, 23), date(2026, 11, 27)) is None
    assert "SECRETKEY" not in seen["url"] and seen["headers"]["X-Finnhub-Token"] == "SECRETKEY"


# ================================================================= events

def ev(title, when, impact="High", country="USD"):
    return {"title": title, "country": country, "date": when, "impact": impact,
            "forecast": "", "previous": ""}


def test_event_at_midnight_singapore_keeps_its_time():
    """11:00 New York in winter is 00:00 Singapore. It used to print as 'all day'."""
    when, all_day = events.to_sgt(ev("FOMC Member Daly Speaks", "2026-11-17T11:00:00-05:00"))
    assert all_day is False and when.strftime("%a %d %b %H:%M") == "Wed 18 Nov 00:00"
    when, all_day = events.to_sgt(ev("Bank Holiday", "2026-11-26T00:00:00-05:00"))
    assert all_day is True and when.strftime("%a %d %b") == "Thu 26 Nov"
    when, all_day = events.to_sgt(ev("CPI m/m", "2026-11-12T08:30:00-05:00"))
    assert all_day is False and when.strftime("%a %H:%M") == "Thu 21:30"
    assert events.to_sgt(ev("x", "garbage")) == (None, True)


def test_event_filter():
    assert events.wanted(ev("CPI m/m", "x", impact="Medium"))             # always-list keyword
    assert events.wanted(ev("Obscure Survey", "x", impact="High"))
    assert not events.wanted(ev("Obscure Survey", "x", impact="Low"))
    assert not events.wanted(ev("CPI m/m", "x", impact="High", country="EUR"))
    assert events.clean_title("Core PCE Price Index  (MoM)") == "Core PCE Price Index"


def test_digest_week_is_the_singapore_monday():
    assert events.digest_week(datetime(2026, 11, 23, 7, 0, tzinfo=SGT)) == "2026-11-23"
    assert events.digest_week(datetime(2026, 11, 25, 15, 0, tzinfo=SGT)) == "2026-11-23"
    assert events.digest_week(datetime(2026, 11, 29, 23, 0, tzinfo=SGT)) == "2026-11-23"


@pytest.fixture
def ff(tg, monkeypatch):
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "events")
    world = {"data": [ev("CPI m/m", "2026-11-12T08:30:00-05:00"),
                      ev("FOMC Member Daly Speaks", "2026-11-17T11:00:00-05:00"),
                      ev("German Thing", "2026-11-12T03:00:00-05:00", country="EUR")]}
    monkeypatch.setattr(events, "fetch_week", lambda url: world["data"])

    # Pinned to Monday 16 Nov 2026, 07:05 Singapore: the digest week is the
    # one the sample events above belong to, whatever day the suite runs.
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 11, 15, 23, 5, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(events, "datetime", FixedDatetime)
    return world


def test_events_from_the_wrong_week_count_as_a_failed_fetch(tg, monkeypatch):
    """This week's feed fails; next week's feed holds the FOLLOWING week."""
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "events")
    feeds = {events.THIS_WEEK: None,
             events.NEXT_WEEK: [ev("CPI m/m", "2026-11-26T08:30:00-05:00")]}
    monkeypatch.setattr(events, "fetch_week", lambda url: feeds[url])

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 11, 15, 23, 5, tzinfo=timezone.utc)   # Mon 16 Nov SGT
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(events, "datetime", FixedDatetime)
    events.run_digest()
    st = json.loads(events.STATE_FILE.read_text())
    assert "sent_week" not in st and st["failed_week"] == "2026-11-16"
    assert body_of(tg.texts[0]).startswith("US EVENTS - NO DATA")
    feeds[events.THIS_WEEK] = [ev("FOMC Member Daly Speaks", "2026-11-17T11:00:00-05:00")]
    events.run_digest()                                        # the backup run
    assert json.loads(events.STATE_FILE.read_text())["sent_week"] == "2026-11-16"
    assert "Daly" in tg.texts[1]


def test_a_forced_run_on_sunday_afternoon_sends_the_coming_week(tg, monkeypatch):
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "events")
    feeds = {events.THIS_WEEK: [ev("CPI m/m", "2026-11-24T08:30:00-05:00")],   # rolled over
             events.NEXT_WEEK: None}
    monkeypatch.setattr(events, "fetch_week", lambda url: feeds[url])

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)   # Sun 20:00 SGT
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(events, "datetime", FixedDatetime)
    events.run_digest()                                          # not forced: old week, no data
    assert body_of(tg.texts[0]).startswith("US EVENTS - NO DATA")
    events.run_digest(force=True)
    assert "CPI" in tg.texts[1]
    assert json.loads(events.STATE_FILE.read_text())["sent_week"] == "2026-11-23"


def test_covers_week():
    wk = "2026-11-16"
    assert events.covers_week([ev("x", "2026-11-15T19:00:00-05:00")], wk)   # the feed's Sunday
    assert events.covers_week([ev("x", "2026-11-21T10:00:00-05:00")], wk)   # its Saturday
    assert not events.covers_week([ev("x", "2026-11-23T08:30:00-05:00")], wk)
    assert not events.covers_week([ev("x", "2026-11-13T08:30:00-05:00")], wk)
    assert events.covers_week([], wk)


def test_events_digest_is_sent_once_per_week_in_singapore_time(ff, tg):
    events.run_digest()
    events.run_digest()
    assert len(tg.sent) == 1
    msg = body_of(tg.texts[0])
    assert msg.startswith("WEEK AHEAD - US EVENTS\n")
    assert "Thu 12 Nov  21:30  -  CPI m/m" in msg
    assert "Wed 18 Nov  00:00  -  FOMC Member Daly Speaks" in msg
    assert "German" not in msg and "All times Singapore." in msg
    events.run_digest(force=True)
    assert len(tg.sent) == 2


def test_events_feed_failure_is_announced_once_then_recovers(ff, tg):
    good = ff["data"]
    ff["data"] = None
    events.run_digest()
    events.run_digest()
    assert len(tg.sent) == 1 and body_of(tg.texts[0]).startswith("US EVENTS - NO DATA")
    assert json.loads(events.STATE_FILE.read_text())["fail_count"] == 2
    ff["data"] = good
    events.run_digest()
    assert len(tg.sent) == 2 and "CPI" in tg.texts[1]


def test_quiet_events_week_still_counts(ff, tg):
    ff["data"] = [ev("German Thing", "2026-11-17T03:00:00-05:00", country="EUR")]
    events.run_digest()
    events.run_digest()
    assert len(tg.sent) == 1 and "No high impact US events" in tg.texts[0]
