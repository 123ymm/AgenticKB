-- 019（66号批次1）：wiki 目录草稿表（fresh install 路径；与存量库版本化迁移
-- core/20261103_011_wiki_toc 同文，改表必须双改并刷新镜像校验和）。
-- 身份列故意无 FK（对齐 kb 表惯例：跨表身份用 TEXT 引用，不建强外键）。
CREATE TABLE IF NOT EXISTS kb_wiki_tocs (
    artifact_id TEXT PRIMARY KEY,
    toc         JSONB NOT NULL DEFAULT '{"sections":[]}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT ''
);
COMMENT ON TABLE kb_wiki_tocs IS 'wiki 制品设计态目录（page_key 级）；发布时冻结进 kb_artifact_revisions.schema_json.toc';
