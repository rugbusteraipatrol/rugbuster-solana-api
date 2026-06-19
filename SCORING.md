# RugBuster Solana API Scoring

Phase 1 is cache-only. The API reads the latest `solana_scans` row and never
writes to Postgres.

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

Cache misses return `UNKNOWN` with a null risk score. They never return GOOD.
