-- 021 · 知识制品工作台 7 表（67号工作台 MVP Task 1，fresh install 启动链路径；
-- 与存量库版本化迁移 core/20261116_012_kb_workspaces 同文——改表必须双改，
-- 漂移由 tests/maintenance/test_schema_mirror_sync.py 钉死）。
--
-- 语义与决策依据：67号规划 §4.1——登记（workspaces+members）/来源（sources：
-- kb_dir、excel 两连接器，input_manifest 冻结输入）/运行（runs：graph_snapshot_json
-- 冻结图+七态状态机）/输出（run_outputs：is_current 单活指针+部分唯一索引）/
-- 绑定（bindings：port→artifact 导出去处）/导出（exports：request_id 幂等键）。
-- 时间戳口径=TEXT ISO（同 002/020 任务记录族；017 是 TIMESTAMPTZ 勿抄）。
-- actor_kind/actor_id 三表落位：Agent 演进从第一张表就带 actor 字段（67号 §0）。

CREATE TABLE IF NOT EXISTS kb_workspaces (
    id            TEXT PRIMARY KEY,           -- wsp_<16hex>，应用生成
    domain_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CONSTRAINT ck_kb_workspaces_status
                  CHECK (status IN ('active', 'archived')),
    graph_json     JSONB NOT NULL DEFAULT '{"nodes":[]}'::jsonb,  -- 步骤流图（nodes/ports，设计态草稿）
    graph_revision INTEGER NOT NULL DEFAULT 0,                   -- 乐观锁版本号（PUT expected token）
    batch_no       INTEGER NOT NULL DEFAULT 1,                   -- 导出编号计数器（t001 起）
    actor_kind     TEXT NOT NULL DEFAULT 'user'
                   CONSTRAINT ck_kb_workspaces_actor_kind
                   CHECK (actor_kind IN ('user', 'mcp_key')),
    actor_id    TEXT NOT NULL DEFAULT '',     -- actor_kind='mcp_key' 时为钥匙 id
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,                -- TEXT ISO（同 002/020 口径）
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_workspaces_domain ON kb_workspaces(domain_id);
CREATE INDEX IF NOT EXISTS idx_ws_workspaces_owner ON kb_workspaces(owner_user_id);

CREATE TABLE IF NOT EXISTS kb_workspace_members (
    workspace_id TEXT NOT NULL REFERENCES kb_workspaces(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL,
    role         TEXT NOT NULL
                  CONSTRAINT ck_kb_workspace_members_role
                  CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS kb_workspace_sources (
    id            TEXT PRIMARY KEY,           -- wss_<16hex>
    workspace_id  TEXT NOT NULL REFERENCES kb_workspaces(id) ON DELETE CASCADE,
    node_id       TEXT NOT NULL,
    connector     TEXT NOT NULL
                  CONSTRAINT ck_kb_workspace_sources_connector
                  CHECK (connector IN ('kb_dir', 'excel')),
    config         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- kb_dir:{kb_id,directories[]}; excel:{file_name,sheet_name}
    input_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 冻结输入：kb_dir=文件清单+计数; excel=解析后整份数据集（受护栏约束）
    last_read_at TEXT,
    created_by   TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CONSTRAINT uq_ws_sources_node UNIQUE (workspace_id, node_id)
);

CREATE TABLE IF NOT EXISTS kb_workspace_runs (
    id            TEXT PRIMARY KEY,           -- wsr_<16hex>
    workspace_id  TEXT NOT NULL REFERENCES kb_workspaces(id) ON DELETE CASCADE,
    scope         TEXT NOT NULL
                  CONSTRAINT ck_kb_workspace_runs_scope
                  CHECK (scope IN ('node', 'workspace', 'subgraph')),  -- subgraph 预留；trial 不建 run
    target_node_id      TEXT,
    graph_revision      INTEGER NOT NULL,
    graph_hash          TEXT NOT NULL,
    graph_snapshot_json JSONB NOT NULL,       -- 冻结图（含全部节点配置）——重启可判定的根
    batch_no            INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CONSTRAINT ck_kb_workspace_runs_status
                        CHECK (status IN ('queued', 'running', 'awaiting_review',
                                          'completed', 'partial', 'failed', 'cancelled')),
    progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {done:[node_id], total:N, current_node, failed_rows:{node:[]}}
    error         TEXT,
    actor_kind    TEXT NOT NULL DEFAULT 'user'
                  CONSTRAINT ck_kb_workspace_runs_actor_kind
                  CHECK (actor_kind IN ('user', 'mcp_key')),
    actor_id    TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL, created_at TEXT NOT NULL,
    started_at  TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ws_runs_ws ON kb_workspace_runs(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS kb_workspace_run_outputs (
    run_id        TEXT NOT NULL REFERENCES kb_workspace_runs(id) ON DELETE CASCADE,
    workspace_id  TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    port_id       TEXT NOT NULL,
    kind          TEXT NOT NULL
                  CONSTRAINT ck_kb_workspace_run_outputs_kind
                  CHECK (kind IN ('table', 'documents', 'relations')),
    content_inline JSONB,
    row_count      INTEGER NOT NULL DEFAULT 0,
    content_hash   TEXT NOT NULL DEFAULT '',
    quality_json   JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {input_rows,filtered,failed_row_ids,...} 机器可读
    is_current     BOOLEAN NOT NULL DEFAULT FALSE,
    stale_since_run TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id, port_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ws_output_current
    ON kb_workspace_run_outputs(workspace_id, node_id, port_id) WHERE is_current;

CREATE TABLE IF NOT EXISTS kb_workspace_output_bindings (
    workspace_id     TEXT NOT NULL,
    node_id          TEXT NOT NULL,
    port_id          TEXT NOT NULL,
    artifact_id      TEXT NOT NULL,
    artifact_kind    TEXT NOT NULL DEFAULT 'table',
    last_run_id      TEXT,
    last_exported_at TEXT,
    created_by       TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, node_id, port_id)
);

CREATE TABLE IF NOT EXISTS kb_workspace_exports (
    id                 TEXT PRIMARY KEY,      -- wse_<16hex>
    request_id         TEXT NOT NULL
                       CONSTRAINT uq_ws_exports_request UNIQUE,  -- 幂等键
    workspace_id       TEXT NOT NULL,
    node_id            TEXT NOT NULL, port_id TEXT NOT NULL,
    mode               TEXT NOT NULL DEFAULT 'create_draft'
                       CONSTRAINT ck_kb_workspace_exports_mode
                       CHECK (mode IN ('create_draft')),
    target_artifact_id TEXT,
    snapshot_run_id    TEXT,
    mapping_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CONSTRAINT ck_kb_workspace_exports_status
                       CHECK (status IN ('pending', 'succeeded', 'failed')),
    result_json JSONB, error TEXT,
    actor_kind  TEXT NOT NULL DEFAULT 'user'
                CONSTRAINT ck_kb_workspace_exports_actor_kind
                CHECK (actor_kind IN ('user', 'mcp_key')),
    actor_id    TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL, created_at TEXT NOT NULL
);
