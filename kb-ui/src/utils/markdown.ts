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
/** 站内双链标记：[[页面key]] / [[页面key|锚文本]]。 */
const WIKI_LINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g

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
 * 三种标记（key/锚文本/unit_id 均 HTML 转义后插属性，杜绝属性逃逸）：
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
    .replace(WIKI_LINK_RE, (_match, key: string, anchor?: string) => {
      const text = anchor ?? key
      return `<a href="#" data-wiki-page="${escapeHtml(key)}">${escapeHtml(text)}</a>`
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
