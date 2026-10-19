-- 存量库版本化迁移，与启动链 017 同文。
-- 制品挂 domain、与库权限解绑（策展开放）；行地址=业务主键 row_key（ordinal 仅展示）；
-- jsonb 三字段分工：data=业务值 / provenance=来源账 / conflicts=质量标记，永不混装。
-- 身份列故意无 FK（对齐 kb 表惯例：跨表身份用 TEXT 引用，不建强外键）。

CREATE TABLE IF NOT EXISTS kb_artifacts (
    artifact_id      TEXT PRIMARY KEY,
    domain_id        TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'table'
                     CONSTRAINT ck_kb_artifacts_kind CHECK (kind IN ('table','wiki')),
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    usage_note       TEXT NOT NULL DEFAULT '',
    owner_user_id    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft'
                     CONSTRAINT ck_kb_artifacts_status CHECK (status IN ('draft','published','deprecated')),
    current_revision INTEGER NOT NULL DEFAULT 0,
    draft_open       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_artifacts_domain ON kb_artifacts(domain_id);

CREATE TABLE IF NOT EXISTS kb_artifact_revisions (
    artifact_id    TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    schema_json    JSONB NOT NULL,
    row_count      INTEGER NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    published_by   TEXT NOT NULL,
    published_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id, revision)
);

CREATE TABLE IF NOT EXISTS kb_artifact_members (
    artifact_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'viewer'
                CONSTRAINT ck_kb_artifact_members_role CHECK (role IN ('owner','editor','viewer')),
    PRIMARY KEY (artifact_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_kb_artifact_members_user ON kb_artifact_members(user_id);

CREATE TABLE IF NOT EXISTS kb_artifact_batches (
    batch_id    TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'upload'
                CONSTRAINT ck_kb_artifact_batches_channel CHECK (channel IN ('upload','kb_import')),
    file_name   TEXT NOT NULL DEFAULT '',
    sheet_name  TEXT NOT NULL DEFAULT '',
    row_count   INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_artifact_batches_artifact ON kb_artifact_batches(artifact_id);

CREATE TABLE IF NOT EXISTS kb_table_columns (
    artifact_id    TEXT NOT NULL,
    column_id      TEXT NOT NULL,
    column_name    TEXT NOT NULL,
    column_type    TEXT NOT NULL DEFAULT 'text'
                   CONSTRAINT ck_kb_table_columns_type
                   CHECK (column_type IN ('text','int','float','date','enum','bool')),
    enum_values    JSONB,
    is_primary_key BOOLEAN NOT NULL DEFAULT FALSE,
    is_indexed     BOOLEAN NOT NULL DEFAULT FALSE,
    alias          JSONB NOT NULL DEFAULT '[]'::jsonb,
    status         TEXT NOT NULL DEFAULT 'active'
                   CONSTRAINT ck_kb_table_columns_status CHECK (status IN ('active','dropped')),
    ordinal        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (artifact_id, column_id)
);
-- active 列名唯一（对抗审查修复⑳，61号）：data 键=列名，重名会让 jsonb
-- 数据折叠到同一键；dropped 列释放名字（部分唯一索引）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_table_columns_name
    ON kb_table_columns(artifact_id, column_name) WHERE status='active';

CREATE TABLE IF NOT EXISTS kb_table_rows (
    artifact_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    row_key     TEXT NOT NULL,
    data        JSONB NOT NULL,
    provenance  JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicts   JSONB NOT NULL DEFAULT '{}'::jsonb,
    ordinal     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (artifact_id, revision, row_key)
);
