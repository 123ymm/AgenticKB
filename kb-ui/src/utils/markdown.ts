/**
 * 通用 markdown 渲染：marked 转 HTML → DOMPurify 消毒 → 返回 HTML 字符串。
 *
 * 与 help/renderMarkdown 的关系：抽自同一骨架，去掉帮助页特有的
 * 「第 NN 章」站内链接加工（那是帮助书侧处理，通用正文不需要）。
 * 消费方：WikiEditorPanel 预览等制品正文渲染面。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

export function renderMarkdown(md: string): string {
  const raw = marked.parse(md, { async: false })
  return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } })
}
