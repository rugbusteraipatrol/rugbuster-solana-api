# RugBuster Solana API

Standalone, read-only Solana risk score API backed by the existing
`solana_scans` Postgres table.

## Endpoints

```text
GET /
GET /health
GET /score?address=<SOLANA_MINT>
```

Phase 1 is cache-only. Unknown mints return `UNKNOWN` and are explicitly marked
unverified. The service never writes to Postgres.

## Local development

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

Set `DATABASE_URL` only when running the service against Postgres. Offline tests
mock database reads.
