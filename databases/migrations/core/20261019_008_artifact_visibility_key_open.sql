-- 008（1.1.15 权限模型补全）：制品公开/私有 + 钥匙级制品开放子集。
-- 模式 online_expand：加列带默认值不重写表；新表零存量数据，可在线执行。
-- 语义：
--   * kb_artifacts.visibility——private（默认，仅成员可见）/ public（本域全体
--     绑定用户可读）。public 只放大读可见面；写权限仍由 members 角色+owner 决定。
--   * mcp_key_open_artifacts——每把 MCP 钥匙显式勾选开放的制品（Agent 通道），
--     public 不会自动暴露给 Agent；开放行 ⊆ 钥匙主人可见面（读时校验）。
-- 与 017 的关系：017 启动链已含同构 DDL（fresh install 直达终态）；本迁移
-- 服务已应用 007 的存量库。

ALTER TABLE kb_artifacts
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private'
    CONSTRAINT ck_kb_artifacts_visibility CHECK (visibility IN ('private','public'));

CREATE TABLE IF NOT EXISTS mcp_key_open_artifacts (
    key_id      TEXT NOT NULL REFERENCES mcp_keys(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_key_open_artifacts_artifact
    ON mcp_key_open_artifacts(artifact_id);
