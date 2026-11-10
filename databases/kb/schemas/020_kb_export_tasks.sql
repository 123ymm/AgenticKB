-- 020 · KB 整库导出任务（打包下载通道，2026-10-26；原 018——2026-11-10 合并
-- fzl194/master 时与上游 wiki 的 018 撞号让位改号，存量库迁移 id 不变）.
--
-- copy_kb 有 MAX_COPY_DOCUMENTS=5000 上限，超限报错引导"导出/导入通道"——
-- 本表即该通道的任务面。POST /api/kb/{id}/export 秒回 202，后台 worker
-- 流式打包（原始文档 STORED zip + manifest.json 清单）经 put_stream 落
-- 对象存储 staging 桶，浏览器走 presign 直连下载。任务行照 013_kb_purge_tasks
-- 形制（status/phase/progress_json/error + lifespan 恢复），补产物列。
-- 产物是裸对象：不入 asset_storage_objects（不受引用计数/GC 波及），
-- 行保留 expired 终态供列表回看，产物对象由 sweep 定期清理。

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
