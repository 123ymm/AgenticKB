/**
 * 表格知识制品 API（61号 §十）—— 经 main_control 反向代理到 mining 服务。
 *
 * 走 createProxyClient('mining')：baseURL 每次请求按当前域重算
 * （/api/control-plane/api/v1/proxy/{domain}/mining）。制品路由本身不消费
 * domain 查询参数（域过滤走显式 domain_id），故关掉自动 domain 追加。
 * 错误文案统一由调用方用 apiErrorDetail 提取（409 带 {code} 信封）。
 */
import { createProxyClient } from '@/api/proxyClient'
import type {
  Artifact,
  ArtifactColumn,
  ArtifactDetail,
  ArtifactRole,
  ArtifactRowsPage,
  DiffResult,
  ImportBatch,
  ImportConflictPolicy,
  ImportReport,
  MemberInfo,
  PreviewEnvelope,
  PublishResult,
  RevisionInfo,
} from '@/types/artifacts'

export interface ArtifactCreateRequest {
  title: string
  description?: string
  domain_id?: string
}

export interface ArtifactMetaPatchRequest {
  title?: string
  description?: string
  usage_note?: string
  expected_updated_at?: string
}

export interface ColumnAddRequest {
  column_name: string
  column_type: string
  enum_values?: string[]
  is_indexed?: boolean
  expected_updated_at?: string
}

export interface ColumnPatchRequest {
  column_name?: string
  column_type?: string
  enum_values?: string[]
  is_indexed?: boolean
  status?: 'dropped'
  expected_updated_at?: string
}

export interface BatchAssignRequest {
  column: string
  value: unknown
  where: Array<{ column: string; op: string; value: unknown }>
  expected_updated_at?: string
}

export interface ImportRequest {
  preview_token: string
  /** build_mapping 产物，或 {sheet, header_row, column_ops} 原始 ops（后端补跑映射）。 */
  plan: Record<string, unknown>
  conflict_policy?: ImportConflictPolicy
}

/** update_cell 返回 {row_key, data, conflicts?}（成功格回显）。 */
export interface ArtifactRowLike {
  row_key: string
  data: Record<string, unknown>
  conflicts?: Record<string, unknown> | null
}

export function useArtifactsApi() {
  const client = createProxyClient('mining', { includeDomainQuery: false })

  return {
    // ── 制品 CRUD ──
    async create(payload: ArtifactCreateRequest): Promise<Artifact> {
      const { data } = await client.post('/api/artifacts', payload)
      return data
    },

    async list(params?: { domain_id?: string }): Promise<Artifact[]> {
      const { data } = await client.get('/api/artifacts', { params })
      return data.artifacts ?? []
    },

    async get(artifactId: string): Promise<ArtifactDetail> {
      const { data } = await client.get(`/api/artifacts/${artifactId}`)
      return data
    },

    async updateMeta(
      artifactId: string, payload: ArtifactMetaPatchRequest,
    ): Promise<Artifact> {
      const { data } = await client.patch(`/api/artifacts/${artifactId}`, payload)
      return data
    },

    async remove(artifactId: string): Promise<void> {
      await client.delete(`/api/artifacts/${artifactId}`)
    },

    // ── 生命周期 ──
    async publish(
      artifactId: string,
      payload: { change_summary: string; expected_updated_at?: string },
    ): Promise<PublishResult> {
      const { data } = await client.post(`/api/artifacts/${artifactId}/publish`, payload)
      return data
    },

    async rollback(
      artifactId: string, revision: number, expectedUpdatedAt?: string,
    ): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/rollback/${revision}`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    async deprecate(artifactId: string, expectedUpdatedAt?: string): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/deprecate`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    async republish(artifactId: string, expectedUpdatedAt?: string): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/republish`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    async discardDraft(artifactId: string, expectedUpdatedAt?: string): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/discard`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    /** draft vs current 差异（发布确认页数字口径）。 */
    async diff(artifactId: string): Promise<DiffResult> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/diff`)
      return data
    },

    /** 版本历史（运行态信息，viewer+ 可读；revision 倒序）。 */
    async listRevisions(artifactId: string): Promise<RevisionInfo[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/revisions`)
      return data.revisions ?? []
    },

    // ── 成员 ──
    async listMembers(artifactId: string): Promise<MemberInfo[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/members`)
      return data.members ?? []
    },

    async addMember(
      artifactId: string, userId: string, role: ArtifactRole,
    ): Promise<MemberInfo> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/members`, { user_id: userId, role })
      return data
    },

    async updateMember(
      artifactId: string, userId: string, role: ArtifactRole,
    ): Promise<MemberInfo> {
      const { data } = await client.patch(
        `/api/artifacts/${artifactId}/members/${userId}`, { role })
      return data
    },

    async removeMember(artifactId: string, userId: string): Promise<void> {
      await client.delete(`/api/artifacts/${artifactId}/members/${userId}`)
    },

    // ── 导入向导（§六 三步）──
    async uploadTable(artifactId: string, file: File): Promise<PreviewEnvelope> {
      const form = new FormData()
      form.append('file', file)
      const { data } = await client.post(`/api/artifacts/${artifactId}/table/upload`, form)
      return data
    },

    async getPreview(artifactId: string, token: string): Promise<PreviewEnvelope> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/table/preview/${token}`)
      return data
    },

    async importTable(artifactId: string, payload: ImportRequest): Promise<ImportReport> {
      const { data } = await client.post(`/api/artifacts/${artifactId}/table/import`, payload)
      return data
    },

    async listBatches(artifactId: string): Promise<ImportBatch[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/batches`)
      return data.batches ?? []
    },

    // ── 列定义 ──
    async listColumns(
      artifactId: string, includeDropped = false,
    ): Promise<ArtifactColumn[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/table/columns`, {
        params: includeDropped ? { include_dropped: true } : undefined,
      })
      return data.columns ?? []
    },

    async addColumn(
      artifactId: string, payload: ColumnAddRequest,
    ): Promise<ArtifactColumn> {
      const { data } = await client.post(`/api/artifacts/${artifactId}/table/columns`, payload)
      return data
    },

    async updateColumn(
      artifactId: string, columnId: string, payload: ColumnPatchRequest,
    ): Promise<ArtifactColumn> {
      const { data } = await client.patch(
        `/api/artifacts/${artifactId}/table/columns/${columnId}`, payload)
      return data
    },

    async setPrimaryKey(
      artifactId: string, columnIds: string[], expectedUpdatedAt?: string,
    ): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/table/primary-key`,
        { column_ids: columnIds, expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    // ── 行 ──
    async listRows(
      artifactId: string, params?: { draft?: boolean; page?: number; size?: number },
    ): Promise<ArtifactRowsPage> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/table/rows`, { params })
      return data
    },

    async updateCell(
      artifactId: string, rowKey: string,
      payload: { column: string; value: unknown; expected_updated_at?: string },
    ): Promise<ArtifactRowLike> {
      const { data } = await client.patch(
        `/api/artifacts/${artifactId}/table/rows/${encodeURIComponent(rowKey)}`, payload)
      return data
    },

    async batchAssign(
      artifactId: string, payload: BatchAssignRequest,
    ): Promise<{ updated: number }> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/table/rows/batch-assign`, payload)
      return data
    },

    async deleteRow(
      artifactId: string, rowKey: string, expectedUpdatedAt?: string,
    ): Promise<void> {
      await client.delete(
        `/api/artifacts/${artifactId}/table/rows/${encodeURIComponent(rowKey)}`,
        { data: { expected_updated_at: expectedUpdatedAt } },
      )
    },
  }
}
