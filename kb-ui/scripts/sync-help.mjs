// 把 docs/平台指南 的书页同步进 kb-ui：生成 src/help/content/*.md 与 src/help/helpManifest.ts。
//
// 铁律：
//  - 真相源永远是仓库的 docs/平台指南/（repo 读者看全书）；本目录下的生成物不进 git。
//  - 本脚本挂在 npm 的 predev / prebuild / pretest 钩子上自动执行，无需手动调用。
//  - 缺章、多章、编号越界、命名不规范 → 立即失败，让构建在第一时间发现书结构漂移。
//
// 00-目录.md 是 repo 阅读入口（阅读路径/状态表/与旧文档关系），平台侧的章树即目录，跳过。
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const kbRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const srcDir = path.join(kbRoot, '..', 'docs', '平台指南')
const contentDir = path.join(kbRoot, 'src', 'help', 'content')
const manifestPath = path.join(kbRoot, 'src', 'help', 'helpManifest.ts')

// 部分分组：编号区间 → 部分键/标签（与书 00-目录 的四部分一致）
const PART_RANGES = [
  { key: 'intro', label: '开篇', from: 1, to: 3 },
  { key: 'user', label: '用户篇', from: 10, to: 21 },
  { key: 'dev', label: '开发篇', from: 30, to: 34 },
  { key: 'ops', label: '运维篇', from: 40, to: 42 },
]

const SKIP_FILES = new Set(['00-目录.md'])

function fail(message) {
  console.error(`[sync-help] 失败：${message}`)
  process.exit(1)
}

if (!fs.existsSync(srcDir)) {
  fail(`找不到书目录 ${srcDir}（本脚本必须在仓库完整检出内运行）`)
}

const files = fs.readdirSync(srcDir).filter((f) => f.endsWith('.md')).sort()

const chapters = []
const seenIds = new Set()
for (const file of files) {
  if (SKIP_FILES.has(file)) continue
  const match = file.match(/^(\d{2})-(.+)\.md$/)
  if (!match) fail(`无法识别的章节文件名（应为 NN-标题.md）：${file}`)
  const id = match[1]
  if (seenIds.has(id)) fail(`章节编号重复：${id}（${file}）`)
  seenIds.add(id)
  const num = Number(id)
  const part = PART_RANGES.find((r) => num >= r.from && num <= r.to)
  if (!part) fail(`章节 ${id}（${file}）不落在任何编号区间 ${PART_RANGES.map((r) => `${r.from}-${r.to}`).join('/')} 内——若书新增了部分，请同步维护 PART_RANGES`)
  // 标题以书页 H1 为准（「# NN · 标题」）——比文件名忠实（文件名不含空格，如 MCP接入/MCP 接入）
  const h1 = fs
    .readFileSync(path.join(srcDir, file), 'utf8')
    .split('\n')
    .find((line) => line.startsWith('# '))
  const titleMatch = h1?.match(/^#\s+\d{2}\s·\s(.+?)\s*$/)
  if (!titleMatch) fail(`章节 H1 缺失或格式应为「# NN · 标题」：${file}`)
  chapters.push({ id, file, title: titleMatch[1], part: part.key })
}
chapters.sort((a, b) => Number(a.id) - Number(b.id))
if (chapters.length === 0) fail('书目录里没有任何章节文件')

// 内容同步：清空重建，避免残留已删除的章节
fs.rmSync(contentDir, { recursive: true, force: true })
fs.mkdirSync(contentDir, { recursive: true })
for (const ch of chapters) {
  fs.copyFileSync(path.join(srcDir, ch.file), path.join(contentDir, ch.file))
}

// 生成 helpManifest.ts（TS 侧唯一入口，避免 TS/Node 双份清单漂移）
const parts = PART_RANGES.map((r) => ({
  key: r.key,
  label: r.label,
  chapters: chapters
    .filter((c) => c.part === r.key)
    .map(({ id, file, title }) => ({ id, file, title })),
}))
const manifestTs = `// ⚠️ 自动生成：node scripts/sync-help.mjs（predev/prebuild/pretest 自动执行）——不要手改。
// 真相源：docs/平台指南/
export interface HelpChapter {
  id: string
  file: string
  title: string
}

export interface HelpPart {
  key: string
  label: string
  chapters: HelpChapter[]
}

export const HELP_PARTS: HelpPart[] = ${JSON.stringify(parts, null, 2)}

export const HELP_CHAPTER_COUNT = ${chapters.length}
`
fs.writeFileSync(manifestPath, manifestTs)
console.log(`[sync-help] 已同步 ${chapters.length} 章 → src/help/content/；helpManifest.ts 已生成`)
