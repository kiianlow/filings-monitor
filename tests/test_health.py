"""Heartbeat, state files and housekeeping."""

import csv
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import monitor
from conftest import FakeResponse, body_of, flat, rows_of

UTC = timezone.utc


def iso(hours_ago=0.0):
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def healthy_state(state_dir, **overrides):
    """Write state files for a fleet where everything ran a moment ago."""
    old = iso(24 * 30)
    labels = list(monitor.FORMS) + ["congress-house"] + [c[0] for c in monitor.MODULE_CHECKS]
    monitor.save_json(monitor.FIRST_SEEN_FILE, {k: old for k in labels})
    monitor.save_json(monitor.HEALTH_FILE, {f: iso(0.1) for f in monitor.FORMS})
    monitor.save_json(state_dir / "congress_health.json", {"house": iso(1)})
    for _, fname, _ in monitor.MODULE_CHECKS:
        key = "at" if fname == "fear_greed.json" else "last_ok"
        monitor.save_json(state_dir / fname, {key: iso(0.1)})
    for fname, content in overrides.items():
        monitor.save_json(state_dir / fname, content)


@pytest.fixture
def fleet(tg):
    healthy_state(monitor.STATE_DIR)
    return tg


# -------------------------------------------------------------- heartbeat

def test_heartbeat_is_silent_when_everything_is_healthy(fleet):
    assert monitor.heartbeat() is True
    assert fleet.sent == []


def test_sec_feed_silent_for_13_hours_is_reported_not_after_four_days(fleet):
    health = {f: iso(0.1) for f in monitor.FORMS}
    health["4"] = iso(13)
    monitor.save_json(monitor.HEALTH_FILE, health)
    assert monitor.heartbeat() is True                  # warning delivered
    msg = body_of(fleet.texts[0])
    assert msg.startswith("<b>FILINGS NEED ATTENTION</b>")
    assert "last ok 13h ago (limit 12h)" in flat(msg)
    assert "Still ok" in msg and "144" in msg
    # readable names (18 Nov 2026): not a bare "4", not internal labels
    assert "Form 4     last ok 13h ago" in rows_of(msg)
    assert "Form 144" in msg and "Fear &amp; Greed" in flat(msg)
    assert "congress-house" not in msg and "fear-greed" not in msg


def test_sec_feed_at_11_hours_is_still_fine(fleet):
    monitor.save_json(monitor.HEALTH_FILE, {f: iso(11) for f in monitor.FORMS})
    assert monitor.heartbeat() is True and fleet.sent == []


def test_old_8k_and_nt_keys_left_in_health_json_are_ignored(fleet):
    health = {f: iso(0.1) for f in monitor.FORMS}
    health.update({"8-K": iso(24 * 70), "NT 10-K": iso(24 * 70)})
    monitor.save_json(monitor.HEALTH_FILE, health)
    assert monitor.heartbeat() is True and fleet.sent == []


def test_congress_limit_is_48_hours(fleet):
    monitor.save_json(monitor.STATE_DIR / "congress_health.json", {"house": iso(47)})
    monitor.heartbeat()
    assert fleet.sent == []
    monitor.save_json(monitor.STATE_DIR / "congress_health.json", {"house": iso(49)})
    monitor.heartbeat()
    assert "last ok 49h ago (limit 48h)" in flat(body_of(fleet.texts[0]))


def test_failing_module_shows_its_error_text(fleet):
    monitor.save_json(monitor.STATE_DIR / "trump_state.json", {
        "last_ok": iso(50), "last_error": "discovery failed, only the seed list",
        "fail_count": 2})
    monitor.heartbeat()
    msg = body_of(fleet.texts[0])
    assert any(r.startswith("Trump      ") for r in rows_of(msg))   # its own row
    msg = flat(msg)
    assert "limit 48h" in msg and "discovery failed" in msg


def test_repeated_module_errors_are_reported(fleet):
    monitor.save_json(monitor.ERROR_FILE, {
        "fear_greed": {"count": 3, "last": "HTTP 418", "at": iso()},
        "prune": {"count": 1, "last": "one blip", "at": iso()}})
    monitor.heartbeat()
    msg = body_of(fleet.texts[0])
    msg = flat(msg)
    assert "Fear &amp; Greed x3 HTTP 418" in msg and "one blip" not in msg
    # a problem is not also listed as fine (fixed 18 Nov 2026)
    still_ok = msg.split("Still ok", 1)[1].split("</code>")[0]
    assert "Fear" not in still_ok and "Form 4" in still_ok


def test_new_feed_gets_a_grace_period(fleet):
    first = json.loads(monitor.FIRST_SEEN_FILE.read_text())
    first["4"] = iso(1)
    monitor.save_json(monitor.FIRST_SEEN_FILE, first)
    health = {f: iso(0.1) for f in monitor.FORMS}
    del health["4"]
    monitor.save_json(monitor.HEALTH_FILE, health)
    assert monitor.heartbeat() is True and fleet.sent == []


def test_alerts_stuck_in_an_outbox_for_over_an_hour_are_reported(fleet):
    monitor.save_json(monitor.STATE_DIR / "outbox_halts.json",
                      [{"text": "a", "at": iso(3)}, {"text": "b", "at": iso(0.2)}])
    monitor.save_json(monitor.STATE_DIR / "outbox_congress.json",
                      [{"text": "c", "at": iso(0.5)}])              # under an hour
    monitor.heartbeat()
    msg = body_of(fleet.texts[0])
    msg = flat(msg)
    assert "halts send" in msg and "2 alert(s) undelivered, oldest 3h" in msg
    assert "congress send" not in msg.lower()


def test_revoked_token_fails_the_run_because_no_message_can_say_so(fleet):
    fleet.get_script = [FakeResponse(401, "Unauthorized")]
    assert monitor.heartbeat() is False
    assert fleet.sent == []


def test_warning_that_cannot_be_delivered_fails_the_run_and_is_not_queued(fleet):
    monitor.save_json(monitor.HEALTH_FILE, {f: iso(40) for f in monitor.FORMS})
    fleet.script = [FakeResponse(500)] * 3
    assert monitor.heartbeat() is False
    assert list(monitor.STATE_DIR.glob("outbox_*.json")) == []


def test_main_exits_nonzero_when_the_heartbeat_fails(fleet, monkeypatch):
    fleet.get_script = [FakeResponse(403, "Forbidden: bot was kicked")]
    monkeypatch.setattr(monitor.sys, "argv", ["monitor.py", "heartbeat"])
    with pytest.raises(SystemExit) as e:
        monitor.main()
    assert e.value.code == 1


# ------------------------------------------------------- market calendar

def test_holidays_match_the_exchange_list_that_used_to_be_typed_in():
    typed = {"2026-11-26", "2026-12-25",
             "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
             "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
    got = {d.isoformat() for d in monitor.nyse_holidays(2027)}
    got |= {d.isoformat() for d in monitor.nyse_holidays(2026) if d.isoformat() >= "2026-11-26"}
    assert got == typed


def test_holidays_for_other_years():
    # 2022: New Year's Day fell on a Saturday and was NOT moved to Friday.
    y22 = {d.isoformat() for d in monitor.nyse_holidays(2022)}
    assert "2021-12-31" not in y22 and "2022-01-01" not in y22 and "2022-01-17" in y22
    assert "2022-04-15" in y22 and "2022-06-20" in y22 and "2022-12-26" in y22
    y24 = {d.isoformat() for d in monitor.nyse_holidays(2024)}
    assert y24 == {"2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
                   "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25"}
    assert date(2030, 4, 19) in monitor.nyse_holidays(2030)          # Good Friday 2030


@pytest.mark.parametrize("when,is_open", [
    ("2026-11-18T15:00:00+00:00", True),     # Wed 10:00 New York (EST)
    ("2026-11-18T14:00:00+00:00", False),    # Wed 09:00, before the bell
    ("2026-11-18T22:00:00+00:00", False),    # Wed 17:00, after the close
    ("2026-07-15T14:00:00+00:00", True),     # Wed 10:00 New York (EDT)
    ("2026-11-21T15:00:00+00:00", False),    # Saturday
    ("2026-11-26T15:00:00+00:00", False),    # Thanksgiving
    ("2028-01-17T15:30:00+00:00", False),    # MLK day 2028, past the old list
])
def test_us_market_open(when, is_open):
    now = datetime.fromisoformat(when)
    assert monitor.us_market_open(now) is is_open
    assert monitor.halts_limit(now) == (3 if is_open else 12)


# ------------------------------------------------------------ state files

def test_save_json_never_leaves_a_half_written_file(sandbox, monkeypatch):
    path = monitor.STATE_DIR / "x.json"
    monitor.save_json(path, {"a": 1})

    def crash(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(monitor.json, "dumps", crash)
    with pytest.raises(OSError):
        monitor.save_json(path, {"a": 2})
    monkeypatch.undo()
    assert json.loads(path.read_text()) == {"a": 1}


def test_ledger_remembers_order_and_drops_the_oldest(sandbox):
    path = monitor.STATE_DIR / "ledger.json"
    led = monitor.Ledger(path, cap=3)
    for k in ("a", "b", "c", "b", "d"):
        led.add(k)
    assert "a" in led and "z" not in led and len(led) == 4
    led.save()
    assert json.loads(path.read_text()) == ["b", "c", "d"]
    again = monitor.Ledger(path, cap=3)
    assert "a" not in again and "d" in again


def test_ledger_survives_a_corrupt_file(sandbox):
    path = monitor.STATE_DIR / "ledger.json"
    path.write_text("{not json")
    assert len(monitor.Ledger(path, cap=5)) == 0
    path.write_text('{"a": 1}')
    assert len(monitor.Ledger(path, cap=5)) == 0


def test_stamp_keeps_last_ok_through_a_failure(sandbox):
    monitor.stamp("m.json", items=3)
    first = monitor.load_json(monitor.STATE_DIR / "m.json", {})
    monitor.stamp("m.json", error="feed unreachable")
    monitor.stamp("m.json", error="feed unreachable")
    st = monitor.load_json(monitor.STATE_DIR / "m.json", {})
    assert st["last_ok"] == first["last_ok"] and st["fail_count"] == 2
    assert st["last_error"] == "feed unreachable" and st["items"] == 3
    monitor.stamp("m.json")
    st = monitor.load_json(monitor.STATE_DIR / "m.json", {})
    assert st["fail_count"] == 0 and "last_error" not in st


def test_due_holds_its_interval(sandbox):
    assert monitor.due("job", 55) is True
    assert monitor.due("job", 55) is False
    sched = monitor.load_json(monitor.SCHEDULE_FILE, {})
    sched["job"] = iso(1)
    monitor.save_json(monitor.SCHEDULE_FILE, sched)
    assert monitor.due("job", 55) is True
    assert monitor.due("other", 5, mark=False) is True
    assert monitor.due("other", 5, mark=False) is True       # not marked until done
    monitor.mark_done("other")
    assert monitor.due("other", 5, mark=False) is False


def test_self_heal_resets_corrupt_files_and_trims_a_runaway_queue(sandbox):
    monitor.SEEN_FILE.write_text("{broken")
    monitor.save_json(monitor.QUEUE_FILE, [{"key": str(i)} for i in range(5200)])
    fixed = monitor.self_heal()
    assert any("seen.json" in f for f in fixed) and any("runaway" in f for f in fixed)
    assert json.loads(monitor.SEEN_FILE.read_text()) == []
    assert len(json.loads(monitor.QUEUE_FILE.read_text())) == 500


def test_prune_csv_keeps_the_window_and_repairs_the_header(sandbox):
    path = monitor.DATA_DIR / "alerts.csv"
    old, new = iso(24 * 400), iso(1)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(monitor.ALERTS_HEADER[:8])                 # the old 8-column header
        w.writerow([old, "FORM_144", "A", "Co", "h", "1", "", "l", "LOW"])
        w.writerow([new, "FORM_144", "B", "Co", "h", "1", "", "l", "LOW"])
        w.writerow(["not a date", "X", "C", "Co", "h", "1", "", "l"])
    monitor.prune_csv(path, monitor.ALERTS_HEADER, 365)
    rows = list(csv.reader(path.open()))
    assert rows[0] == monitor.ALERTS_HEADER
    assert [r[2] for r in rows[1:]] == ["B", "C"]             # old row gone, odd row kept
    assert all(len(r) == 9 for r in rows)


def test_digest_sent_this_week_blocks_a_repeat_but_an_early_copy_does_not():
    week = "2026-11-23"                                       # a Monday
    f = monitor.digest_already_sent
    assert f({"sent_week": week, "sent_at": "2026-11-22T23:01:00+00:00"}, week) is True
    assert f({"sent_week": week, "sent_at": "2026-11-23T02:30:00+00:00"}, week) is True
    assert f({"sent_week": week, "sent_at": "2026-11-18T05:00:00+00:00"}, week) is False
    assert f({"sent_week": "2026-11-16", "sent_at": "2026-11-15T23:00:00+00:00"}, week) is False
    assert f({}, week) is False
    assert f({"sent_week": week}, week) is False


# ----------------------------------------------------------- fear & greed

def fng(score, rating=None):
    d = {"score": score, "previous_close": 40.0, "previous_1_week": 35.0,
         "previous_1_month": 60.0}
    if rating:
        d["rating"] = rating
    return FakeResponse(200, json_data={"fear_and_greed": d})


def test_fear_and_greed_alerts_only_when_the_level_changes(tg):
    tg.get_script = [fng(30.0, "fear"), fng(33.0, "fear"), fng(50.2, "neutral")]
    monitor.check_fear_greed()                                # baseline
    monitor.check_fear_greed()                                # same level
    assert tg.sent == []
    monitor.check_fear_greed()
    msg = body_of(tg.texts[0])
    head, sub = msg.split("\n")[:2]
    assert head.startswith("<b>FEAR &amp; GREED</b>")
    assert "Now        NEUTRAL (50.2)" in msg and "Was        FEAR" in msg
    assert sub == "Level change · moved toward greed"


def test_fear_and_greed_failure_is_recorded_not_raised(tg):
    tg.get_script = [FakeResponse(403, "blocked")]
    monitor.check_fear_greed()
    assert json.loads(monitor.ERROR_FILE.read_text())["fear_greed"]["count"] == 1


@pytest.mark.parametrize("score,level", [(10, "extreme fear"), (25, "fear"), (44.9, "fear"),
                                         (45, "neutral"), (55, "neutral"), (56, "greed"),
                                         (75, "greed"), (76, "extreme greed")])
def test_fng_level_bands(score, level):
    assert monitor.fng_level(score) == level
