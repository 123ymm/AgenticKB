import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { DomainInfo } from '@/types'
import { useControlPlaneApi } from '@/api/controlPlane'

const STORAGE_KEY = 'kb-ui-current-domain'

export const useDomainStore = defineStore('domain', () => {
  const domains = ref<DomainInfo[]>([])
  const currentDomain = ref<string>(
    localStorage.getItem(STORAGE_KEY) || ''
  )
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref('')

  const currentDomainInfo = computed<DomainInfo | undefined>(() =>
    domains.value.find(d => d.domain_id === currentDomain.value)
  )

  const activeDomains = computed<string[]>(() =>
    domains.value.filter(d => d.enabled).map(d => d.domain_id)
  )

  const enabledDomains = computed<DomainInfo[]>(() =>
    domains.value.filter(d => d.enabled)
  )

  async function fetchDomains() {
    if (loaded.value) return
    loading.value = true
    error.value = ''
    try {
      const api = useControlPlaneApi()
      domains.value = await api.getDomains()
      loaded.value = true

      // Auto-select first enabled domain if current is invalid
      const isValid = domains.value.some(
        d => d.domain_id === currentDomain.value && d.enabled
      )
      if (!isValid) {
        const first = enabledDomains.value[0]
        if (first) {
          currentDomain.value = first.domain_id
          localStorage.setItem(STORAGE_KEY, first.domain_id)
        }
      }
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Failed to load domains'
    } finally {
      loading.value = false
    }
  }

  function switchDomain(domainId: string) {
    if (domains.value.some(d => d.domain_id === domainId && d.enabled)) {
      currentDomain.value = domainId
      localStorage.setItem(STORAGE_KEY, domainId)
    }
  }

  async function refreshDomains() {
    loaded.value = false
    await fetchDomains()
  }

  return {
    domains,
    currentDomain,
    currentDomainInfo,
    activeDomains,
    enabledDomains,
    loading,
    loaded,
    error,
    fetchDomains,
    switchDomain,
    refreshDomains,
  }
})
