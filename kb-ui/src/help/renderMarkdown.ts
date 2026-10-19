// 帮助页 markdown 渲染：marked 转 HTML → dompurify 消毒 → 输出。
//
// 两个页面侧加工（设计定稿）：
//  1. 「第 NN 章」就地转成站内链接（仅对书里真实存在的章号），marked 渲染成 <a href="/help/NN">；
//     区间引用（如「第 12–16 章」）与不存在章号保持纯文本。
//  2. 素材来源里的仓库文件引用在书中是行内代码（非链接），无需处理；若未来书里出现
//     相对 .md 链接，HelpView 的点击委托会拦下不可达跳转（见 HelpView.onContentClick）。
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { HELP_PARTS } from './helpManifest'

const CHAPTER_IDS = new Set(HELP_PARTS.flatMap((p) => p.chapters.map((c) => c.id)))

/**
 * 章号引用（「第 15 章」）→ markdown 链接；书里所有引用都带空格，无空格形态一并兼容。
 * 只动散文：``` 围栏与行内 `code` 里的章号保持原样（书的流程图块内有「第 NN 章」字样，
 * 误转会把链接语法当字面文本渲染进 <code>——12/33 章已踩中）。
 */
function linkChapterRefs(md: string): string {
  const replaceProse = (text: string) =>
    text.replace(/第\s?(\d{2})\s?章/g, (whole, id: string) =>
      CHAPTER_IDS.has(id) ? `[${whole}](/help/${id})` : whole,
    )
  return md
    .split(/(```[\s\S]*?```)/g)
    .map((segment) =>
      segment.startsWith('```')
        ? segment
        : segment
            .split(/(`[^`\n]*`)/g)
            .map((piece) => (piece.startsWith('`') ? piece : replaceProse(piece)))
            .join(''),
    )
    .join('')
}

export function renderHelpMarkdown(md: string): string {
  const raw = marked.parse(linkChapterRefs(md), { async: false })
  return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } })
}
