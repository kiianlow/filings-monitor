# Changelog

Newest first. One entry per deploy. Each entry says what changed, why, how it was tested, and anything decided on the owner's behalf so it can be reversed.

## 2026-11-18 — audit and upgrade

Full audit of the six Python modules, the eight workflows and the Apps Script side. The architecture stays as it is: GitHub Actions for the work, the repo for state, Apps Script for the clock and the status card. The changes are fixes inside that design.

### Alerts that were being lost

| Change | Why |
|---|---|
| Telegram sends are retried, and a message that still cannot be delivered is saved to `state/outbox_<module>.json` and sent by the next run with a "Delayed delivery" line | A send was tried once. If Telegram was slow or rate limited the alert was gone, because the filing or halt was already marked seen. Nothing reported it |
| A filing whose documents fail to download goes back on the queue, up to 3 tries | One SEC timeout marked the filing seen for good |
| Discovery reads further pages of EDGAR's feed when a whole page is new, up to 4 | The feed shows only the newest 100 filings per form. At the US close 60 to 70 arrive every five minutes, and the longest gap between runs in 45 days was 81 minutes, so older filings had scrolled away unread |
| A congress PDF that fails to download is retried on later runs, up to 6 | It was reported at once as "open manually" and the trades in it were never read |
| When congress reaches its 15-alert cap it stops and leaves the rest for the next run | It carried on, marking the remaining filings seen with their alerts silently skipped |
| In January the previous year's House index is read too | Filings from the last days of December were missed, and until the new year's file existed every run failed |

### Alerts that were wrong

| Change | Why |
|---|---|
| Halt code `M` no longer alerts. `MWC0` to `MWC3` and `MWCQ` now do, as one message | `M` was labelled "market wide". Nasdaq defines it as a volatility pause in one exchange-listed stock, the same event as the `LUDP` pauses that were excluded as noise. It was 389 of the 601 alertable halts on record, about 7 a day. The real market-wide circuit breaker codes were not watched at all |
| Trump ticker resolution no longer accepts a near prefix | `SNAP INC CLASS A` was reported as SNA (Snap-on) and `IDEXX LABS INC` as IEX (IDEX Corp) |
| Events digest decides "all day" in the feed's own time zone | An event at 11:00 New York in winter is 00:00 Singapore and was printed as "all day" with its time dropped |
| Congress no longer filters out listed partnerships | "L.P." and "Partners" in the name blocked 13 real trades: Brookfield Renewable (BEP), Energy Transfer (ET), Enterprise Products (EPD), Perella Weinberg (PWP) and others |
| Two share classes in one ticker field use the first (`HEI, HEI.A` becomes `HEI`) | The whole string was treated as a ticker, so those buys never clustered and the alert showed both |
| A Form 4 that does not say what is held after the trade shows `HOLDING not disclosed` | It showed "new position" |
| Earnings watchlist: `SQ` replaced by `XYZ` | Block Inc changed ticker in January 2025, so its results were never flagged |
| Halt times read `09:41 ET 17 Nov (22:41 SGT)` | The feed's New York time was shown bare, which reads as Singapore time |

### Noise

| Change | Why |
|---|---|
| Insider clusters need $250,000 of combined buying (`CLUSTER_MIN_TOTAL`) | Clusters had no dollar floor and were the largest source of pings: 188 in 30 days, all HIGH, a quarter of them totalling under $62,000 |
| Monday digests are sent once per week however often the job runs | They are now started by `Kick.gs` at 07:00 Singapore as well as by cron, with two backup cron times |
| A digest source failure is announced once, then retried quietly | Each backup run would have repeated the failure message |

### Problems that went unreported

| Change | Why |
|---|---|
| SEC feed staleness limit 96 hours to 12; congress 96 to 48 | EDGAR's feed answers around the clock, weekends included, so 96 hours meant a dead filings monitor went unreported for four days |
| The heartbeat asks Telegram whether the bot can still see the channel, and its run goes red if not, or if its warning could not be delivered | A revoked token or a removed bot was invisible: every send failed quietly and every run stayed green |
| The heartbeat reports alerts stuck in an outbox for more than an hour | New with the outbox |
| Trump: falling back to the built-in seed list is stamped as a failure | It was stamped as a success, so if the one working index went away, new filings would stop and nothing would say so |
| Repeatedly unreadable SEC filings are reported after three runs in a row | New with the retry queue |

### Security

| Change | Why |
|---|---|
| Credentials are removed from every log line before printing | A network error message contains the request URL, and the Telegram URL contains the bot token. This repo's logs are public. GitHub's own masking was the only protection |
| The Finnhub key is sent in a header, not the URL | Same reason. Not exploitable today, the key is not set |
| Dependencies pinned to exact versions | Every run installed whatever was newest. The pins are the versions the live runs were already using |

### Housekeeping

- State files are written to a temporary file and swapped in, so a killed run cannot leave a truncated file.
- `congress.py` and `trump.py` keep their seen lists in order, so trimming drops the oldest entries and not random ones.
- NYSE holidays are computed from the exchange's rules. The typed list ended in 2027.
- The weekly SEC company list refresh restarts its clock only after a download that worked.
- First-time 13D and 13G filings by non-institutional holders who are not on the watchlist are written to `alerts.csv` at LOW. They are still not pushed. This gives real numbers for deciding whether to widen stake alerts.
- Removed: the 8-K and late-filing handlers (unused since mid September), unused imports and variables. `pyflakes` reports nothing (19 warnings before).
- Diagnostics show real US Eastern time. They assumed UTC-4 all year.
- `heartbeat.yml` and `diagnostics.yml` have timeouts. `events.yml` uses the same five-try state push as the others.
- Added: `tests/` (244 tests), the Tests workflow, `README.md`, `DEPLOYMENT.md`, `TEST_PLAN.md`, `.env.example`, `.gitignore`.

### Tested

`pyflakes` clean. 244 of 244 tests pass on Python 3.11. Every module run end to end with the network blocked. Telegram payloads byte-identical to the previous version for 7 message types. Trump tickers compared old and new over all 835 logged rows: 28 changed, all corrections or newly resolved. Cluster rule replayed over `data/buys.csv`: 191 alerts in 30 days become 106. Details in `TEST_PLAN.md`.

Not tested here: anything needing the live SEC, House, OGE or Telegram. Run **Diagnostics** after deploying.

### Decided on the owner's behalf, and how to reverse each

| Decision | To reverse |
|---|---|
| Cluster floor of $250,000 | `CLUSTER_MIN_TOTAL = 0` in `monitor.py` |
| Halt code `M` stops alerting | add `"M"` to `ALERT_ON` in `halts.py` |
| Stake alerts stay watchlist-only | `STAKES_NOTABLE_ONLY = False` in `monitor.py` |
| No market cap filter (no Finnhub key) | add a `FINNHUB_KEY` secret. Note it would hide insider buys in companies under $300M |
| SEC staleness limit of 12 hours | `FORMS_STALE_HOURS` in `monitor.py` |

### Not changed

`state/`, `data/`, the message layout, the `CONGRESS` header, the watchlists (apart from `SQ`), every alert threshold other than the cluster floor, the workflow file names, `ubuntu-24.04`, Python 3.11, the action versions.

## Earlier

Before this entry the repo had no changelog. Changes from August to 11 November 2026 are recorded by date in the project's technical reference.
