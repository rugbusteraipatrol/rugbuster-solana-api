# RugBuster Solana API Scoring

Collector data always has priority. The API only reads `solana_scans`; it never
writes to or alters that collector-owned table.

## Priority

1. If `full_record.risk_percent` is numeric, use it unchanged (clamped 0-100).
2. Otherwise calibrate `rugcheck_score` into a 0-100 risk percentage.
3. Apply structured evidence boosts when the precomputed percentage is absent.
4. If no numeric evidence exists, map the cached label. Unknown or malformed
   evidence becomes 55/WARN, never GOOD.

## RugCheck calibration

| Raw RugCheck score | Risk range |
|---|---|
| below 100 | 5-15 |
| 100-1,999 | 30-50 |
| 2,000-4,999 | 50-65 |
| 5,000-11,999 | 65-78 |
| 12,000-71,999 | 78-90 |
| 72,000+ | 90-98 |

Linear interpolation is used within each range.

## Evidence boosts

Boosts apply only when `risk_percent` is missing, because a present
`risk_percent` was already calibrated by the collector.

- Creator rug rate >=80%: minimum risk 85; `creator_rug_rate_high`.
- Creator rug rate >=40%: minimum risk 70; `creator_rug_rate_elevated`.
- Fake LP lock: +15, capped at 98; `fake_lp_lock`.
- LP lock between 0% and 50%: +10, capped at 98; `short_lp_lock`.
- Sniped launch: +10, capped at 98; `sniped_in_<N>ms` or
  `sniped_at_launch`.

## Label fallback

- GOOD -> 15
- WARN -> 55
- DANGER -> 90
- Missing/unknown/malformed -> 55 (WARN)

## Final label

- risk below 35 -> GOOD
- risk 35-69 -> WARN
- risk 70 or above -> DANGER

## Live baseline for collector misses

If `solana_scans` has no row, the API checks its separate one-hour
`solana_live_cache`, then makes at most one serialized RugCheck request. Live
results are a one-shot baseline, not equivalent to collector-enriched evidence.

The live baseline starts with RugCheck `score_normalised` (or the calibrated raw
score when normalized score is absent), then applies:

- Active mint authority: +10; `mint_authority_active`.
- Active freeze authority: +10; `freeze_authority_active`.
- Both authorities active: minimum risk 50.
- Mutable metadata: +5; `mutable_metadata`.
- RugCheck `rugged: true`: force risk 98/DANGER;
  `rugcheck_flagged_rugged`.

Collector records may include creator rug history, launch sniping, funding-chain
analysis, and wallet clustering accumulated over time. A single live RugCheck
report cannot reproduce those richer signals, so its source is explicitly
`live_rugcheck` or `live_cache`.

## Deployer history across a refresh (2026.09.10)

When a stored collector row is too old to serve and the token is refreshed from
a live report, the row's deployer history is not discarded with it. The record's
`creator`, `v6_serial_rug_count`, `creator_rug_rate` and `cia_funding_hops` are
carried into the response as `deployer_history` (with `source:
collector_record` and the date the row was written) and the same floors the
stored path applies are applied to the live score, on both refresh branches:

- Creator rug rate >=80%: minimum risk 85; `creator_rug_rate_high`.
- Creator rug rate >=40%: minimum risk 70; `creator_rug_rate_elevated`.
- One or more earlier tokens by this creator on record as rugged: minimum
  risk 70; `creator_history_of_rugged_tokens`.

`prior_rugs_on_record` is `v6_serial_rug_count` minus one when the row itself
is DANGER: the collector increments the creator's count for the token being
scanned before reporting it, so a row reading count=1, DANGER says "this token"
and nothing about an earlier one. A verdict may not cite itself as its own
history. `creator_rug_rate` is computed before the label and is used as is.
`cia_funding_hops` is carried for the reader and does not move the score: the
collector's own calibration weighs it, and this service does not know its
threshold.

These are floors, never caps. They count as independent serious signals, so
they may carry a verdict past the WARN ceiling that disclosures alone cannot.
The evidence dimension `creator_history` reports the record as collected; a
count of zero is reported as PARTIAL coverage, not as clearance. A row that
names no creator and carries no count yields no history block, and the
dimension stays NOT_COLLECTED.

RugCheck HTTP errors, 429 responses, timeouts, malformed JSON, or cache setup
failure return `UNKNOWN` with a null risk score and source
`live_scan_unavailable`. They never return GOOD.
