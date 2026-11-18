"""Message format and delivery: the part every module depends on."""

import json
from datetime import datetime, timedelta, timezone

import requests

import monitor
from conftest import FAKE_CHAT, FAKE_TOKEN, FakeResponse, body_of


# ---------------------------------------------------------------- format

def test_box_aligns_labels_and_marks_block_plaintext():
    out = monitor.box("INSIDER BUY", [("TICKER", "NVDA"), ("PERSON", "A & B <x>"),
                                      ("EMPTY", ""), ("NONE", None)],
                      link="https://example.org/a?b=1&c=2", footer="Note <1>")
    assert out.startswith('INSIDER BUY\n<pre><code class="language-plaintext">')
    assert "TICKER  NVDA\nPERSON  A &amp; B &lt;x&gt;</code></pre>" in out
    assert "EMPTY" not in out and "NONE" not in out          # blank rows dropped
    assert out.endswith("\nNote &lt;1&gt;\nhttps://example.org/a?b=1&amp;c=2")


def test_header_is_added_and_skipped_when_it_would_not_fit():
    short = monitor.with_preview_head("hello")
    assert short == "CONGRESS\n" + "⠀" * 56 + "\nhello"
    long_text = "x" * 3990
    assert monitor.with_preview_head(long_text) == long_text


def test_money_dmy_and_days_between():
    assert monitor.money(1_250_000_000) == "$1.25B"
    assert monitor.money(67_500_000) == "$67.50M"
    assert monitor.money(250_000) == "$250K"
    assert monitor.money("abc") == "n/a"
    assert monitor.dmy("2026-10-23") == "23 Oct"
    assert monitor.dmy("") == ""
    assert monitor.days_between("2026-08-25", "2026-08-27") == 2
    assert monitor.days_between("bad", "2026-08-27") == ""


# -------------------------------------------------------------- delivery

def test_message_payload_is_exactly_what_it_was_before_the_audit(tg):
    """Header, source tag, HTML mode, no link preview, buzzes."""
    assert monitor.telegram("TITLE\n<pre>body</pre>") is True
    assert len(tg.calls) == 1
    call = tg.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert call["timeout"] == 20
    assert call["json"] == {
        "chat_id": FAKE_CHAT,
        "text": "CONGRESS\n" + "⠀" * 56 + "\n\U0001F3DB️ TITLE\n<pre>body</pre>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": False,
    }


def test_not_configured_prints_and_returns_false(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(monitor, "TELEGRAM_TOKEN", "")
    assert monitor.telegram("hello") is False
    assert "hello" in capsys.readouterr().out


def test_server_error_is_retried_and_then_delivered(tg):
    tg.script = [FakeResponse(502, "bad gateway"), FakeResponse(200)]
    assert monitor.telegram("x") is True
    assert len(tg.calls) == 2 and len(tg.sent) == 1
    assert not monitor.outbox_path().exists()


def test_network_error_is_retried(tg):
    tg.script = [requests.ConnectionError("boom"), FakeResponse(200)]
    assert monitor.telegram("x") is True
    assert len(tg.sent) == 1


def test_rate_limit_waits_the_time_telegram_asks_for(tg, monkeypatch):
    waits = []
    monkeypatch.setattr(monitor.time, "sleep", waits.append)
    tg.script = [FakeResponse(429, "slow down",
                              {"ok": False, "parameters": {"retry_after": 7}}),
                 FakeResponse(200)]
    assert monitor.telegram("x") is True
    assert 7 in waits


def test_html_telegram_cannot_parse_is_resent_as_plain_text(tg):
    tg.script = [FakeResponse(400, "Bad Request: can't parse entities: bad tag")]
    assert monitor.telegram("TITLE\n<pre>a &lt; b</pre>") is True
    assert len(tg.sent) == 1
    plain = tg.sent[0]
    assert "parse_mode" not in plain
    assert "<pre>" not in plain["text"] and "a < b" in plain["text"]


def test_undeliverable_message_goes_to_the_outbox_not_into_thin_air(tg):
    tg.script = [FakeResponse(500)] * 3
    assert monitor.telegram("HALT XYZ") is True          # handled: queued
    box = json.loads(monitor.outbox_path().read_text())
    assert len(box) == 1 and "HALT XYZ" in box[0]["text"]
    assert len(tg.calls) == 3                            # three tries, no more


def test_once_telegram_is_down_later_messages_skip_straight_to_outbox(tg):
    tg.script = [FakeResponse(401, "Unauthorized")]
    monitor.telegram("first")
    calls_after_first = len(tg.calls)
    monitor.telegram("second")
    monitor.telegram("third")
    assert len(tg.calls) == calls_after_first            # no more attempts
    assert len(json.loads(monitor.outbox_path().read_text())) == 3


def test_outbox_is_sent_on_the_next_run_and_marked_delayed(tg, monkeypatch):
    tg.script = [FakeResponse(500)] * 3
    monitor.telegram("HALT XYZ")
    monkeypatch.setattr(monitor, "_telegram_down", False)    # a new run
    assert monitor.flush_outbox() == 1
    assert "HALT XYZ" in tg.texts[0]
    assert "Delayed delivery. First tried" in tg.texts[0] and "SGT" in tg.texts[0]
    assert not monitor.outbox_path().exists()


def test_outbox_keeps_what_still_fails_and_drops_what_is_too_old(tg):
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat(timespec="seconds")
    new = datetime.now(timezone.utc).isoformat(timespec="seconds")
    monitor.save_json(monitor.outbox_path(), [
        {"text": "ancient", "at": old}, {"text": "recent", "at": new},
        {"broken": True}])
    tg.script = [FakeResponse(500)] * 3
    assert monitor.flush_outbox() == 0
    left = json.loads(monitor.outbox_path().read_text())
    assert [i["text"] for i in left] == ["recent"]


def test_outbox_is_capped(tg, monkeypatch):
    monkeypatch.setattr(monitor, "_telegram_down", True)
    for i in range(monitor.OUTBOX_MAX + 5):
        monitor.telegram(f"m{i}")
    box = json.loads(monitor.outbox_path().read_text())
    assert len(box) == monitor.OUTBOX_MAX
    assert "m4" not in box[0]["text"] and "m5" in box[0]["text"]


def test_each_module_has_its_own_outbox_file(sandbox, monkeypatch):
    for name in ("halts", "congress", "trump", "earnings", "events", "monitor"):
        monkeypatch.setattr(monitor, "OUTBOX_NAME", name)
        assert monitor.outbox_path().name == f"outbox_{name}.json"
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "something_else")
    assert monitor.outbox_path().name == "outbox_monitor.json"


def test_heartbeat_mode_has_no_outbox_so_a_failed_send_reports_false(tg, monkeypatch):
    monkeypatch.setattr(monitor, "OUTBOX_ENABLED", False)
    tg.script = [FakeResponse(500)] * 3
    assert monitor.telegram("x") is False
    assert not monitor.outbox_path().exists()


def test_send_alert_counts_only_what_was_pushed(tg):
    assert monitor.send_alert("FORM_144", "ABC", "Co", "h", 1, "", "l", "msg", "LOW") is False
    assert monitor.alerts_sent == 0 and tg.sent == []
    assert monitor.send_alert("FORM_144", "ABC", "Co", "h", 1, "", "l", "msg", "MEDIUM") is True
    assert monitor.alerts_sent == 1
    rows = monitor.ALERTS_CSV.read_text().strip().splitlines()
    assert len(rows) == 3                                 # header + both alerts


# ------------------------------------------------------- secrets in logs

def test_the_bot_token_never_reaches_the_log(tg, capsys):
    leak = (f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded "
            f"with url: /bot{FAKE_TOKEN}/sendMessage")
    tg.script = [requests.ConnectionError(leak)] * 3
    monitor.telegram("x")
    monitor.log(f"something went wrong: {leak}")
    out = capsys.readouterr().out
    assert FAKE_TOKEN not in out
    assert "TESTtoken" not in out


def test_scrub_removes_keys_in_query_strings():
    s = monitor.scrub("GET https://finnhub.io/api/v1/x?symbol=A&token=abc123 failed")
    assert "abc123" not in s and "token=***" in s
    assert "/bot***" in monitor.scrub("/bot999999999:AAAbbbCCCdddEEEfff_ggg-hhh/sendMessage")


# ------------------------------------------------------- telegram check

def test_telegram_check_passes_when_the_bot_can_see_the_channel(tg):
    ok, _ = monitor.telegram_check()
    assert ok is True
    assert tg.calls[0]["url"].endswith("/getChat")
    assert tg.calls[0]["params"] == {"chat_id": FAKE_CHAT}


def test_telegram_check_reports_a_revoked_token_at_once(tg):
    tg.get_script = [FakeResponse(401, "Unauthorized")]
    ok, detail = monitor.telegram_check()
    assert ok is False and "401" in detail
    assert len(tg.calls) == 1


def test_telegram_check_retries_a_network_blip(tg):
    tg.get_script = [requests.Timeout("slow"), FakeResponse(200)]
    assert monitor.telegram_check()[0] is True


def test_body_helper_strips_header(tg):
    monitor.telegram("abc")
    assert body_of(tg.texts[0]) == "abc"
