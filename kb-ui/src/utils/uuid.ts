/**
 * 请求幂等键生成（HTTP 内网环境兼容）。
 *
 * crypto.randomUUID 仅在安全上下文（HTTPS/localhost）存在——内网用
 * IP/域名走 HTTP 访问时该函数不存在，导出等依赖 request_id 的功能会
 * 直接抛 "crypto.randomUUID is not a function"。这里做三层回退：
 * ① randomUUID（安全上下文，标准 v4）
 * ② getRandomValues 拼标准 v4 格式（非安全上下文也可用）
 * ③ Math.random 兜底（极老环境，幂等键只需唯一性不需密码学强度）
 */
export function generateRequestId(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  if (c && typeof c.getRandomValues === 'function') {
    const bytes = c.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (ch) => {
    const r = (Math.random() * 16) | 0
    const v = ch === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}
