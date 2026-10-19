/**
 * row_key 展示层解码（008 UI 批次）。
 *
 * 后端（codex 三审）把业务主键 hex 编码后拼 row_key（`hexlify(utf8).join('.')`）
 * ——网关 `_has_unsafe_proxy_path` 拒绝代理路径一切 % 编码字节，协议层 hex 是
 * 终态。展示层把 hex 解回原文（多列主键 '.' 分隔 → ' · '），协议不动。
 *
 * 无主键制品的行键是 uuid.hex（32 位十六进制）——解码出来是乱码字节，
 * hasPrimaryKey=false 时原样返回。
 */

function decodeHexPart(part: string): string {
  if (!part || part.length % 2 !== 0 || !/^[0-9a-f]+$/i.test(part)) return part
  const bytes = new Uint8Array(part.length / 2)
  for (let i = 0; i < part.length; i += 2) {
    bytes[i / 2] = Number.parseInt(part.slice(i, i + 2), 16)
  }
  const decoded = new TextDecoder('utf-8', { fatal: false }).decode(bytes)
  // 解码结果控制字符占半数以上 → 判定为非文本（uuid 片段），回落原文
  let control = 0
  for (const ch of decoded) {
    const code = ch.charCodeAt(0)
    if (code < 0x20 && code !== 9) control++
  }
  return control * 2 >= decoded.length ? part : decoded
}

/** 行键展示：hex → 原文（' · ' 连接多列主键）；tooltip 请用原始 row_key。 */
export function decodeRowKey(rowKey: string, hasPrimaryKey: boolean): string {
  if (!hasPrimaryKey || !rowKey) return rowKey
  try {
    return rowKey.split('.').map(decodeHexPart).join(' · ')
  } catch {
    return rowKey
  }
}
