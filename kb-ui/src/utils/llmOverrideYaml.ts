import { load as yamlLoad, dump as yamlDump } from 'js-yaml'

/**
 * 域覆盖 YAML 的表单双向读写（通用工具：llm_service 分段 / onenet 平铺）。
 *
 * 单一真相源是 YAML 字符串（即所存原文）；表单字段经 readForm/patchForm
 * （分段）或 readFlat/patchFlat（平铺）读写该字符串，高级 YAML 模式直接
 * 编辑同一字符串——零双状态同步。
 */

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {}
}

/** 覆盖 YAML → 对象。空/全注释/非法 → {}（表单降级可用；服务端 400 仍可达用户）。 */
export function parseOverrideYaml(text: string): Record<string, unknown> {
  try {
    return asRecord(yamlLoad(text))
  } catch {
    return {}
  }
}

/** 读某段某字段为字符串（缺省 ''，表单空值语义 = 继承全局）。 */
export function readForm(text: string, section: string, field: string): string {
  const sec = parseOverrideYaml(text)[section]
  const v = asRecord(sec)[field]
  if (v == null) return ''
  return typeof v === 'string' ? v : String(v)
}

/**
 * 表单字段写回 YAML 原文：空值 = 删键；段内清空 = 删段；其余键原样保留。
 * 返回新字符串（js-yaml dump 不保留注释——表单是唯一书写路径时无注释可丢；
 * 载入/切换域永不规范化原文）。
 */
export function patchForm(text: string, section: string, field: string, value: string): string {
  const doc = parseOverrideYaml(text)
  const sec = asRecord(doc[section])
  if (value === '') delete sec[field]
  else sec[field] = value
  if (Object.keys(sec).length === 0) delete doc[section]
  else doc[section] = sec
  return yamlDump(doc, { lineWidth: -1 })
}

/** 平铺覆盖（onenet 10 键无段结构）：读某键为字符串（缺省 '' = 继承全局）。 */
export function readFlat(text: string, field: string): string {
  const v = parseOverrideYaml(text)[field]
  if (v == null) return ''
  return typeof v === 'string' ? v : String(v)
}

/** 平铺覆盖键写回：空值 = 删键；其余键原样保留（全清后 dump 为 `{}` 空映射）。 */
export function patchFlat(text: string, field: string, value: string): string {
  const doc = parseOverrideYaml(text)
  if (value === '') delete doc[field]
  else doc[field] = value
  return yamlDump(doc, { lineWidth: -1 })
}

/** embedding 段（无/非映射 → {}）。非空 = 本域使用独立 embedding 配置。 */
export function embeddingSection(text: string): Record<string, unknown> {
  return asRecord(parseOverrideYaml(text).embedding)
}

/** 覆盖文件里声明的自有 chat 模型键（provider.models 的键，供下拉候选并集）。 */
export function ownModelKeys(text: string): string[] {
  const provider = asRecord(parseOverrideYaml(text).provider)
  return Object.keys(asRecord(provider.models))
}
