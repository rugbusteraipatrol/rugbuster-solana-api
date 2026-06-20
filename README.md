# RugBuster Solana API

Standalone Solana risk score API backed first by the collector-owned
`solana_scans` table and, on a miss, one rate-limited live RugCheck lookup.

## Endpoints

```text
GET /
GET /health
GET /score?address=<SOLANA_MINT>
```

Collector hits remain the richest and highest-priority source. A collector miss
checks the service-owned `solana_live_cache` for a result newer than one hour,
then attempts one live lookup. RugCheck errors, malformed responses, timeouts,
and rate limits return `UNKNOWN`; they never become a false `GOOD` result.

The API never writes to or alters `solana_scans`. Its only writes are live
baseline results in the separate `solana_live_cache` table.

## Local development

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

Set `DATABASE_URL` only when running the service against Postgres. Offline tests
mock database and network access.

## Client skill

The companion
[RugBuster Solana Preflight Skill](https://github.com/rugbusteraipatrol/rugbuster-solana-preflight-skill)
consumes this API and maps scores to `ALLOW`, `WARN`, `BLOCK`, or
`UNAVAILABLE`. Once an integration adopts its optional Shield wrapper, the
wrapper enforces those decisions around the supplied action function. An
integration can still bypass Shield by calling separate action logic directly.
