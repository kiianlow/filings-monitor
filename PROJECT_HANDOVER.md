# Project handover

The story of this project up to 18 Nov 2026: what it was for, what it looked like, what the November 2026 audit found, and what changed. For the state today see `CURRENT_STATE.md`; for the reasons behind each choice see `DECISIONS.md`.

## Objectives

- Know about a market-moving public disclosure as soon as it appears, on a phone, without watching anything.
- Never be caught by a scheduled event (earnings, economic data) while holding a position.
- Free to run, unattended, for one person. Telegram is the only interface.
- Few, meaningful pings: everything buzzes, so noise is a real cost.

## The original implementation (before 18 Nov 2026)

Built up from August 2026 in a series of chat-assisted releases.

- **Python on GitHub Actions** in this public repo: `monitor.py` (EDGAR Form 4, 144, 13D/13G, an unused 8-K/NT handler, Fear & Greed, heartbeat), `halts.py` (moved off Apps Script on 17 Sep 2026 because Apps Script cannot time out a stalled fetch), `congress.py`, `trump.py`, `earnings.py`, `events.py`. State and history committed to the repo by every run.
- **Google Apps Script** project "Filings monitor": `Kick.gs` (a 5-minute clock that dispatches the workflows, because GitHub cron drifts badly; it also kicks the separate OPTIONS bot `csp-scanner`), `Status.gs` (the SCOUT STATUS card and a stamp endpoint used by SNKRS, TCG and CSP), `Core.gs`, `Config.gs` (which held the bot token in source), `Setup.gs`, and a dead `Halts.gs`.
- **Messages**: a plain title, a monospace box with UPPERCASE labels (`PRIORITY HIGH`, `TICKER ABC`), a footer and a raw URL.
- No tests, unpinned dependencies.

## What the audit found (18 Nov 2026)

Nine high-severity findings, no critical. One root cause ran through most of them: **an item was marked "handled" before it was safely read and delivered.**

| Area | Finding |
|---|---|
| Lost alerts | one failed Telegram send lost the alert; one SEC timeout dropped a filing for good; a congress PDF that failed to download was never read |
| Missed filings | EDGAR's feed shows the newest 100 per form; after a late run, older filings scrolled away unread |
| Wrong alerts | halt code `M` was labelled "market wide" (it is a single-stock pause, 65% of halt pings) and real market-wide halts were not watched; two Trump tickers were wrong (SNAP shown as SNA, IDEXX as IEX); listed partnerships were filtered out of congress |
| Silent failure | a dead filings monitor took 4 days to report; a revoked token would never be reported |
| Noise | 188 insider-cluster pings in 30 days, a quarter under $62K in total |
| Security | the bot token could reach the public Actions log in an error message; a third-party page decided which PDFs were downloaded; the token sat in Apps Script source |
| Safety | `setup()` and `deleteTriggers()` sat one click from deleting every trigger |

Later the same day a second pass on real House filings found the congress parser dropped 263 of 3,768 real trades and misread amounts at page breaks; two independent code reviews then found five more issues (duplicate alerts from runs that started on stale state, a market-wide halt overflowing the seen list, job time limits, a wrong-week events fallback, and smaller ones). All fixed.

## What changed, and why

Delivered as seven pull requests, all merged on 18 Nov 2026, plus pasted Apps Script files.

| PR | What | Why |
|---|---|---|
| #1 | Telegram retries + per-module outbox; SEC retry queue; EDGAR feed paging (up to 4 pages); cluster $250K floor; halt codes fixed and market-wide halts added; Trump ticker resolver rewritten; congress PDF retries, alert cap, year rollover, partnership fix; digests sent once per week; heartbeat limits 12 h / 48 h and a Telegram check; log scrubbing; official-host downloads only; exact pins; 246 tests; docs. Second pass: congress keeps every trade, page-break fix, option labels and `asset_type` column, `data/halts.csv` | the audit findings above |
| #2, #4 | halts: one gentle retry when Nasdaq's bot filter answers instead of the feed | 18 of 1,500 runs were blocked; fast retries made it worse (#4 tuned #2) |
| #3 | checkout of the branch tip in every state-writing workflow; one seen key per market-wide halt level per day; congress next-row ticker fix; run budgets; events week check | first independent review |
| #5 | forced Sunday events digest; monitor job limit 20 min; market-wide history labels | second independent review |
| #6 | phone-width wrapping, consistent titles and dates | owner asked for a tidier look |
| #7 | every message redesigned as a card in the OPTIONS bot's shape | owner asked for the same look as the OPTIONS bot in SCOUT |

Apps Script (pasted by the owner, deployed as Version 9 then Version 10): `Kick.gs` also kicks congress every 30 minutes and the Monday digests at 07:00 SGT, and stops warning on brief GitHub 5xx errors; `Status.gs` gains a KICK row, a 12-hour FILINGS limit, a fail-closed stamp endpoint and the card layout; the token moved to Script Properties; `Setup.gs` can no longer delete triggers (`checkTriggers`, `repairTriggers`, `testTelegram` instead); `Halts.gs` deleted.

The same day, the OPTIONS bot (`csp-scanner`, private, separate repo) got a cosmetic v7.8 message tidy; see that repo's CHANGELOG.

## Removed or replaced

| Removed | Replaced by / reason |
|---|---|
| 8-K / NT handlers and helpers in `monitor.py` | dead code, never reachable |
| `Halts.gs`, `setup()`, `deleteTriggers()` in Apps Script | halts live in Python since Sep 2026; the two functions could delete every trigger |
| Halt code `M` alerts | it is a single-stock pause; replaced by the real market-wide codes MWC0-3, MWCQ |
| Congress row de-duplication | it only ever dropped real trades |
| The `box()` message layout | `card()`; `box()` is kept only for `python monitor.py fmtest` |
| Token in `Config.gs` | Script Property `TELEGRAM_TOKEN` |

## Current functionality

See the module table in `README.md` and the verified/unverified split in `CURRENT_STATE.md`.

## Architecture

Unchanged in shape: GitHub Actions does the work, the repo stores state and history, Apps Script is the clock and the status card, Telegram is the interface. It was reassessed against a package refactor, a different storage design and an always-on server, and kept: it is the right answer for "free, unattended, one user, deployed by pasting". Details in `CLAUDE.md`.

## Outstanding

Nothing broken is known. Open items, all optional or awaiting time and data, are in `NEXT_STEPS.md`. Accepted limitations are in `CURRENT_STATE.md`.

## Future opportunities

- Measure the price move after each alert type (needs a free price source and a few months of `data/` including `data/halts.csv`) to learn which alerts deserve a buzz.
- Widen 13D/13G stake alerts beyond the watchlist once the logged LOW rows show the volume.
- Congress: options detail (strike, expiry) from the filer's description line.

## Where the rest lives (owner's Mac, not in this repo)

`~/Desktop/Claude Audits/CONGRESS/`: `audit/` (the ten audit reports, including the security audit, kept private), `apps-script/` (original and current Apps Script files, Node tests), `handback/` (owner steps, the full technical reference `SCOUT_Filings_Monitor_TECHNICAL_EXPANDED.md`, paste-ready Apps Script files, rollback copies).
