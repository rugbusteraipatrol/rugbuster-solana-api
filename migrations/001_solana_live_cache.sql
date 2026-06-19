CREATE TABLE IF NOT EXISTS solana_live_cache (
  id BIGSERIAL PRIMARY KEY,
  contract_address TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'live_rugcheck',
  risk_score NUMERIC,
  label TEXT,
  rugcheck_score INTEGER,
  risk_flags JSONB,
  raw_response JSONB,
  token_name TEXT,
  token_symbol TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_solana_live_cache_address
  ON solana_live_cache (contract_address, created_at DESC);
