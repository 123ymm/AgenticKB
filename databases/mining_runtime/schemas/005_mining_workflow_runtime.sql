ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS workflow_id TEXT;
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS workflow_version INTEGER;
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS workflow_version_id TEXT;
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS workflow_graph_hash TEXT;
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS workflow_manifest_json JSONB;
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS execution_engine TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS active_node_id TEXT;
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS active_operator_type TEXT;
ALTER TABLE mining_runs ADD COLUMN IF NOT EXISTS pause_step TEXT;

DO $$
BEGIN
    ALTER TABLE mining_runs
        ADD CONSTRAINT ck_mining_runs_execution_engine
        CHECK (execution_engine IN ('legacy', 'workflow'));
EXCEPTION WHEN duplicate_object THEN
    NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE mining_runs
        ADD CONSTRAINT ck_mining_runs_workflow_version
        CHECK (workflow_version IS NULL OR workflow_version > 0);
EXCEPTION WHEN duplicate_object THEN
    NULL;
END
$$;

CREATE TABLE IF NOT EXISTS mining_workflow_node_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES mining_runs(id) ON DELETE CASCADE,
    run_document_id TEXT REFERENCES mining_run_documents(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    operator_type TEXT NOT NULL,
    operator_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'started', 'completed', 'failed', 'skipped', 'paused',
            'fallback', 'not_applicable'
        )
    ),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    input_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT,
    error_message TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mining_workflow_node_events_attempt
    ON mining_workflow_node_events(
        run_id,
        node_id,
        COALESCE(run_document_id, '__global__'),
        attempt_no
    );

CREATE INDEX IF NOT EXISTS idx_mining_runs_workflow
    ON mining_runs(workflow_id, workflow_version);

CREATE INDEX IF NOT EXISTS idx_mining_workflow_node_events_run
    ON mining_workflow_node_events(run_id, started_at, node_id);
