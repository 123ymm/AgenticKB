import { createProxyClient } from '@/api/proxyClient'
import type {
  CloneMiningWorkflowRequest,
  CreateMiningWorkflowRequest,
  MiningOperatorCatalog,
  MiningWorkflow,
  MiningWorkflowGraph,
  MiningWorkflowOption,
  MiningWorkflowValidationResult,
  MiningWorkflowVersion,
  PublishMiningWorkflowRequest,
  RestoreMiningWorkflowDraftRequest,
  SaveMiningWorkflowDraftRequest,
} from '@/types/miningWorkflow'

/** Global mining Workflow API. The proxy path remains Domain-routed, but the API receives no Domain filter. */
export function useMiningWorkflowApi() {
  const client = createProxyClient('mining', { includeDomainQuery: false })

  return {
    async getCatalog(): Promise<MiningOperatorCatalog> {
      const { data } = await client.get('/api/mining-operators/catalog')
      return data
    },

    async list(params?: { include_archived?: boolean }): Promise<MiningWorkflow[]> {
      const { data } = await client.get('/api/mining-workflows', { params })
      return data.items ?? []
    },

    async options(): Promise<MiningWorkflowOption[]> {
      const { data } = await client.get('/api/mining-workflows/options')
      return data.items ?? []
    },

    async get(workflowId: string): Promise<MiningWorkflow> {
      const { data } = await client.get(`/api/mining-workflows/${workflowId}`)
      return data
    },

    async create(payload: CreateMiningWorkflowRequest): Promise<MiningWorkflow> {
      const { data } = await client.post('/api/mining-workflows', payload)
      return data
    },

    async saveDraft(workflowId: string, payload: SaveMiningWorkflowDraftRequest): Promise<MiningWorkflow> {
      const { data } = await client.put(`/api/mining-workflows/${workflowId}/draft`, payload)
      return data
    },

    async validate(workflowId: string, graph?: MiningWorkflowGraph): Promise<MiningWorkflowValidationResult> {
      const { data } = await client.post(`/api/mining-workflows/${workflowId}/validate`, graph ? { graph } : {})
      return data
    },

    async publish(workflowId: string, payload: PublishMiningWorkflowRequest): Promise<MiningWorkflowVersion> {
      const { data } = await client.post(`/api/mining-workflows/${workflowId}/publish`, payload)
      return data
    },

    async listVersions(workflowId: string): Promise<MiningWorkflowVersion[]> {
      const { data } = await client.get(`/api/mining-workflows/${workflowId}/versions`)
      return data.items ?? []
    },

    async getVersion(workflowId: string, version: number): Promise<MiningWorkflowVersion> {
      const { data } = await client.get(`/api/mining-workflows/${workflowId}/versions/${version}`)
      return data
    },

    async restoreDraft(
      workflowId: string,
      version: number,
      payload: RestoreMiningWorkflowDraftRequest,
    ): Promise<MiningWorkflow> {
      const { data } = await client.post(
        `/api/mining-workflows/${workflowId}/versions/${version}/restore-draft`, payload,
      )
      return data
    },

    async clone(workflowId: string, payload: CloneMiningWorkflowRequest): Promise<MiningWorkflow> {
      const { data } = await client.post(`/api/mining-workflows/${workflowId}/clone`, payload)
      return data
    },

    async archive(workflowId: string, payload: { updated_by?: string } = {}): Promise<MiningWorkflow> {
      const { data } = await client.post(`/api/mining-workflows/${workflowId}/archive`, payload)
      return data
    },
  }
}

