// 书页懒加载：glob 相对本文件解析（src/help/content/），构建期打进产物、运行期零请求。
// 缓存是模块级的——HelpView 换章复用，正文搜索也吃它（预取全书后即可全文过滤）。
// 版本号随缓存写入递增：Map 本身非响应式，依赖缓存的 computed（关键词过滤）靠它感知
// 预取完成——否则慢网下先搜后到会把未命中章节静默漏掉。
import { ref } from 'vue'

const loaders = import.meta.glob('./content/*.md', {
  query: '?raw',
  import: 'default',
}) as Record<string, () => Promise<string>>

const cache = new Map<string, string>()

export const helpContentVersion = ref(0)

/** 内容目录的文件清单（glob 键形如 './content/01-平台是什么.md'）——供一致性断言。 */
export const HELP_CONTENT_FILES: readonly string[] = Object.keys(loaders)

export async function loadHelpRaw(file: string): Promise<string> {
  const cached = cache.get(file)
  if (cached !== undefined) return cached
  const loader = loaders[`./content/${file}`]
  if (!loader) {
    throw new Error(`章节内容缺失：${file}（先跑 node scripts/sync-help.mjs）`)
  }
  const raw = await loader()
  cache.set(file, raw)
  helpContentVersion.value += 1
  return raw
}

export const helpContentCache: ReadonlyMap<string, string> = cache
