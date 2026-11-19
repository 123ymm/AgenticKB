import { describe, expect, it, vi } from 'vitest'
import { generateRequestId } from '../uuid'

/** 内网 HTTP 兼容：crypto.randomUUID 不存在时必须仍能生成合法幂等键。 */
describe('generateRequestId（三层回退）', () => {
  it('标准路径：randomUUID 存在时返回 UUID 形态', () => {
    expect(generateRequestId()).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('回退一：无 randomUUID 但有 getRandomValues（内网 HTTP 常态）', () => {
    const orig = globalThis.crypto
    vi.stubGlobal('crypto', {
      getRandomValues: orig.getRandomValues.bind(orig),
    })
    try {
      expect(generateRequestId()).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('回退二：crypto 整体不存在（极老环境）仍唯一且 v4 形态', () => {
    vi.stubGlobal('crypto', undefined)
    try {
      const a = generateRequestId()
      const b = generateRequestId()
      expect(a).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      )
      expect(a).not.toBe(b)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
