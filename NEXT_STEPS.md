# Next steps

Nothing critical is open. Do not implement the optional items below unless the owner asks.

## 1. Watch (no code)

| When | Check | If wrong |
|---|---|---|
| Next alerts after 18 Nov | the card layout reads well on the owner's phone (no awkward wrapping) | lower or raise `CARD_WIDTH` in `monitor.py` (the OPTIONS bot uses 32) |
| Mon 23 Nov, 07:00 SGT | both digests arrive once each; `state/earnings_state.json` and `events_state.json` gain `sent_week` | read the earnings/events workflow logs and `kick_weekly:*` Script Properties |
| First US session after 18 Nov | halts alerts arrive; `data/halts.csv` appears | halts workflow log; bot-check lines are expected occasionally |
| Ongoing | heartbeat stays silent; no failed-workflow emails | Diagnostics, then `DEPLOYMENT.md` recovery table |

## 2. After about 30 days of data (owner decisions)

| Task | Depends on | Decision by |
|---|---|---|
| Review alert volume by type in `data/alerts.csv`; adjust `CLUSTER_MIN_TOTAL` if wanted | 30 days of alerts | owner (said he will adjust it himself) |
| Count first-time non-institutional 13D/13G rows logged at LOW; decide whether to widen stake alerts (`STAKES_NOTABLE_ONLY`) | 30 days of LOW rows | owner |

## 3. Optional improvements (suggested order)

1. **Alert-value analysis**: price move 1 day / 1 week / 1 month after each alert type, from `data/*.csv` and `data/halts.csv`. Needs a free price source. Medium effort. Depends on: a few months of data.
2. **Congress options detail**: strike and expiry from the filer's description line, shown on the card. Medium effort, parser change: replay real PTRs (see `tests/test_modules.py` fixtures) before shipping.
3. **Pin GitHub Actions to commit SHAs** (security finding S10, low). Small. Only if the owner agrees; his rule keeps v4/v5.
4. **Bump `actions/checkout` / `setup-python`** when GitHub stops forcing Node 24 on v4/v5, or if the owner lifts the version rule. Small; run Diagnostics after.
5. **Remove `box()` / `BOX_WIDTH`** once `fmtest` is no longer wanted (it is the only user). Small.

## 4. Awaiting the owner

| Item | Default today |
|---|---|
| Cluster floor | $250,000 (owner will change it himself) |
| Widen stake alerts | no |
| Finnhub key (market cap floor, earnings fallback) | not set |
| Pin actions to SHAs | no |
| Remove `kiianlow` as collaborator on `csp-scanner`; delete the fork `kiianlow/filings-monitor` | kept (useful for future work) |

## 5. Not possible / decided

Senate trades (blocked site), kick token expiry (owner: never expires), "OPTIONS KICK FAILED" restyle (inside the OPTIONS bot's protected block). See `DECISIONS.md`.
