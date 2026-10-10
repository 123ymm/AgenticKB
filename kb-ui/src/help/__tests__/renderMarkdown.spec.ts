/**
 * 帮助书结构与渲染：manifest 一致性（sync-help 生成物）、markdown 渲染、消毒、章号链接。
 * 走 helpContent 的加载器取正文（不直接读文件系统——app tsconfig 无 node 类型）。
 * 运行前置：pretest 钩子已跑 node scripts/sync-help.mjs（content/ 与 helpManifest.ts 就位）。
 */
import { describe, expect, it } from 'vitest'
import { HELP_CONTENT_FILES, loadHelpRaw } from '@/help/helpContent'
import { HELP_CHAPTER_COUNT, HELP_PARTS } from '@/help/helpManifest'
import { renderHelpMarkdown } from '@/help/renderMarkdown'

describe('帮助书结构（sync-help 生成物）', () => {
  it('四部分 23 章：章号唯一升序，内容文件与 manifest 一一对应', async () => {
    const chapters = HELP_PARTS.flatMap((p) => p.chapters)
    const ids = chapters.map((c) => c.id)
    expect(ids).toHaveLength(HELP_CHAPTER_COUNT)
    expect(new Set(ids).size).toBe(ids.length)
    expect([...ids].sort()).toEqual(ids)
    // 内容目录不多不少：多出的文件 = 书里加了章但没进 PART_RANGES；少 = 缺章
    expect(HELP_CONTENT_FILES).toHaveLength(HELP_CHAPTER_COUNT)
    for (const ch of chapters) {
      await expect(loadHelpRaw(ch.file), ch.file).resolves.toBeTypeOf('string')
    }
  })
})

describe('renderHelpMarkdown', () => {
  it('渲染标题/表格/代码块', () => {
    const html = renderHelpMarkdown(
      '# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```bash\necho hi\n```',
    )
    expect(html).toContain('<h1>标题</h1>')
    expect(html).toContain('<table>')
    expect(html).toContain('<pre><code')
  })

  it('消毒：script 标签被剥掉', () => {
    const html = renderHelpMarkdown('正文\n\n<script>alert(1)</script>')
    expect(html).not.toContain('<script')
  })

  it('「第 15 章」链接到 /help/15；不存在的章号与区间引用保持纯文本', () => {
    const html = renderHelpMarkdown('见第 15 章（MCP）；第 99 章不存在；见第 12–16 章区间')
    expect(html).toContain('href="/help/15"')
    expect(html).not.toContain('/help/99')
  })

  it('全书每一章均可渲染、非空且无脚本注入（冒烟）', async () => {
    for (const ch of HELP_PARTS.flatMap((p) => p.chapters)) {
      const html = renderHelpMarkdown(await loadHelpRaw(ch.file))
      expect(html.length > 0, ch.file).toBe(true)
      expect(html, ch.file).not.toContain('<script')
    }
  })
})
