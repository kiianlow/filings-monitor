# Decision log

Decisions that shape this project, so they are not re-argued or quietly reversed. Status: **Permanent** (changing it needs a new reason), **Review** (revisit when the stated condition is met), **Owner** (the owner's call; follow it).

---

### D1. Keep the architecture: GitHub Actions + repo as database + Apps Script clock
- **Problem:** the audit asked whether to retain, refactor, redesign or rebuild.
- **Alternatives:** a Python package refactor; a real database or object store; an always-on server.
- **Chosen because:** free, unattended, one user, deployed by pasting and merging. Every alternative adds cost or a moving part without fixing a measured problem.
- **Trade-offs:** about 290 state commits a day (history grows, 18.5 MB on 18 Nov); GitHub cron drift, covered by Kick.gs.
- **Status:** Permanent.

### D2. Free only; the repo stays public
- **Problem:** private repos get 2,000 Actions minutes a month; this uses far more.
- **Chosen because:** owner rule. **Trade-offs:** everything in the repo is public, so no secrets, chat id, emails or security reports here (D21).
- **Status:** Owner, permanent.

### D3. At-least-once delivery: retries plus a per-module outbox
- **Problem:** one failed send lost an alert.
- **Alternatives:** single attempt; a shared queue file.
- **Chosen because:** an undelivered message is saved to `state/outbox_<module>.json` (one writer each) and sent by the next run, marked "Delayed delivery". A Telegram read timeout is retried too: Telegram may have posted it, but a rare duplicate beats a lost alert.
- **Trade-offs:** possible duplicate after a slow Telegram reply. Outbox capped at 50, dropped after 48 h.
- **Status:** Permanent.

### D4. Mark an item seen only after it was handled
- **Problem:** SEC timeouts and PDF download failures marked items seen and lost them.
- **Chosen:** SEC filings go back on `state/queue.json` (3 tries); congress PDFs retry (6 tries); the congress alert cap and run budget leave the rest unseen.
- **Status:** Permanent.

### D5. Page through EDGAR's feed (up to 4 pages of 100)
- **Problem:** at the US close 60-70 filings arrive per 5 minutes; after a late run, older ones scrolled away. **Verified live** (Diagnostics `paging OK`).
- **Status:** Permanent.

### D6. Insider clusters need $250,000 of combined buying
- **Problem:** 188 cluster pings in 30 days, a quarter under $62K.
- **Chosen because:** replay of `data/buys.csv` gives 106 instead of 191. **Reverse:** `CLUSTER_MIN_TOTAL = 0`.
- **Status:** Owner (he will adjust it himself if he wants).

### D7. Halts: no `M`; market-wide codes; one key per level per day
- **Problem:** `M` was mislabelled and 65% of halt pings; real market-wide halts were not watched; a whole-market halt would overflow the 4,000-key seen list.
- **Chosen:** `ALERT_ON = T1, H10, MWC0-3, MWCQ`; market-wide rows keyed `MW|code|date`. **Reverse M:** add `"M"` to `ALERT_ON`.
- **Status:** Permanent (the feed's market-wide layout is unobserved since March 2020; the code does not depend on it).

### D8. Heartbeat limits 12 h (SEC feeds), 48 h (congress); a red run when Telegram refuses the bot
- **Problem:** 96 h limits hid a dead monitor for four days; a revoked token was invisible.
- **Chosen because:** `health.json` is stamped around the clock (weekends included). A red heartbeat run triggers GitHub's failed-workflow email, the one channel that still works when Telegram does not.
- **Status:** Permanent.

### D9. Kick.gs starts congress (every 30 min) and the Monday digests (07:00 SGT)
- **Problem:** cron alone ran congress about 3 times a day and the digests up to 3.5 h late.
- **Chosen:** Apps Script kicks; crons stay as backup; the digests are send-once per week (`sent_week`), so two triggers cannot double-send.
- **Status:** Permanent.

### D10. Telegram token in Script Properties; no trigger-deleting functions
- **Problem:** token in source; `setup()`/`deleteTriggers()` could wipe every trigger.
- **Status:** Permanent.

### D11. Download only from official hosts
- **Problem:** a third-party page decided which Trump PDFs were downloaded and reported.
- **Chosen:** Trump PDFs only from `https://*.oge.gov`; SEC links only to `https://www.sec.gov/`.
- **Status:** Permanent.

### D12. Exact dependency pins; GitHub Actions stay at v4/v5
- **Problem:** `>=` pins installed a new release on every run with the token in the environment.
- **Trade-offs:** v4/v5 run on Node 20 forced to Node 24 with a warning; they work. Pinning actions to commit SHAs was considered and left optional.
- **Status:** Owner rule for the action versions; Review when GitHub drops Node 20 forcing.

### D13. 13D/13G stake alerts stay watchlist-only; no market cap floor
- **Problem:** widening stakes risks noise; the cap floor needs a (free) Finnhub key, which is not set.
- **Chosen:** keep as is; first-time non-institutional filings are logged at LOW so the volume can be measured. **Reverse:** `STAKES_NOTABLE_ONLY = False`; set `FINNHUB_KEY`.
- **Status:** Review after about 30 days of LOW rows.

### D14. Congress parser keeps every row, strips the repeated page heading, records the asset type
- **Problem:** a merge step dropped 263 of 3,768 real trades; the page heading's "$200?" broke 11 amounts; options showed as stock.
- **Chosen:** verified on 411 real filings: every old trade kept or corrected. `asset_type` added as the last CSV column (header rewritten once, rows untouched). `[GS]`/`[CS]` never alert as stocks.
- **Status:** Permanent.

### D15. Checkout the branch tip at job start; abort a failed rebase
- **Problem:** a queued run started from the commit current when it was queued and could repeat alerts (Monday digests are triggered twice at 23:00 UTC).
- **Status:** Permanent.

### D16. Run budgets: stop starting new work at 10 min (monitor, job limit 20) and 14 min (congress, limit 20)
- **Problem:** a job killed at its limit saves nothing, so its alerts repeat.
- **Status:** Permanent.

### D17. Halts: one gentle retry on Nasdaq's bot-check page, cookies kept
- **Problem:** about 1 in 80 runs gets a challenge page. Three quick tries escalated to a harder block.
- **Trade-offs:** a blocked run delays the halt about 5 minutes (next run, different machine). Nothing is lost.
- **Status:** Review if the block rate rises.

### D18. Events digest: a feed counts only if it holds the digest's week
- **Problem:** falling back to next week's feed on a Monday sent the following week as "this week" and recorded it as sent.
- **Chosen:** otherwise a failed fetch; backups retry. A forced Sunday-afternoon run accepts the coming week.
- **Status:** Permanent.

### D19. Messages are cards in the OPTIONS bot's shape
- **Problem:** owner wanted one tidy look across SCOUT.
- **Alternatives:** keep the old box (tidied in PR #6); full OPTIONS copy including a coloured status emoji in the title.
- **Chosen:** bold title with the priority as a tag, subtitle, plaintext block at 32 wide / 11-char labels, optional Do line, italic note, short tappable link. **No emoji in titles** (owner had the green/red circles removed earlier) and the block stays `language-plaintext` (stops Telegram colouring numbers).
- **Trade-offs:** long values wrap more at 32 wide; event names in the events digest may continue on a second line (owner asked for one entry per event: day, date, time, event).
- **Status:** Owner; Review if the owner reports wrapping on his phone (`CARD_WIDTH`).

### D20. Halts history in its own file `data/halts.csv`
- **Problem:** the planned alert-value analysis needs halts; `alerts.csv` belongs to monitor.yml (one writer per file).
- **Chosen:** halts.yml writes `data/halts.csv` (skip LUDP/LUDS, rolling year).
- **Status:** Permanent.

### D21. Audit reports stay out of the public repo
- **Problem:** one is a security report.
- **Chosen:** kept in the owner's local `audit/` folder. **Status:** Permanent.

### D22. How changes reach the repo
- `gh` on the owner's Mac is **kiianlow** (read-only here). Branches go to the fork `kiianlow/filings-monitor` (Actions off), pull requests to `kiianwanted-byte/filings-monitor`; merges need the owner's account. Commits use the GitHub no-reply address. The owner has "Keep my email addresses private" on.
- **Status:** Permanent until the owner changes access.

### D23. Senate trades disabled
- **Problem:** the Senate site refuses GitHub's servers. **Status:** Permanent unless a free reachable source appears.

### D24. Kick token without expiry
- **Problem:** an expiring token adds a yearly chore that stops halts if missed. **Status:** Owner (decided 18 Nov 2026: keep non-expiring).

### D25. "OPTIONS KICK FAILED" warning left as it is
- **Problem:** it lives in the OPTIONS bot's block of the shared `Kick.gs`, which must stay byte-identical (parity test). **Status:** Review only together with the OPTIONS bot.
