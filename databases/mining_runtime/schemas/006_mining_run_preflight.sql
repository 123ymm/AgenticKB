ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS preflight_manifest_json JSONB;

CREATE INDEX IF NOT EXISTS idx_mining_runs_preflight_id
    ON mining_runs ((preflight_manifest_json ->> 'preflight_id'))
    WHERE preflight_manifest_json IS NOT NULL;
