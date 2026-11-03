-- 66号批次1：wiki 目录草稿持久化（online_expand，与启动链 019 同文）。
-- 设计依据：66号 §7 D03「确认目录必须成为本次生成和后续发布可读取的数据」。
-- 设计态目录按 artifact_id 单行保存（page_key 级结构进 toc jsonb）；
-- 发布时冻结进 kb_artifact_revisions.schema_json.toc，本表只承载草稿态。
-- 身份列故意无 FK（对齐 kb 表惯例：跨表身份用 TEXT 引用，不建强外键）。
CREATE TABLE IF NOT EXISTS kb_wiki_tocs (
    artifact_id TEXT PRIMARY KEY,
    toc         JSONB NOT NULL DEFAULT '{"sections":[]}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT ''
);
COMMENT ON TABLE kb_wiki_tocs IS 'wiki 制品设计态目录（page_key 级）；发布时冻结进 kb_artifact_revisions.schema_json.toc';
