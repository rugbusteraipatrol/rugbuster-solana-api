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

RugCheck HTTP errors, 429 responses, timeouts, malformed JSON, or cache setup
failure return `UNKNOWN` with a null risk score and source
`live_scan_unavailable`. They never return GOOD.
