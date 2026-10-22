/**
 * 通用 markdown 渲染：marked 转 HTML → DOMPurify 消毒 → 返回 HTML 字符串。
 *
 * 与 help/renderMarkdown 的关系：抽自同一骨架，去掉帮助页特有的
 * 「第 NN 章」站内链接加工（那是帮助书侧处理，通用正文不需要）。
 * 消费方：WikiEditorPanel 预览、WikiReaderPanel 正文等制品渲染面。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

export function renderMarkdown(md: string): string {
  const raw = marked.parse(md, { async: false })
  return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } })
}

// ── Wiki 站内标记（63号批次2）──

/** 溯源角标标记：[[src:unit_id]]（先于双链匹配，否则会被当成页面键）。 */
const WIKI_SRC_RE = /\[\[src:([^\]|]+)\]\]/g
/** 站内双链标记：[[页面key]] / [[页面key|锚文本]]。
 *
 * 字符口径对齐后端 db_wiki._WIKI_LINK_RE（方括号一律禁入；键另禁 |，
 * 锚文本允许含 |——后端 partition 取首个 | 后的余段）。捕获后 key/anchor
 * 各自 trim（同 extract_wiki_links），键 trim 后为空=后端不记边。
 */
const WIKI_LINK_RE = /\[\[([^\[\]|]+)(?:\|([^\[\]]+))?\]\]/g

/** HTML 转义（属性值与内联文本共用——键/锚文本/unit_id 不可信，先转义再插值）。 */
function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * Wiki 正文渲染：站内标记预处理 → renderMarkdown（marked → DOMPurify）。
 *
 * 三种标记（key/锚文本/unit_id 均 HTML 转义后插属性，杜绝属性逃逸；
 * key/锚文本捕获后 trim——与后端 extract_wiki_links 记边口径一致）：
 *   [[页面key]]       → <a href="#" data-wiki-page="key">key</a>
 *   [[页面key|锚文本]] → <a href="#" data-wiki-page="key">锚文本</a>
 *   [[src:unit_id]]   → <sup class="wiki-src" data-unit-id="id" title="id">¶</sup>
 *
 * DOMPurify 默认 ALLOW_DATA_ATTR=true——实测 USE_PROFILES html 下
 * data-wiki-page / data-unit-id / class / title / sup 均存活，无需 ADD_ATTR
 * （markdown.spec 有存活断言兜底，若升级 dompurify 改行为会先红）。
 */
export function renderWikiMarkdown(md: string): string {
  const preprocessed = md
    .replace(
      WIKI_SRC_RE,
      (_match, unitId: string) =>
        `<sup class="wiki-src" data-unit-id="${escapeHtml(unitId)}" title="${escapeHtml(unitId)}">¶</sup>`,
    )
    .replace(WIKI_LINK_RE, (match, key: string, anchor?: string) => {
      const pageKey = key.trim()
      // 与后端同口径：空键不记边 → 保留原文纯文本，不产可点击链
      if (!pageKey) return match
      const anchorText = (anchor ?? '').trim()
      return `<a href="#" data-wiki-page="${escapeHtml(pageKey)}">${escapeHtml(anchorText || pageKey)}</a>`
    })
  return renderMarkdown(preprocessed)
}

/** 点击委托：命中站内双链（a[data-wiki-page]，含内部节点冒泡）→ 目标页面键；否则 null。 */
export function extractWikiNavTarget(el: EventTarget | null): string | null {
  if (!(el instanceof Element)) return null
  return el.closest('a[data-wiki-page]')?.getAttribute('data-wiki-page') ?? null
}

/** 点击委托：命中溯源角标（sup.wiki-src）→ 切片 unit_id；否则 null。 */
export function extractWikiSrcUnit(el: EventTarget | null): string | null {
  if (!(el instanceof Element)) return null
  return el.closest('sup.wiki-src')?.getAttribute('data-unit-id') ?? null
}
