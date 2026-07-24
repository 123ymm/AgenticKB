CREATE TABLE IF NOT EXISTS mining_workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    draft_graph_json JSONB NOT NULL,
    draft_revision INTEGER NOT NULL DEFAULT 0 CHECK (draft_revision >= 0),
    current_version INTEGER CHECK (current_version IS NULL OR current_version > 0),
    is_system BOOLEAN NOT NULL DEFAULT FALSE,
    is_system_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT,
    updated_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mining_workflows_system_default
    ON mining_workflows (is_system_default)
    WHERE is_system_default = TRUE;

CREATE TABLE IF NOT EXISTS mining_workflow_versions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES mining_workflows(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL CHECK (version > 0),
    graph_json JSONB NOT NULL,
    compiled_manifest_json JSONB NOT NULL,
    graph_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    operator_catalog_version TEXT NOT NULL,
    release_notes TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (workflow_id, version)
);

CREATE INDEX IF NOT EXISTS idx_mining_workflows_status
    ON mining_workflows(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_mining_workflow_versions_workflow
    ON mining_workflow_versions(workflow_id, version DESC);
