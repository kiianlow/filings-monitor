"""
Shared test setup.

Every test runs in a sandbox: state and data files live in a temporary
folder, there is no network, nothing sleeps, and Telegram is a fake that
records what would have been sent. Nothing here can reach the real SCOUT
channel or touch the repo's own state/ and data/ folders.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SEC_USER_AGENT", "Test Suite tests@example.org")

import monitor  # noqa: E402
import halts  # noqa: E402
import congress  # noqa: E402
import trump  # noqa: E402
import earnings  # noqa: E402
import events  # noqa: E402

FAKE_TOKEN = "1234567890:TESTtoken_notreal_abcdefghijklmnopqrs"
FAKE_CHAT = "-1000000000001"


class FakeResponse:
    def __init__(self, status=200, text="", json_data=None, headers=None,
                 content=b""):
        self.status_code = status
        self.text = text
        self._json = json_data
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeTelegram:
    """Stands in for requests.Session on the Telegram side."""

    def __init__(self):
        self.sent = []          # payloads Telegram accepted
        self.calls = []         # every attempt, accepted or not
        self.script = []        # responses to give, in order; then 200s
        self.get_script = []

    def post(self, url, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        resp = self.script.pop(0) if self.script else FakeResponse(200)
        if isinstance(resp, Exception):
            raise resp
        if resp.status_code == 200:
            self.sent.append(json)
        return resp

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append({"url": url, "params": params})
        resp = self.get_script.pop(0) if self.get_script else FakeResponse(200)
        if isinstance(resp, Exception):
            raise resp
        return resp

    @property
    def texts(self):
        return [p["text"] for p in self.sent]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every module at a throwaway state/ and data/ folder."""
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    data.mkdir()

    m = monkeypatch.setattr
    m(monitor, "STATE_DIR", state)
    m(monitor, "DATA_DIR", data)
    for name, fname in (("SEEN_FILE", "seen.json"), ("QUEUE_FILE", "queue.json"),
                        ("HEALTH_FILE", "health.json"),
                        ("FIRST_SEEN_FILE", "first_seen.json"),
                        ("SEC_COMPANIES_FILE", "sec_companies.json"),
                        ("CLUSTER_STATE_FILE", "cluster_alerts.json"),
                        ("FNG_STATE", "fear_greed.json"),
                        ("ERROR_FILE", "errors.json"),
                        ("SCHEDULE_FILE", "schedule.json")):
        m(monitor, name, state / fname)
    m(monitor, "ALERTS_CSV", data / "alerts.csv")
    m(monitor, "BUYS_CSV", data / "buys.csv")

    m(halts, "STATE_DIR", state)
    m(halts, "SEEN_FILE", state / "halts_seen.json")
    m(halts, "STATE_FILE", state / "halts_state.json")
    m(congress, "STATE_DIR", state)
    m(congress, "DATA_DIR", data)
    m(congress, "SEEN_FILE", state / "congress_seen.json")
    m(congress, "HEALTH_FILE", state / "congress_health.json")
    m(congress, "RETRY_FILE", state / "congress_retry.json")
    m(congress, "TRADES_CSV", data / "congress_trades.csv")
    m(trump, "STATE_DIR", state)
    m(trump, "DATA_DIR", data)
    m(trump, "SEEN_FILE", state / "trump_seen.json")
    m(trump, "TRADES_CSV", data / "trump_trades.csv")
    m(earnings, "STATE_DIR", state)
    m(earnings, "STATE_FILE", state / "earnings_state.json")
    m(events, "STATE_DIR", state)
    m(events, "STATE_FILE", state / "events_state.json")

    # Per-run globals and caches.
    m(monitor, "alerts_sent", 0)
    m(monitor, "_telegram_down", False)
    m(monitor, "OUTBOX_ENABLED", True)
    m(monitor, "OUTBOX_NAME", "monitor")
    m(monitor, "_sec_rows", None)
    m(monitor, "_cik_index", None)
    m(monitor, "_name_index", None)
    m(monitor, "_cap_cache", {})
    m(monitor, "FINNHUB_KEY", "")
    m(congress, "alerts_sent", 0)

    # Nothing sleeps and nothing reaches the network.
    m(monitor.time, "sleep", lambda s: None)

    def no_network(*a, **kw):
        raise AssertionError("a test tried to use the real network")
    import requests
    m(requests.Session, "request", no_network)

    monitor.ensure_files()
    return tmp_path


@pytest.fixture
def tg(sandbox, monkeypatch):
    """A working, fake Telegram. tg.texts lists what was delivered."""
    fake = FakeTelegram()
    monkeypatch.setattr(monitor, "TELEGRAM_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", FAKE_CHAT)
    monkeypatch.setattr(monitor, "session", fake)
    return fake


def body_of(text):
    """A delivered message without the chat-list header and source tag."""
    head = f"{monitor.PREVIEW_HEAD}\n{monitor.PREVIEW_BLANK * monitor.PREVIEW_SPACER}\n"
    assert text.startswith(head), "message is missing the CONGRESS header"
    rest = text[len(head):]
    assert rest.startswith(monitor.SOURCE_TAG + " "), "message is missing the source tag"
    return rest[len(monitor.SOURCE_TAG) + 1:]
