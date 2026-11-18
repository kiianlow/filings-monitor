"""SEC side of monitor.py: discovery, the queue, Form 4, Form 144, stakes."""

import csv
import json
from datetime import datetime, timezone

import pytest

import monitor
from conftest import body_of

NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")


def atom(entries):
    """entries: list of (form_title, company, cik, accession)."""
    items = "".join(
        f"<entry><title>{form} - {co} ({cik}) (Reporting)</title>"
        f'<link rel="alternate" type="text/html" href="https://www.sec.gov/'
        f'Archives/edgar/data/{int(cik)}/{acc.replace("-", "")}/{acc}-index.htm"/>'
        f"<updated>{TODAY}T16:05:00-05:00</updated></entry>"
        for form, co, cik, acc in entries)
    return ('<?xml version="1.0" encoding="ISO-8859-1"?>'
            f'<feed xmlns="http://www.w3.org/2005/Atom">{items}</feed>')


def acc(n):
    return f"0001234567-26-{n:06d}"


def form4_xml(ticker="ACME", owner="Doe Jane", shares=10000, price=30.0,
              after=40000, code="P", ad="A", planned=False, doc_type="4",
              director=True, officer_title="", date=TODAY, extra_owner=""):
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <documentType>{doc_type}</documentType>
  <issuer><issuerCik>0000111111</issuerCik><issuerName>Acme Corp</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>{1 if director else 0}</isDirector>
      <isOfficer>{1 if officer_title else 0}</isOfficer>
      <officerTitle>{officer_title}</officerTitle></reportingOwnerRelationship>
  </reportingOwner>{extra_owner}
  <aff10b5One>{1 if planned else 0}</aff10b5One>
  <nonDerivativeTable><nonDerivativeTransaction>
    <transactionDate><value>{date}</value></transactionDate>
    <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>{shares}</value></transactionShares>
      <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
    </transactionAmounts>
    <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value>
    </sharesOwnedFollowingTransaction></postTransactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>"""


def item(form="4", n=1, company="Acme Corp", actual=None):
    a = acc(n)
    return {"key": f"{form}|{a}", "form": form, "accession": a,
            "company": company, "cik": "0000111111",
            "link": f"https://www.sec.gov/Archives/edgar/data/111111/{a.replace('-', '')}/{a}-index.htm",
            "filed": TODAY, "actual_form": actual or form}


class Sec:
    """A fake SEC: answers monitor.fetch from a dict of url-suffix -> body."""

    def __init__(self, monkeypatch):
        self.pages = {}
        self.fetched = []
        monkeypatch.setattr(monitor, "fetch", self.fetch)

    def fetch(self, url, tries=3, is_sec=True, extra_headers=None):
        self.fetched.append(url)
        for suffix, body in self.pages.items():
            if url.endswith(suffix) or suffix in url:
                return body() if callable(body) else body
        return None

    def filing(self, it, xml, form_type="4"):
        folder = it["link"][:it["link"].rfind("/")]
        self.pages[f"{folder}/index.json"] = json.dumps({"directory": {"item": [
            {"name": "primary_doc.xml", "type": form_type},
            {"name": "R1.xml"}, {"name": f"{it['accession']}-index.htm"}]}})
        self.pages[f"{folder}/primary_doc.xml"] = xml


@pytest.fixture
def sec(tg, monkeypatch):
    return Sec(monkeypatch)


def alerts_csv():
    with monitor.ALERTS_CSV.open(newline="") as f:
        return list(csv.DictReader(f))


def buys_csv():
    with monitor.BUYS_CSV.open(newline="") as f:
        return list(csv.DictReader(f))


# -------------------------------------------------------------- discovery

def test_title_to_form_handles_edgars_two_naming_styles():
    f = monitor.title_to_form
    assert f("SCHEDULE 13G/A - GoPro, Inc. (0001500435) (Subject)", ["SC 13D", "SC 13G"]) == ("SC 13G", "SCHEDULE 13G/A")
    assert f("SC 13D - X (0000000001)", ["SC 13D", "SC 13G"])[0] == "SC 13D"
    assert f("4 - Doe Jane (0000000002) (Reporting)", ["4"])[0] == "4"
    assert f("3 - Doe Jane (0000000002) (Reporting)", ["4"])[0] is None


def test_feed_url_keeps_the_literal_plus_and_adds_start_only_when_paging():
    first = monitor.feed_url("SCHEDULE+13")
    assert "type=SCHEDULE+13&" in first and "start=" not in first
    assert "count=100&output=atom" in first
    assert "&start=100&" in monitor.feed_url("4", start=100)


def test_discover_queues_new_filings_and_stamps_health(sec):
    sec.pages["type=4&"] = atom([("4", "Doe Jane", "0000000002", acc(1)),
                                 ("4/A", "Roe Rick", "0000000003", acc(2)),
                                 ("3", "Not Watched", "0000000004", acc(3))])
    sec.pages["type=144&"] = atom([("144", "Acme Corp", "0000111111", acc(4))])
    sec.pages["type=SCHEDULE+13&"] = atom(
        [("SCHEDULE 13D/A", "Target Inc", "0000222222", acc(5))])
    queue = monitor.discover({f"4|{acc(2)}"})
    assert [q["key"] for q in queue] == [f"4|{acc(1)}", f"144|{acc(4)}", f"SC 13D|{acc(5)}"]
    assert queue[0]["company"] == "Doe Jane" and queue[0]["cik"] == "0000000002"
    assert queue[2]["actual_form"] == "SCHEDULE 13D/A"
    health = json.loads(monitor.HEALTH_FILE.read_text())
    assert set(health) == {"4", "144", "SC 13D", "SC 13G"}
    # Second pass adds nothing: everything is already queued.
    assert len(monitor.discover({f"4|{acc(2)}"})) == 3


def test_discover_reads_further_pages_when_a_whole_page_is_new(sec, monkeypatch):
    monkeypatch.setattr(monitor, "FEED_COUNT", 3)
    page1 = atom([("4", "A", "0000000001", acc(n)) for n in (9, 8, 7)])
    page2 = atom([("4", "A", "0000000001", acc(n)) for n in (6, 5, 4)])
    page3 = atom([("4", "A", "0000000001", acc(n)) for n in (3, 2, 1)])
    sec.pages = {"type=4&company=&dateb=&owner=include&count": page1,
                 "type=4&company=&dateb=&owner=include&start=3&": page2,
                 "type=4&company=&dateb=&owner=include&start=6&": page3,
                 "type=144&": atom([]), "type=SCHEDULE+13&": atom([]),
                 "type=&company": atom([])}
    queue = monitor.discover({f"4|{acc(5)}"})            # one on page 2 is known
    got = sorted(q["accession"] for q in queue)
    assert got == sorted(acc(n) for n in (9, 8, 7, 6, 4))
    assert not any("start=6" in u for u in sec.fetched)   # stopped at the known one


def test_discover_stops_if_edgar_ignores_paging(sec, monkeypatch):
    monkeypatch.setattr(monitor, "FEED_COUNT", 2)
    same = atom([("4", "A", "0000000001", acc(n)) for n in (2, 1)])
    sec.pages = {"type=4&": same, "type=144&": atom([]),
                 "type=SCHEDULE+13&": atom([]), "type=&company": atom([])}
    assert len(monitor.discover(set())) == 2
    assert sum("type=4&" in u for u in sec.fetched) == 2   # page 1, then one retry


def test_discover_never_reads_more_than_the_page_limit(sec, monkeypatch):
    monkeypatch.setattr(monitor, "FEED_COUNT", 1)
    counter = {"n": 0}

    def fresh_page():
        counter["n"] += 1
        return atom([("4", "A", "0000000001", acc(counter["n"]))])
    sec.pages = {"type=4&": fresh_page, "type=144&": atom([]),
                 "type=SCHEDULE+13&": atom([]), "type=&company": atom([])}
    monitor.discover(set())
    assert counter["n"] == monitor.FEED_MAX_PAGES


def test_discover_survives_an_unreachable_or_broken_feed(sec):
    sec.pages["type=144&"] = "<not xml"
    assert monitor.discover(set()) == []
    assert json.loads(monitor.HEALTH_FILE.read_text()) == {}


# ------------------------------------------------------------------ drain

def test_filing_that_fails_to_download_is_retried_then_dropped_and_reported(sec, monkeypatch):
    it = item()
    seen = []
    for expected_tries in (1, 2):
        queue = [dict(it)] if expected_tries == 1 else json.loads(monitor.QUEUE_FILE.read_text())
        monitor.drain(queue, seen)
        left = json.loads(monitor.QUEUE_FILE.read_text())
        assert len(left) == 1 and left[0]["tries"] == expected_tries
        assert seen == []                                # not marked seen yet
    monitor.drain(json.loads(monitor.QUEUE_FILE.read_text()), seen)
    assert json.loads(monitor.QUEUE_FILE.read_text()) == []
    assert json.loads(monitor.SEEN_FILE.read_text()) == [it["key"]]
    errs = json.loads(monitor.ERROR_FILE.read_text())
    assert errs["filings_unreadable"]["count"] == 1 and it["accession"] in errs["filings_unreadable"]["last"]


def test_retried_filing_succeeds_when_the_sec_recovers(sec):
    it = item()
    monitor.drain([dict(it)], [])
    sec.filing(it, form4_xml(shares=10000, price=30, after=40000))
    monitor.drain(json.loads(monitor.QUEUE_FILE.read_text()), [])
    assert len(buys_csv()) == 1
    assert json.loads(monitor.QUEUE_FILE.read_text()) == []


def test_a_crashing_handler_does_not_stop_the_run(sec, monkeypatch):
    def boom(it):
        raise RuntimeError("parser bug")
    monkeypatch.setitem(monitor.HANDLERS, "4", boom)
    a, b = item(n=1), item(n=2)
    monitor.drain([a, b], [])
    assert json.loads(monitor.SEEN_FILE.read_text()) == [a["key"], b["key"]]


def test_drain_stops_at_the_alert_cap_and_keeps_the_rest_queued(sec, monkeypatch):
    monkeypatch.setattr(monitor, "MAX_ALERTS_PER_RUN", 1)
    items = [item(n=n) for n in (1, 2, 3)]
    for it in items:
        sec.filing(it, form4_xml(owner=f"Person {it['accession'][-1]}",
                                 shares=20000, price=30, after=20000))
    monitor.drain(list(items), [])
    assert len(json.loads(monitor.QUEUE_FILE.read_text())) == 2


# ----------------------------------------------------------------- Form 4

def test_open_market_buy_over_the_thresholds_alerts_at_medium(sec, tg):
    it = item()
    sec.filing(it, form4_xml(shares=10000, price=30.0, after=40000,
                             officer_title="Chief Financial Officer"))
    assert monitor.handle_form4(it) is True
    msg = body_of(tg.texts[0])
    assert msg.startswith("INSIDER BUY\n")
    for line in ("PRIORITY  MEDIUM", "TICKER    ACME", "PERSON    Doe Jane",
                 "ROLE      Director, Chief Financial Officer",
                 "SHARES    10,000", "PRICE     $30.00", "VALUE     $300K",
                 "HOLDING   +33.3%"):
        assert line in msg, line
    assert it["link"] in msg
    row = buys_csv()[0]
    assert (row["ticker"], row["value_usd"], row["holding_change_pct"]) == ("ACME", "300000", "33.3")
    assert alerts_csv()[0]["priority"] == "MEDIUM"


def test_small_buy_is_logged_but_not_pushed(sec, tg):
    it = item()
    sec.filing(it, form4_xml(shares=100, price=30.0, after=40000))
    assert monitor.handle_form4(it) is False
    assert tg.sent == [] and len(buys_csv()) == 1 and alerts_csv() == []


@pytest.mark.parametrize("code,ad", [("S", "D"), ("A", "A"), ("M", "A"), ("P", "D")])
def test_only_open_market_purchases_count(sec, tg, code, ad):
    it = item()
    sec.filing(it, form4_xml(code=code, ad=ad, shares=50000, price=50))
    assert monitor.handle_form4(it) is False
    assert buys_csv() == []


def test_planned_10b5_1_buy_is_low_and_never_pushed(sec, tg):
    it = item()
    sec.filing(it, form4_xml(shares=20000, price=30, after=40000, planned=True))
    monitor.handle_form4(it)
    assert tg.sent == [] and alerts_csv()[0]["priority"] == "LOW"


def test_amendment_is_labelled(sec, tg):
    it = item()
    sec.filing(it, form4_xml(shares=20000, price=30, after=40000, doc_type="4/A"))
    monitor.handle_form4(it)
    assert body_of(tg.texts[0]).startswith("AMENDED  INSIDER BUY")


def test_watchlist_name_is_high_whatever_the_size(sec, tg):
    it = item()
    sec.filing(it, form4_xml(owner="Buffett Warren E", shares=10, price=10, after=1000))
    monitor.handle_form4(it)
    msg = body_of(tg.texts[0])
    assert msg.startswith("WATCHLIST BUY") and "PRIORITY  HIGH  ★ WATCHLIST" in msg


def test_ten_million_dollar_buy_alerts_despite_small_holding_change(sec, tg):
    it = item()
    sec.filing(it, form4_xml(shares=200000, price=60, after=50_000_000))
    monitor.handle_form4(it)
    assert "PRIORITY  HIGH" in body_of(tg.texts[0])


def test_buy_without_a_ticker_is_never_pushed(sec, tg):
    it = item()
    sec.filing(it, form4_xml(ticker="NONE", shares=20000, price=30, after=40000))
    monitor.handle_form4(it)
    assert tg.sent == [] and alerts_csv()[0]["priority"] == "LOW"


def test_two_share_classes_in_the_ticker_field_use_the_first(sec, tg):
    it = item()
    sec.filing(it, form4_xml(ticker="HEI, HEI.A", shares=20000, price=30, after=40000))
    monitor.handle_form4(it)
    assert buys_csv()[0]["ticker"] == "HEI" and "TICKER    HEI\n" in body_of(tg.texts[0])


def test_unknown_holding_is_not_called_a_new_position(sec, tg):
    it = item()
    sec.filing(it, form4_xml(shares=20000, price=30, after=0))
    monitor.handle_form4(it)
    assert "HOLDING   not disclosed" in body_of(tg.texts[0])
    assert buys_csv()[0]["holding_change_pct"] == ""


def test_first_ever_purchase_is_a_new_position(sec, tg):
    it = item()
    sec.filing(it, form4_xml(shares=20000, price=30, after=20000))
    monitor.handle_form4(it)
    assert "HOLDING   new position" in body_of(tg.texts[0])
    assert buys_csv()[0]["holding_change_pct"] == "NEW"


def test_market_cap_floor_applies_only_when_a_key_is_set(sec, tg, monkeypatch):
    it = item()
    sec.filing(it, form4_xml(shares=20000, price=30, after=40000))
    monkeypatch.setattr(monitor, "FINNHUB_KEY", "k")
    sec.pages["finnhub.io"] = json.dumps({"marketCapitalization": 120})   # $120M
    assert monitor.handle_form4(it) is False and tg.sent == []
    assert not any("token=" in u or "k" == u for u in sec.fetched)        # key not in URL


# --------------------------------------------------------------- clusters

def buy(sec, n, owner, shares, price, ticker="ACME", after=None, **kw):
    it = item(n=n)
    sec.filing(it, form4_xml(owner=owner, shares=shares, price=price, ticker=ticker,
                             after=after if after is not None else shares * 50, **kw))
    return monitor.handle_form4(it)


def test_cluster_below_the_dollar_floor_stays_quiet_and_keeps_its_cooldown(sec, tg):
    assert buy(sec, 1, "Alpha Ann", 1000, 30) is False          # $30K
    assert buy(sec, 2, "Beta Bob", 1000, 30) is False           # cluster of 2, $60K
    assert tg.sent == []
    assert not monitor.CLUSTER_STATE_FILE.exists()              # cooldown untouched
    assert buy(sec, 3, "Gamma Gil", 7000, 30) is True           # lifts it to $270K
    msg = body_of(tg.texts[0])
    assert msg.startswith("INSIDER CLUSTER - 3 BUYERS") and "PRIORITY  HIGH" in msg
    assert "CLUSTER   3 insiders / 14d / $270K" in msg
    assert alerts_csv()[-1]["type"] == "INSIDER_CLUSTER"


def test_cluster_alerts_once_then_again_only_at_five_insiders(sec, tg):
    buy(sec, 1, "A One", 5000, 30)
    assert buy(sec, 2, "B Two", 5000, 30) is True               # $300K, fires
    assert buy(sec, 3, "C Three", 100, 30) is False             # cooldown
    assert buy(sec, 4, "D Four", 100, 30) is False
    assert buy(sec, 5, "E Five", 100, 30) is True               # milestone of 5
    assert [body_of(t).split("\n")[0] for t in tg.texts] == [
        "INSIDER CLUSTER - 2 BUYERS", "INSIDER CLUSTER - 5 BUYERS"]


def test_floor_of_zero_restores_the_old_behaviour(sec, tg, monkeypatch):
    monkeypatch.setattr(monitor, "CLUSTER_MIN_TOTAL", 0)
    buy(sec, 1, "A One", 100, 30)
    assert buy(sec, 2, "B Two", 100, 30) is True


def test_planned_buy_never_opens_a_cluster(sec, tg):
    buy(sec, 1, "A One", 5000, 30)
    assert buy(sec, 2, "B Two", 5000, 30, planned=True) is False
    assert not monitor.CLUSTER_STATE_FILE.exists()


def test_cluster_dropped_by_the_market_cap_floor_does_not_start_the_cooldown(sec, tg, monkeypatch):
    monkeypatch.setattr(monitor, "FINNHUB_KEY", "k")
    sec.pages["finnhub.io"] = json.dumps({"marketCapitalization": 120})
    buy(sec, 1, "A One", 5000, 30)
    assert buy(sec, 2, "B Two", 5000, 30) is False
    assert not monitor.CLUSTER_STATE_FILE.exists()


def test_same_person_buying_twice_is_not_a_cluster(sec, tg):
    buy(sec, 1, "A One", 5000, 30)
    assert buy(sec, 2, "A One", 5000, 30) is False


# --------------------------------------------------------------- Form 144

def form144(value, cik="0000111111"):
    return f"""<edgarSubmission><issuerInfo><issuerCik>{cik}</issuerCik>
      <issuerName>Acme Corp</issuerName>
      <nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>Doe Jane</nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>
      </issuerInfo><securitiesInformation><unitsToBeSold>500000</unitsToBeSold>
      <aggregateMarketValue>{value}</aggregateMarketValue>
      <approxSaleDate>11/20/2026</approxSaleDate><brokerName>Big Broker LLC</brokerName>
      </securitiesInformation></edgarSubmission>"""


def test_large_proposed_sale_alerts_with_the_ticker_found_from_the_cik(sec, tg):
    monitor.save_json(monitor.SEC_COMPANIES_FILE, [["111111", "ACME", "Acme Corp"]])
    monitor.save_json(monitor.SCHEDULE_FILE, {"sec_companies": NOW.isoformat()})
    it = item(form="144")
    sec.filing(it, form144(40_000_000), form_type="144")
    assert monitor.handle_form144(it) is True
    msg = body_of(tg.texts[0])
    assert msg.startswith("INSIDER SELL NOTICE - Form 144")
    assert "TICKER     ACME" in msg and "VALUE      $40.00M" in msg
    assert "Filed BEFORE the sale happens." in msg


def test_small_or_tickerless_proposed_sale_is_csv_only(sec, tg):
    monitor.save_json(monitor.SEC_COMPANIES_FILE, [["111111", "ACME", "Acme Corp"]])
    monitor.save_json(monitor.SCHEDULE_FILE, {"sec_companies": NOW.isoformat()})
    a, b = item(form="144", n=1), item(form="144", n=2)
    sec.filing(a, form144(5_000_000), form_type="144")
    sec.filing(b, form144(90_000_000, cik="0000999999"), form_type="144")
    monitor.handle_form144(a)
    monitor.handle_form144(b)
    assert tg.sent == []
    assert [r["priority"] for r in alerts_csv()] == ["LOW", "LOW"]


# ------------------------------------------------------------------ stakes

def stake_page(filer):
    return (f'<html><div>Filed by</div><span class="companyName">{filer} '
            f'(Filed by)</span></html>')


def test_watchlist_stake_alerts_high_with_the_stake_size(sec, tg):
    it = item(form="SC 13D", company="Target Inc", actual="SCHEDULE 13D")
    folder = it["link"][:it["link"].rfind("/")]
    sec.pages[it["link"]] = stake_page("Pershing Square Capital Management, L.P.")
    sec.pages[f"{folder}/index.json"] = json.dumps({"directory": {"item": [{"name": "sc13d.htm"}]}})
    sec.pages[f"{folder}/sc13d.htm"] = "<p>Percent of class represented by amount in row (11): 9.9%</p>"
    assert monitor.handle_stake(it) is True
    msg = body_of(tg.texts[0])
    assert msg.startswith("WATCHLIST STAKE") and "STAKE     9.9% of class" in msg
    assert "(activist)" in msg and "★ WATCHLIST" in msg


def test_first_time_stake_by_an_unknown_holder_is_logged_not_pushed(sec, tg):
    it = item(form="SC 13G", company="GoPro, Inc.", actual="SCHEDULE 13G")
    sec.pages[it["link"]] = stake_page("Quiet Holder Partners")
    assert monitor.handle_stake(it) is False
    assert tg.sent == []
    row = alerts_csv()[0]
    assert (row["type"], row["priority"]) == ("STAKE_13G", "LOW")
    assert "Quiet Holder Partners (not on watchlist)" in row["headline"]


def test_amendments_and_institutions_leave_no_row(sec, tg):
    a = item(form="SC 13G", n=1, actual="SCHEDULE 13G/A")
    b = item(form="SC 13G", n=2, actual="SCHEDULE 13G")
    sec.pages[a["link"]] = stake_page("Quiet Holder Partners")
    sec.pages[b["link"]] = stake_page("The Vanguard Group")
    assert monitor.handle_stake(a) is False and monitor.handle_stake(b) is False
    assert alerts_csv() == [] and tg.sent == []


def test_stake_page_that_will_not_load_is_retried_not_dropped(sec):
    with pytest.raises(monitor.FetchFailed):
        monitor.handle_stake(item(form="SC 13G"))


def test_parse_stake():
    assert monitor.parse_stake("<td>Percent of class represented: </td><td>8.5 %</td>") == 8.5
    assert monitor.parse_stake("holds 3.25% and later 6.75% of shares") == 6.75
    assert monitor.parse_stake("") == 0.0


# ----------------------------------------------------------------- tickers

@pytest.mark.parametrize("raw,want", [
    ("nvda", "NVDA"), (" HEI, HEI.A ", "HEI"), ("LEN, LEN.B", "LEN"),
    ("NYSE:FLG", "FLG"), ("BRK/B", "BRK/B"), ("BF.B", "BF.B"),
    ("N/A", ""), ("N.A.", ""), ("[NONE]", ""), ("none", ""), ("", ""), (None, ""),
    ("--", ""), ("TBD", ""),
])
def test_clean_ticker(raw, want):
    assert monitor.clean_ticker(raw) == want


def test_watchlist_matches_whole_names_only():
    assert monitor.is_notable("Buffett Warren E")
    assert monitor.is_notable("BERKSHIRE HATHAWAY INC")
    assert monitor.is_notable("Bill & Melinda Gates Foundation Trust")
    assert not monitor.is_notable("Edell Michael")           # contains "dell michael"
    assert not monitor.is_notable("Huang Jen-Chau")
    assert monitor.notable_in_text("<b>Filed by</b> Icahn Carl C") == "icahn"
    assert monitor.notable_in_text("1 Walton Street, Koch Road") == ""


def test_name_to_ticker_is_exact_and_drops_ambiguous_names(sandbox):
    monitor.save_json(monitor.SEC_COMPANIES_FILE, [
        ["1", "BKNG", "Booking Holdings Inc."], ["2", "GOOGL", "Alphabet Inc."],
        ["2", "GOOG", "Alphabet Inc."], ["3", "AAA", "Brown Corp"],
        ["4", "BBB", "Brown Inc"]])
    assert monitor.name_to_ticker("BOOKING HLDGS INC") == "BKNG"
    assert monitor.name_to_ticker("Alphabet Inc Class A") == "GOOGL"     # same company, first listing
    assert monitor.name_to_ticker("Brown") == ""                         # two companies share it
    assert monitor.name_to_ticker("Brown-Forman") == ""                  # never guessed
    assert monitor.ticker_for_cik("0000000001") == "BKNG"
    assert monitor.ticker_for_cik("not a number") == ""


def test_company_list_refresh_clock_restarts_only_after_a_good_download(sandbox, monkeypatch):
    monitor.save_json(monitor.SEC_COMPANIES_FILE, [["1", "OLD", "Old Co"]])
    monkeypatch.setattr(monitor, "fetch", lambda *a, **k: None)          # SEC down
    assert monitor.load_sec_companies() == [["1", "OLD", "Old Co"]]
    assert "sec_companies" not in monitor.load_json(monitor.SCHEDULE_FILE, {})
    monkeypatch.setattr(monitor, "_sec_rows", None)
    monkeypatch.setattr(monitor, "fetch", lambda *a, **k: json.dumps(
        {"0": {"cik_str": 7, "ticker": "new", "title": "New Co"}}))
    assert monitor.load_sec_companies() == [["7", "NEW", "New Co"]]
    assert "sec_companies" in monitor.load_json(monitor.SCHEDULE_FILE, {})
