import { createProxyClient } from '@/api/proxyClient'

export interface TableSource {
  id: string
  document_name: string
  directory_path?: string | null
  snapshot_id?: string | null
}
export interface PipelineRun {
  id: string
  status: string
  started_at: string
  error_summary?: string | null
  output: { row_count?: number; issue_count?: number }
  parent_run_id?: string
  step_index?: number
  operator?: string
  progress?: { done: number; total: number; task_id?: string }
}
export interface PipelineColumn { key: string; name: string; type: string }
export interface TableContent {
  表头: string[]
  数据: Array<Record<string, unknown>>
}
export interface PipelineRow {
  [key: string]: any
  row_id: string
  directory: string
  file_name: string
  file_content: string
  table_name: string
  table_content: TableContent
  provenance: { document_id: string; snapshot_id: string; table_ref: string; name_source: string }
}
export interface PipelineSnapshot extends PipelineRun {
  kb_id?: string
  publications?: Array<{ artifact_id: string; revision: number; row_count: number }>
  columns?: PipelineColumn[]
  parameters?: Record<string, any>
  preview?: boolean
  sources: TableSource[]
  rows: PipelineRow[]
  total: number
  issues: Array<{ document_id: string; file_name: string; reason: string; message: string }>
}

export function useArtifactPipelineApi() {
  const client = createProxyClient('mining', { includeDomainQuery: false })
  const base = (id: string) => `/api/artifacts/${encodeURIComponent(id)}/pipeline`
  return {
    async extract(id: string, kbId: string, documentIds: string[]) {
      return (await client.post<{ run_id: string }>(`${base(id)}/extract`, {
        kb_id: kbId, document_ids: documentIds,
      })).data
    },
    async runs(id: string) {
      return (await client.get<{ items: PipelineRun[] }>(`${base(id)}/runs`)).data.items
    },
    async snapshot(id: string, runId: string, page = 1, filters: unknown[] = []) {
      return (await client.get<PipelineSnapshot>(`${base(id)}/runs/${encodeURIComponent(runId)}`, {
        params: { page, size: 20, filters: JSON.stringify(filters) },
      })).data
    },
    async step(id: string, parent: string, operator: string, parameters: Record<string, any>) {
      return (await client.post<{ run_id: string }>(`${base(id)}/steps`, { parent_run_id: parent, operator, parameters })).data
    },
    async operators(id: string) {
      return (await client.get<{ items: Array<{ id: string; name: string; version: string }> }>(`${base(id)}/operators`)).data.items
    },
    async cancel(id: string, runId: string) { await client.post(`${base(id)}/runs/${encodeURIComponent(runId)}/cancel`) },
    async lookup(id: string, file: File) {
      const data = new FormData(); data.append('file', file)
      return (await client.post<{ lookup_id: string; filename: string; columns: PipelineColumn[]; row_count: number }>(`${base(id)}/lookup`, data)).data
    },
    async targets(id: string) {
      return (await client.get<{ items: Array<{ artifact_id: string; title: string; columns: Array<{ column_id: string; column_name: string; column_type: string; is_primary_key: boolean }> }> }>(`${base(id)}/targets`)).data.items
    },
    async publish(id: string, runId: string, plan: Record<string, any>, confirm = false) {
      return (await client.post<Record<string, any>>(`${base(id)}/runs/${encodeURIComponent(runId)}/publish`, plan, { params: { confirm } })).data
    },
  }
}
