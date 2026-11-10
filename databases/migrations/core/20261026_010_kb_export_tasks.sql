-- 存量库版本化迁移，与启动链 018 同文。
-- KB 整库导出任务表（打包下载通道）：纯 CREATE IF NOT EXISTS 幂等增量。
-- 身份列故意无 FK（对齐 kb 表惯例：跨表身份用 TEXT 引用，不建强外键）。

CREATE TABLE IF NOT EXISTS kb_export_tasks (
    id            TEXT PRIMARY KEY,
    kb_id         TEXT NOT NULL,
    kb_name       TEXT NOT NULL,
    domain        TEXT NOT NULL,
    status        TEXT NOT NULL
                  CONSTRAINT ck_kb_export_tasks_status
                  CHECK (status IN ('queued', 'running', 'cancelling',
                                    'succeeded', 'failed', 'cancelled', 'expired')),
    phase         TEXT NOT NULL DEFAULT 'queued',
    progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    requested_by  TEXT NOT NULL,
    error         TEXT,
    -- 产物（succeeded 时非空）
    artifact_bucket     TEXT,
    artifact_object_key TEXT,
    artifact_size       BIGINT,
    artifact_sha256     TEXT,
    doc_count           INTEGER,
    expires_at          TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_export_tasks_domain
    ON kb_export_tasks(domain, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_export_tasks_kb
    ON kb_export_tasks(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_export_tasks_expiry
    ON kb_export_tasks(expires_at) WHERE status = 'succeeded';
