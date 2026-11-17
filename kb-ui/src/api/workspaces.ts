/**
 * 知识制品工作台 API（67号 Task 10）—— 经 main_control 反向代理到 mining 服务。
 *
 * 走 createProxyClient('mining')：baseURL 每次请求按当前域重算
 * （/api/control-plane/api/v1/proxy/{domain}/mining）。工作区路由与制品
 * 同款在路由层自解析域（list 的 domain_id 查询参数），故关掉自动 domain
 * 追加。错误文案统一由调用方用 apiErrorDetail 提取（404/403/409 带
 * {code, message} 信封——反探测口径：不存在与无权同响应）。
 *
 * 契约权威 = mining/kb/routes/workspaces.py（22 端点：CRUD 5 + 成员 4 +
 * 图 2 + 来源 4 + 运行 4 + 输出试跑 2 + 导出 1）与
 * workspace_service.py（响应形态）：
 * - 列表/详情行：db_workspaces._WS_COLUMNS（列表不带 graph_json——节点数
 *   需读图才知道，列表页不显示该列）。
 * - 图整图替换：PUT 带 expected_revision（graph_revision 整数令牌，失配
 *   409 graph_conflict）；服务端归一节点并回写 pending/stale（客户端写的
 *   两标志被丢弃——服务端权威）。
 * - runs status 轮询信封 {run, nodes}（nodes 按当前图带 pending/stale 与
 *   各端口 row_count/stale 账）；outputs current 为 {"node:port": 行}。
 */
import { createProxyClient } from '@/api/proxyClient'

// ----------------------------------------------------------------- 类型

export type WorkspaceStatus = 'active' | 'archived'

/**
 * run 状态七值词表（012 DDL / executor._TERMINAL_STATUSES）：
 * 非终态 queued/running/awaiting_review → 终态 completed/partial/
 * failed/cancelled。**无 'succeeded'**（那是导出行词表——勿混）。
 */
export type RunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_review'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'cancelled'

/** 工作区行（列表口径：无 graph_json；021 DDL _WS_COLUMNS 同序）。 */
export interface WorkspaceSummary {
  id: string
  domain_id: string
  name: string
  description: string
  owner_user_id: string
  /**
   * 负责人用户名（db 层 get/list LEFT JOIN kb_users；用户行不存在 →
   * null）。create/patch/archive 的 RETURNING 不带该列——前端显示回落
   * owner_user_id 前 8 位。
   */
  owner_username?: string | null
  status: WorkspaceStatus
  graph_revision: number
  batch_no: number
  actor_kind: string
  actor_id: string | null
  created_by: string
  created_at: string
  updated_at: string
}

/**
 * 详情行：完整行 + graph_json + my_role。2026-11-17 收紧后 my_role 只剩
 * 合成角色 'admin' | 'domain_admin'（服务层 _assert_ws_role 仅管理员判定，
 * 普通成员 404 反探测）；owner/editor/viewer 保留在联合里作历史口径兼容。
 */
export interface WorkspaceDetail extends WorkspaceSummary {
  graph_json: WorkspaceGraph
  my_role: 'owner' | 'editor' | 'viewer' | 'admin' | 'domain_admin'
}

/**
 * DELETE 两态结果（用户反馈问题1——两步门禁）：active 工作区 → 归档行
 * （status='archived'，整行返回）；已归档 → 真删（行已不存在，只回
 * {id, status:'deleted'}——调用方按 status 从列表移除该行）。
 */
export type WorkspaceDeleteResult =
  | WorkspaceSummary
  | { id: string; status: 'deleted' }

/** 图节点输出端口（db 层 _validate_ports 归一形态）。 */
export interface GraphPort {
  id: string
  name: string
  kind: string
}

/**
 * 图节点（db 层 _validate_node 归一形态）。pending/stale 只在**读**（GET
 * graph / run status nodes）出现——服务端 diff 写回；PUT 时携带会被丢弃。
 */
export interface GraphNode {
  id: string
  op: string
  name: string
  config: Record<string, unknown>
  /** 上游引用列表，每项形如 "node:port"。 */
  inputs: string[]
  ports: GraphPort[]
  x: number
  y: number
  /** 读取态：配置待执行（配置变更后未跑）。 */
  pending?: boolean
  /** 读取态：上游已推进，输出过期。 */
  stale?: boolean
}

/** 整图（{nodes:[...]}；db 层校验环/引用/规模 ≤200）。 */
export interface WorkspaceGraph {
  nodes: GraphNode[]
}

/** GET/PUT graph 信封（PUT 返回多 updated_at 新令牌）。 */
export interface GraphEnvelope {
  graph: WorkspaceGraph
  graph_revision: number
  batch_no: number
  updated_at?: string
}

/** 成员行（list_members 联 username/display_name）。 */
export interface WorkspaceMember {
  user_id: string
  role: 'owner' | 'editor' | 'viewer'
  username: string | null
  user_name: string | null
}

/** 来源行（list 口径：input_manifest 已 slim——只剩计数无 dataset 本体）。 */
export interface WorkspaceSource {
  id: string
  workspace_id: string
  node_id: string
  connector: 'kb_dir' | 'excel'
  config: Record<string, unknown>
  input_manifest: Record<string, unknown> | null
  last_read_at: string | null
  created_by: string
  created_at: string
  updated_at: string
}

/** 来源写响应（_source_response：行剥 manifest 本体 + frozen 计数 + 推进后的 batch_no）。 */
export interface WorkspaceSourceWriteResult {
  id: string
  node_id: string
  connector: 'kb_dir' | 'excel'
  config: Record<string, unknown>
  last_read_at: string | null
  created_by: string
  created_at: string
  updated_at: string
  frozen: Record<string, unknown>
  batch_no: number | null
}

/** run 行（list/get 均已剥 graph_snapshot_json 审计快照）。 */
export interface RunSummary {
  id: string
  workspace_id: string
  scope: 'workspace' | 'node' | 'subgraph'
  target_node_id: string | null
  graph_revision: number
  graph_hash: string
  batch_no: number
  status: RunStatus
  progress_json: Record<string, unknown> | null
  error: string | null
  actor_kind: string
  actor_id: string | null
  created_by: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

/** run status 节点态（executor.get_status 按当前图合成）。 */
export interface RunNodeStatus {
  id: string
  op: string
  name: string
  pending: boolean
  stale: boolean
  outputs: Record<string, {
    row_count: number
    content_hash: string
    stale: boolean
    stale_since_run: string | null
  }>
}

/** run status 轮询信封。 */
export interface RunStatusEnvelope {
  run: RunSummary
  nodes: RunNodeStatus[]
}

/** 单条输出行（content_inline 为 jsonb 解析后的数据集对象）。 */
export interface WorkspaceOutput {
  run_id: string
  workspace_id: string
  node_id: string
  port_id: string
  kind: string
  content_inline: Record<string, unknown>
  row_count: number
  content_hash: string
  quality_json: Record<string, unknown> | null
  is_current: boolean
  stale_since_run: string | null
  created_at: string
}

/** 当前输出面信封：键形如 "node:port"。 */
export interface OutputsEnvelope {
  outputs: Record<string, WorkspaceOutput>
}

/** 单口试跑结果（不建 run 不写库；前 3 行切片）。 */
export interface TrialResult {
  node_id: string
  port_id: string
  outputs: Record<string, unknown>
  quality: Record<string, unknown> | null
  trial: boolean
  input_rows: number
}

/** 导出单项请求（owner_user_id 缺省=工作区 owner）。 */
export interface ExportItemRequest {
  node_id: string
  port_id: string
  artifact_name: string
  owner_user_id?: string
}

/** 导出逐项结果（succeeded 带 artifact_id；failed 带 error 机器码）。 */
export interface ExportResultItem {
  node_id: string
  port_id: string
  artifact_id: string | null
  name: string
  status: 'succeeded' | 'failed'
  error?: string
}

/** 导出回执（幂等重放 200+duplicate:true；首发/部分成功 201）。 */
export interface ExportResult {
  exports: ExportResultItem[]
  duplicate?: boolean
}

// ----------------------------------------------------------------- 请求体

export interface WorkspaceCreateRequest {
  name: string
  description?: string
  /** 缺省回落用户首个绑定域（后端路由层解析）；前端显式传当前域。 */
  domain?: string
}

export interface WorkspacePatchRequest {
  name?: string
  description?: string
  expected_updated_at?: string
}

export interface SourceKbDirRequest {
  node_id: string
  kb_id: string
  directories: string[]
}

export interface SourceExcelRequest {
  node_id: string
  file: File
  sheet_name?: string
}

export interface RunStartRequest {
  scope: 'workspace' | 'node'
  target_node_id?: string
}

// ----------------------------------------------------------------- 客户端

export function useWorkspacesApi() {
  const client = createProxyClient('mining', { includeDomainQuery: false })

  return {
    // ── 工作区 CRUD（DELETE=归档，幂等软删）──
    async listWorkspaces(domain?: string): Promise<WorkspaceSummary[]> {
      const params = domain ? { domain_id: domain } : undefined
      const { data } = await client.get('/api/workspaces', { params })
      return data.workspaces ?? []
    },

    async createWorkspace(payload: WorkspaceCreateRequest): Promise<WorkspaceSummary> {
      const body: Record<string, unknown> = { name: payload.name }
      if (payload.description) body.description = payload.description
      if (payload.domain) body.domain_id = payload.domain
      const { data } = await client.post('/api/workspaces', body)
      return data
    },

    async getWorkspace(workspaceId: string): Promise<WorkspaceDetail> {
      const { data } = await client.get(`/api/workspaces/${workspaceId}`)
      return data
    },

    async patchWorkspace(
      workspaceId: string, payload: WorkspacePatchRequest,
    ): Promise<WorkspaceSummary> {
      const { data } = await client.patch(`/api/workspaces/${workspaceId}`, payload)
      return data
    },

    /** DELETE 两态：active → 归档（返回 archived 行）；已归档 → 真删（返回
     * status='deleted'，无整行——行已清理）。owner-only + admin 旁路。 */
    async deleteWorkspace(workspaceId: string): Promise<WorkspaceDeleteResult> {
      const { data } = await client.delete(`/api/workspaces/${workspaceId}`)
      return data
    },

    // ── 成员（Task 12 详情页接线；owner-only + admin 旁路）──
    async listMembers(workspaceId: string): Promise<WorkspaceMember[]> {
      const { data } = await client.get(`/api/workspaces/${workspaceId}/members`)
      return data.members ?? []
    },

    async addMember(
      workspaceId: string, userId: string, role: WorkspaceMember['role'],
    ): Promise<WorkspaceMember> {
      const { data } = await client.post(
        `/api/workspaces/${workspaceId}/members`, { user_id: userId, role })
      return data
    },

    async updateMemberRole(
      workspaceId: string, userId: string, role: WorkspaceMember['role'],
    ): Promise<WorkspaceMember> {
      const { data } = await client.patch(
        `/api/workspaces/${workspaceId}/members/${userId}`, { role })
      return data
    },

    async removeMember(workspaceId: string, userId: string): Promise<void> {
      await client.delete(`/api/workspaces/${workspaceId}/members/${userId}`)
    },

    // ── 图（整图替换 + 乐观锁 expected_revision）──
    async getGraph(workspaceId: string): Promise<GraphEnvelope> {
      const { data } = await client.get(`/api/workspaces/${workspaceId}/graph`)
      return data
    },

    async putGraph(
      workspaceId: string, graph: WorkspaceGraph, expectedRevision: number,
    ): Promise<GraphEnvelope> {
      const { data } = await client.put(`/api/workspaces/${workspaceId}/graph`, {
        graph, expected_revision: expectedRevision,
      })
      return data
    },

    // ── 来源（kb_dir 即读即冻 / excel multipart / 删）──
    async listSources(workspaceId: string): Promise<WorkspaceSource[]> {
      const { data } = await client.get(`/api/workspaces/${workspaceId}/sources`)
      return data.sources ?? []
    },

    async upsertSourceKbDir(
      workspaceId: string, payload: SourceKbDirRequest,
    ): Promise<WorkspaceSourceWriteResult> {
      const { data } = await client.post(
        `/api/workspaces/${workspaceId}/sources`,
        { connector: 'kb_dir', ...payload })
      return data
    },

    async uploadSourceExcel(
      workspaceId: string, payload: SourceExcelRequest,
    ): Promise<WorkspaceSourceWriteResult> {
      const form = new FormData()
      form.append('file', payload.file)
      form.append('node_id', payload.node_id)
      if (payload.sheet_name) form.append('sheet_name', payload.sheet_name)
      const { data } = await client.post(
        `/api/workspaces/${workspaceId}/sources/excel`, form)
      return data
    },

    async deleteSource(workspaceId: string, nodeId: string): Promise<void> {
      await client.delete(
        `/api/workspaces/${workspaceId}/sources/${encodeURIComponent(nodeId)}`)
    },

    // ── 运行面（start 202·cancel 幂等·status 轮询信封 {run,nodes}）──
    async startRun(
      workspaceId: string, payload: RunStartRequest,
    ): Promise<{ run_id: string }> {
      const { data } = await client.post(
        `/api/workspaces/${workspaceId}/runs`, payload)
      return data
    },

    async listRuns(workspaceId: string, limit?: number): Promise<RunSummary[]> {
      const params = limit != null ? { limit } : undefined
      const { data } = await client.get(
        `/api/workspaces/${workspaceId}/runs`, { params })
      return data.runs ?? []
    },

    async getRunStatus(workspaceId: string, runId: string): Promise<RunStatusEnvelope> {
      const { data } = await client.get(
        `/api/workspaces/${workspaceId}/runs/${runId}`)
      return data
    },

    async cancelRun(
      workspaceId: string, runId: string,
    ): Promise<{ cancelled: boolean }> {
      const { data } = await client.post(
        `/api/workspaces/${workspaceId}/runs/${runId}/cancel`)
      return data
    },

    // ── 输出与试跑 ──
    async getCurrentOutputs(workspaceId: string): Promise<OutputsEnvelope> {
      const { data } = await client.get(
        `/api/workspaces/${workspaceId}/outputs/current`)
      return data
    },

    async trialOutput(
      workspaceId: string, nodeId: string, portId: string,
    ): Promise<TrialResult> {
      const { data } = await client.post(
        `/api/workspaces/${workspaceId}/outputs/`
        + `${encodeURIComponent(nodeId)}/${encodeURIComponent(portId)}/trial`)
      return data
    },

    // ── 导出交付（幂等 request_id；逐项 status，全败 422）──
    async exportDrafts(
      workspaceId: string,
      payload: { request_id: string; items: ExportItemRequest[] },
    ): Promise<ExportResult> {
      const { data } = await client.post(
        `/api/workspaces/${workspaceId}/exports`, payload)
      return data
    },
  }
}
