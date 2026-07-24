import type { HealthStatus, SearchResult } from '@/types'
import { createProxyClient } from '@/api/proxyClient'

export function useServingApi() {
  const client = createProxyClient('serving')

  return {
    async getHealth(): Promise<HealthStatus> {
      const { data } = await client.get('/actuator/health')
      return data
    },

    async search(query: string, options?: { domain?: string; debug?: boolean }): Promise<SearchResult> {
      const { data } = await client.post('/api/v1/search', {
        query,
        domain: options?.domain,
        debug: options?.debug ?? true,
      })
      return data.data ?? data
    },
  }
}
