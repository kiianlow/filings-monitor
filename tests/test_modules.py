"""halts.py, congress.py and trump.py."""

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

import congress
import halts
import monitor
import trump
from conftest import FakeResponse, body_of

UTC = timezone.utc


# ================================================================== halts

def halt_item(symbol, code, time="09:41:23", date="11/17/2026", name="Acme Corp",
              rdate="", rtime=""):
    return (f"<item><title>{symbol}</title><ndm:IssueSymbol>{symbol}</ndm:IssueSymbol>"
            f"<ndm:IssueName>{name}</ndm:IssueName><ndm:ReasonCode>{code}</ndm:ReasonCode>"
            f"<ndm:HaltDate>{date}</ndm:HaltDate><ndm:HaltTime>{time}</ndm:HaltTime>"
            f"<ndm:Market>NASDAQ</ndm:Market><ndm:ResumptionDate>{rdate}</ndm:ResumptionDate>"
            f"<ndm:ResumptionTradeTime>{rtime}</ndm:ResumptionTradeTime>"
            f"<ndm:PauseThresholdPrice></ndm:PauseThresholdPrice></item>")


def halts_feed(*items):
    return ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0" '
            'xmlns:ndm="http://www.nasdaqtrader.com/"><channel>'
            + "".join(items) + "</channel></rss>")


@pytest.fixture
def feed(tg, monkeypatch):
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "halts")
    holder = {"xml": halts_feed()}
    monkeypatch.setattr(halts, "fetch_feed", lambda tries=2: holder["xml"])
    return holder


def test_parse_copes_with_the_mis_decoded_byte_order_mark():
    xml = "ï»¿" + halts_feed(halt_item("ABC", "t1"))
    rows = halts.parse(xml)
    assert rows[0]["symbol"] == "ABC" and rows[0]["code"] == "T1"
    assert halts.parse(b"\xef\xbb\xbf" + halts_feed(halt_item("ABC", "T1")).encode())[0]["symbol"] == "ABC"
    assert halts.parse("<html>blocked</html") is None


def test_alert_codes_are_news_suspensions_and_the_real_market_wide_ones():
    assert halts.ALERT_ON == {"T1", "H10", "MWC0", "MWC1", "MWC2", "MWC3", "MWCQ"}
    assert "M" not in halts.ALERT_ON and "LUDP" not in halts.ALERT_ON
    assert "market" not in halts.HALT_CODES["M"].lower().replace("nyse", "")
    assert all(c in halts.HALT_CODES for c in halts.ALERT_ON)


def test_single_stock_volatility_pauses_no_longer_alert(feed, tg):
    feed["xml"] = halts_feed(halt_item("AAA", "M"), halt_item("BBB", "LUDP"),
                             halt_item("CCC", "T2"), halt_item("DDD", "D"))
    halts.run()
    assert tg.sent == []
    assert len(json.loads(halts.SEEN_FILE.read_text())) == 4      # still recorded


def test_news_pending_halt_alerts_once_with_new_york_and_singapore_time(feed, tg):
    feed["xml"] = halts_feed(halt_item("ABC", "T1", time="09:41:23", date="11/17/2026"))
    halts.run()
    halts.run()                                                   # same feed again
    assert len(tg.sent) == 1
    msg = body_of(tg.texts[0])
    assert msg.startswith("TRADING HALT\n")
    assert "EVENT     T1 - News pending" in msg
    assert "HALTED    09:41 ET 17 Nov (22:41 SGT)" in msg
    assert "RESUME    not yet announced" in msg
    assert "T1 means news is pending." in msg
    st = json.loads(halts.STATE_FILE.read_text())
    assert st["items_in_feed"] == 1 and st["fail_count"] == 0 and "last_ok" in st


@pytest.mark.parametrize("d,t,want", [
    ("11/17/2026", "09:41:23", "09:41 ET 17 Nov (22:41 SGT)"),             # winter, UTC-5
    ("07/15/2026", "09:41:23", "09:41 ET 15 Jul (21:41 SGT)"),             # summer, UTC-4
    ("11/17/2026", "15:50:00", "15:50 ET 17 Nov (04:50 SGT 18 Nov)"),      # next day in SG
    ("", "", ""), ("11/17/2026", "", "11/17/2026"), ("bad", "09:41:23", "09:41:23 bad"),
])
def test_when_label(d, t, want):
    assert halts.when_label(d, t) == want


def test_more_than_three_halts_are_grouped_into_one_message(feed, tg):
    feed["xml"] = halts_feed(*[halt_item(f"S{i}", "T1", time=f"10:0{i}:00") for i in range(5)])
    halts.run()
    assert len(tg.sent) == 1
    msg = body_of(tg.texts[0])
    assert msg.startswith("5 TRADING HALTS") and "S0" in msg and "S4" in msg


def test_market_wide_circuit_breaker_is_one_message_however_many_rows(feed, tg):
    rows = [halt_item(f"S{i}", "MWC1", time="13:05:00") for i in range(40)]
    feed["xml"] = halts_feed(halt_item("ABC", "T1"), *rows)
    halts.run()
    titles = [body_of(t).split("\n")[0] for t in tg.texts]
    assert titles == ["MARKET-WIDE TRADING HALT", "TRADING HALT"]
    mw = body_of(tg.texts[0])
    assert "MWC1 - MARKET-WIDE CIRCUIT BREAKER, LEVEL 1" in mw
    assert "ROWS      40 in the feed under this code" in mw
    assert "13:05 ET" in mw and "S17" not in mw


def test_market_wide_resumption_has_its_own_title(feed, tg):
    feed["xml"] = halts_feed(halt_item("ALL", "MWCQ", time="13:20:00"))
    halts.run()
    assert body_of(tg.texts[0]).startswith("MARKET-WIDE HALT ENDED")


def test_feed_failure_is_stamped_and_keeps_the_last_good_time(feed, tg, monkeypatch):
    feed["xml"] = halts_feed(halt_item("ABC", "T1"))
    halts.run()
    good = json.loads(halts.STATE_FILE.read_text())["last_ok"]
    monkeypatch.setattr(halts, "fetch_feed", lambda tries=2: None)
    halts.run()
    monkeypatch.setattr(halts, "fetch_feed", lambda tries=2: "<html>block page</html")
    halts.run()
    st = json.loads(halts.STATE_FILE.read_text())
    assert st["last_ok"] == good and st["fail_count"] == 2
    assert st["last_error"] == "feed not parseable as XML"


def test_halt_that_cannot_be_sent_is_delivered_by_the_next_run(feed, tg, monkeypatch):
    feed["xml"] = halts_feed(halt_item("ABC", "T1"))
    tg.script = [FakeResponse(503)] * 3
    halts.run()
    assert tg.sent == [] and (monitor.STATE_DIR / "outbox_halts.json").exists()
    monkeypatch.setattr(monitor, "_telegram_down", False)          # next run
    halts.run()
    assert len(tg.sent) == 1
    assert "TRADING HALT" in tg.texts[0] and "Delayed delivery" in tg.texts[0]
    assert not (monitor.STATE_DIR / "outbox_halts.json").exists()


def test_seen_ledger_is_capped(feed, tg, monkeypatch):
    monkeypatch.setattr(halts, "SEEN_MAX", 3)
    feed["xml"] = halts_feed(*[halt_item(f"S{i}", "LUDP") for i in range(6)])
    halts.run()
    assert len(json.loads(halts.SEEN_FILE.read_text())) == 3


# =============================================================== congress

WRAPPED = """ID Owner Asset Transaction Type Date Notification Date Amount
SP Berkshire Hathaway Inc. New P 07/28/2026 08/01/2026 $15,001 -
Common Stock (BRK.B) [ST] $50,000
JT NVIDIA Corporation - Common Stock (NVDA) [ST] S (partial) 07/29/2026 08/01/2026 $500,001 - $1,000,000
City of Springfield Revenue Bonds [GS] P 07/30/2026 08/01/2026 $100,001 - $250,000
"""


def test_house_rows_survive_line_wrapping():
    rows = {r["ticker"] or r["asset"]: r for r in congress.extract_house_rows(WRAPPED)}
    assert set(rows) >= {"BRK.B", "NVDA"}
    assert (rows["BRK.B"]["action"], rows["BRK.B"]["low"], rows["BRK.B"]["high"]) == ("BUY", 15001, 50000)
    assert rows["BRK.B"]["trade_date"] == "2026-07-28"
    assert rows["NVDA"]["action"] == "SELL (partial)" and rows["NVDA"]["low"] == 500001
    bond = [r for r in rows.values() if "Springfield" in r["asset"]][0]
    assert bond["ticker"] == "" and bond["low"] == 100001


def test_amounts_and_bands():
    assert congress.parse_amount("$1,001 - $15,000") == (1001, 15000)
    assert congress.parse_amount("Over $50,000,000") == (50000000, 50000000)
    assert congress.parse_amount("none") == (0, 0)
    assert [congress.classify(x) for x in (15001, 50001, 500001)] == ["LOW", "MEDIUM", "HIGH"]
    assert congress.amount_label(0, 0) == "not disclosed"


@pytest.mark.parametrize("asset,ticker,equity", [
    ("NVIDIA Corporation - Common Stock", "NVDA", True),
    ("Brookfield Renewable Partners L.P", "BEP", True),          # was wrongly blocked
    ("Energy Transfer LP Common Units", "ET", True),             # was wrongly blocked
    ("Perella Weinberg Partners - Class A", "PWP", True),        # was wrongly blocked
    ("Dorchester Minerals, L.P. - Common", "DMLP", True),        # was wrongly blocked
    ("Springfield Family Holdings LLC", "", False),              # private: no ticker
    ("U.S. Treasury Bill", "", False),
    ("Acme Corp 5.25% Senior Notes", "ACME", False),             # debt, even with a ticker
    ("Big City Municipal Bond Fund", "BCM", False),
])
def test_is_equity(asset, ticker, equity):
    assert congress.is_equity({"asset": asset, "ticker": ticker}) is equity


@pytest.mark.parametrize("name,hit", [
    ("Nancy Pelosi", True), ("Daniel Crenshaw", True), ("Michael Patrick Guest", True),
    ("Terri A. Sewell", True), ("Debbie Wasserman Schultz", True), ("Mike Johnson", True),
    ("Julie Johnson", False), ("Scott Peters", False), ("Scott Scott Franklin", False),
    ("Rick Scott", True), ("Pelosi", False), ("", False), ("Roger Williams Jr.", True),
])
def test_notable_member_matching(name, hit):
    assert congress.is_notable_member(name) is hit


def test_january_reads_last_years_index_too():
    assert congress.index_years(datetime(2027, 1, 5, tzinfo=UTC)) == [2027, 2026]
    assert congress.index_years(datetime(2027, 2, 1, tzinfo=UTC)) == [2027]
    assert congress.index_years(datetime(2026, 12, 31, tzinfo=UTC)) == [2026]


def filing(doc_id, days_ago=1, member="Nancy Pelosi", year=None):
    d = datetime.now(UTC) - timedelta(days=days_ago)
    return {"doc_id": doc_id, "member": member, "state": "CA11",
            "filed": d.strftime("%m/%d/%Y"), "year": str(year or d.year)}


BIG_TRADE = [{"asset": "NVIDIA Corporation", "ticker": "NVDA", "action": "BUY",
              "low": 1000001, "high": 5000000, "trade_date": "2026-10-20"}]


@pytest.fixture
def house(tg, monkeypatch):
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "congress")
    world = {"index": [], "pdf": {}}
    monkeypatch.setattr(congress, "fetch_house_index", lambda year: world["index"])
    monkeypatch.setattr(congress, "parse_house_pdf",
                        lambda doc, year: world["pdf"].get(doc, [dict(t) for t in BIG_TRADE]))
    with congress.TRADES_CSV.open("w", newline="") as f:
        csv.writer(f).writerow(congress.TRADES_HEADER)
    return world


def run_house():
    seen = monitor.Ledger(congress.SEEN_FILE, congress.SEEN_MAX)
    health = {}
    congress.run_house(seen, health)
    seen.save()
    return seen, health


def test_one_message_per_filing_and_every_trade_logged(house, tg):
    house["index"] = [filing("20001")]
    house["pdf"]["20001"] = [dict(BIG_TRADE[0]),
                             {"asset": "Small Co", "ticker": "SML", "action": "SELL",
                              "low": 1001, "high": 15000, "trade_date": "2026-10-21"},
                             {"asset": "U.S. Treasury Bill", "ticker": "", "action": "BUY",
                              "low": 250001, "high": 500000, "trade_date": "2026-10-22"}]
    seen, health = run_house()
    assert len(tg.sent) == 1 and "house" in health and "H|20001" in seen
    msg = body_of(tg.texts[0])
    assert msg.startswith("CONGRESS FILING\n")
    assert "PRIORITY  HIGH  ★ WATCHLIST" in msg and "MEMBER    Nancy Pelosi" in msg
    assert "BUY NVDA" in msg and "SELL SML" in msg            # watchlist: small trades too
    assert "Treasury" not in msg and "1 bonds/funds" in msg
    assert "ptr-pdfs" in msg and "/20001.pdf" in msg
    rows = list(csv.DictReader(congress.TRADES_CSV.open()))
    assert len(rows) == 3


def test_ordinary_member_only_pushes_trades_over_the_floor(house, tg):
    house["index"] = [filing("20002", member="Jane Ordinary")]
    house["pdf"]["20002"] = [{"asset": "Small Co", "ticker": "SML", "action": "BUY",
                              "low": 15001, "high": 50000, "trade_date": "2026-10-21"}]
    run_house()
    assert tg.sent == []
    assert len(list(csv.DictReader(congress.TRADES_CSV.open()))) == 1


def test_old_backlog_is_absorbed_silently(house, tg):
    house["index"] = [filing("20003", days_ago=40)]
    seen, _ = run_house()
    assert tg.sent == [] and "H|20003" in seen


def test_scanned_filing_gets_the_open_manually_message(house, tg):
    house["index"] = [filing("20004")]
    house["pdf"]["20004"] = []
    run_house()
    assert body_of(tg.texts[0]).startswith("CONGRESS FILING - open manually")


def test_pdf_that_fails_to_download_is_retried_not_reported_as_unreadable(house, tg):
    house["index"] = [filing("20005")]
    house["pdf"]["20005"] = None                              # download failed
    seen, _ = run_house()
    assert tg.sent == [] and "H|20005" not in seen
    assert json.loads(congress.RETRY_FILE.read_text()) == {"20005": 1}
    house["pdf"]["20005"] = [dict(BIG_TRADE[0])]              # the Clerk recovers
    seen, _ = run_house()
    assert len(tg.sent) == 1 and "H|20005" in seen
    assert not congress.RETRY_FILE.exists()


def test_pdf_that_never_downloads_is_reported_after_six_tries(house, tg):
    house["index"] = [filing("20006")]
    house["pdf"]["20006"] = None
    for _ in range(congress.MAX_PDF_TRIES - 1):
        seen, _ = run_house()
        assert tg.sent == [] and "H|20006" not in seen
    seen, _ = run_house()
    assert "H|20006" in seen
    assert body_of(tg.texts[0]).startswith("CONGRESS FILING - open manually")


def test_alert_cap_leaves_the_remaining_filings_for_the_next_run(house, tg, monkeypatch):
    monkeypatch.setattr(congress, "MAX_ALERTS_PER_RUN", 2)
    house["index"] = [filing(str(30000 + i)) for i in range(5)]
    seen, _ = run_house()
    assert len(tg.sent) == 2 and len(seen) == 2
    monkeypatch.setattr(congress, "alerts_sent", 0)           # a new run
    seen, _ = run_house()
    assert len(tg.sent) == 4 and len(seen) == 4


def test_newest_filing_first_across_a_year_boundary(house, tg, monkeypatch):
    monkeypatch.setattr(congress, "MAX_FILINGS_PER_RUN", 1)
    now = datetime.now(UTC)
    older = {"doc_id": "111", "member": "Nancy Pelosi", "state": "CA",
             "filed": (now - timedelta(days=5)).strftime("%m/%d/%Y"), "year": "2026"}
    newer = {"doc_id": "222", "member": "Nancy Pelosi", "state": "CA",
             "filed": (now - timedelta(days=1)).strftime("%m/%d/%Y"), "year": "2026"}
    house["index"] = [older, newer]
    seen, _ = run_house()
    assert "H|222" in seen and "H|111" not in seen


def test_house_index_failure_leaves_health_unstamped(house, tg, monkeypatch):
    monkeypatch.setattr(congress, "fetch_house_index", lambda year: None)
    _, health = run_house()
    assert health == {}


def test_missing_new_year_index_is_not_a_failure_in_january(house, tg, monkeypatch):
    monkeypatch.setattr(congress, "index_years", lambda now: [2027, 2026])
    monkeypatch.setattr(congress, "fetch_house_index",
                        lambda year: None if year == 2027 else [filing("20007")])
    seen, health = run_house()
    assert "house" in health and "H|20007" in seen


# ================================================================== trump

@pytest.fixture
def sec_names(sandbox):
    monitor.save_json(monitor.SEC_COMPANIES_FILE, [
        ["1", "SNAP", "Snap Inc"], ["2", "SNA", "Snap-on Inc"],
        ["3", "GSBD", "Goldman Sachs BDC, Inc."], ["4", "ZS", "Zscaler, Inc."],
        ["5", "TH", "Target Hospitality Corp."]])
    return sandbox


@pytest.mark.parametrize("name,want", [
    ("Apple Inc", "AAPL"),                       # exact
    ("SNAP INC CLASS A", "SNAP"),                # was SNA (Snap-on)
    ("IDEXX LABS INC", "IDXX"),                  # was IEX (IDEX Corp)
    ("IDEXCORP I", "IEX"),                       # run-together suffix plus OCR noise
    ("NETFLIXINC", "NFLX"), ("LINDEPLCF", "LIN"),
    ("PFIZER INC I", "PFE"), ("CHUBB LTD F", "CB"), ("BOEING PANY", "BA"),
    ("AMAZON.COM INC", "AMZN"), ("MERCK & CO INC", "MRK"),
    ("OLD DOMINION FREIGHT LINES INC", "ODFL"), ("CONSTELLATION ENERGY COR", "CEG"),
    ("ADVANCED MICRO DEVIC", "AMD"), ("INTERCONTINENTAL EXCHANG", "ICE"),
    ("FOX CORP CLA", "FOXA"), ("FOX CORP CLB", "FOX"),
    ("Zscaler Inc", "ZS"),                       # SEC list
    ("Goldman Sachs BDC Inc", "GSBD"),           # SEC list beats the "goldman sachs" prefix
    ("Target Hospitality Corp", "TH"),           # SEC list, not TGT
    ("Target Hospitality Partners", ""),         # unknown: never guessed as TGT
    ("Dow Jones Industrial Index", ""),          # not DOW
    ("Newsmax Inc", ""),                         # not NWSA
    ("Klarna Group", ""),                        # not KLAC
    ("Applied", ""),                             # too short to trust as a truncation
    ("= INC", ""), ("", ""),
])
def test_resolve_ticker_never_guesses(sec_names, name, want):
    assert trump.resolve_ticker(name) == want


OCR = """1 | Apple Inc Purchase 09/02/2026 $1,000,001 - $5,000,000
2 NVIDIA CORP wurchase 09/03/2026 $500,001 - $1,000,000
s Howmet Aerospace Inc Sale 09/04/2026 $15,002 - $50,000
4 City of Austin Texas Revenue Bonds due 2031 Purchase 09/05/2026 $250,001 - $500,000
5 NVIDIA CORP Purchase 09/08/2026 $500,001 - $1,000,000
garbage line with no trade in it
"""


def test_278t_rows_tolerate_ocr_damage(sec_names):
    rows = trump.extract_rows(OCR)
    assert [(r["ticker"], r["action"], r["low"], r["high"]) for r in rows[:3]] == [
        ("AAPL", "BUY", 1000001, 5000000), ("NVDA", "BUY", 500001, 1000000),
        ("HWM", "SELL", 15001, 50000)]                            # $15,002 snapped to the band
    assert rows[0]["trade_date"] == "2026-09-02"
    equities = [r for r in rows if trump.is_equity(r)]
    assert len(rows) == 5 and len(equities) == 4                  # the bond is dropped
    agg = {(a["ticker"], a["action"]): a for a in trump.aggregate(equities)}
    nv = agg[("NVDA", "BUY")]
    assert (nv["count"], nv["low"], nv["high"]) == (2, 1000002, 2000000)


def test_trump_filing_message_and_csv(sec_names, tg):
    with trump.TRADES_CSV.open("w", newline="") as f:
        csv.writer(f).writerow(trump.TRADES_HEADER)
    rows = trump.extract_rows(OCR)
    assert trump.send_filing("Donald-J-Trump-10.01.2026-278T.pdf", "https://x/y.pdf",
                             "2026-10-01", rows) is True
    msg = body_of(tg.texts[0])
    assert msg.startswith("TRUMP 278-T - EQUITY")
    assert "BUY AAPL  1x   $1.00M - $5.00M" in msg
    assert "BUY NVDA  2x   $1.00M - $2.00M" in msg
    assert "HWM" not in msg                                       # under the $1M floor
    logged = list(csv.DictReader(trump.TRADES_CSV.open()))
    assert len(logged) == 4 and logged[0]["ticker"] == "AAPL"     # every equity row


def test_discovery_falling_back_to_seeds_is_stamped_as_a_failure(sec_names, tg, monkeypatch):
    monkeypatch.setattr(monitor, "OUTBOX_NAME", "trump")
    monkeypatch.setattr(trump.sys, "argv", ["trump.py"])
    monkeypatch.setattr(trump.session, "get", lambda url, timeout=None: FakeResponse(503))
    trump.main()
    st = json.loads((monitor.STATE_DIR / "trump_state.json").read_text())
    assert st["last_error"] == "discovery failed, only the seed list"
    assert st["fail_count"] == 1 and "last_ok" not in st and st["documents"] == 5
    assert tg.sent == []                                          # old seeds absorbed silently


def test_live_index_is_stamped_healthy(sec_names, tg, monkeypatch):
    monkeypatch.setattr(trump.sys, "argv", ["trump.py"])
    page = ('<a href="/201/Presiden.nsf/PAS+Index/ABCDEF0123456789ABCDEF0123456789/'
            '$FILE/Donald-J-Trump-01.02.2020-278T.pdf">x</a>')
    monkeypatch.setattr(trump.session, "get", lambda url, timeout=None: FakeResponse(200, page))
    trump.main()
    st = json.loads((monitor.STATE_DIR / "trump_state.json").read_text())
    assert "last_ok" in st and st["fail_count"] == 0 and st["documents"] == 1
    assert json.loads(trump.SEEN_FILE.read_text())[0].endswith("278T.pdf")


def test_snap_and_label_date():
    assert trump.snap(15002, trump.BANDS) == 15001
    assert trump.snap(77777, trump.BANDS) == 77777
    assert trump.label_date("Donald-J-Trump-08.12.2026-278T.pdf") == "2026-08-12"
    assert trump.label_date("nothing here") == ""
    assert trump.is_trump_278t("Trump, Donald J.-05.08.2026-278T(2).pdf")
    assert not trump.is_trump_278t("Vance-JD-278T.pdf")
