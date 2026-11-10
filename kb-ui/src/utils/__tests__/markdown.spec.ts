import { describe, expect, it } from 'vitest'
import {
  extractWikiNavTarget,
  extractWikiSrcUnit,
  renderWikiMarkdown,
} from '../markdown'

/**
 * Wiki 标记渲染测试（63号批次2）：三种站内标记 → 预处理 HTML → marked →
 * DOMPurify。断言全部走「渲染产物落 DOM 再查询」——与属性序无关，且天然
 * 校验 data-wiki-page / data-unit-id 在 sanitize 后存活（ALLOW_DATA_ATTR 默认开）。
 */
function renderToHost(md: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = renderWikiMarkdown(md)
  return host
}

describe('renderWikiMarkdown', () => {
  it('[[页面key]] 渲染为站内锚——data-wiki-page 属性 sanitize 后存活', () => {
    const host = renderToHost('见 [[home]] 页面')
    const anchor = host.querySelector('a[data-wiki-page="home"]')
    expect(anchor).not.toBeNull()
    expect(anchor?.getAttribute('href')).toBe('#')
    expect(anchor?.textContent).toBe('home')
  })

  it('[[页面key|锚文本]] 以锚文本展示、data-wiki-page 仍取键', () => {
    const host = renderToHost('入口在 [[home|首页入口]]')
    const anchor = host.querySelector('a[data-wiki-page="home"]')
    expect(anchor).not.toBeNull()
    expect(anchor?.textContent).toBe('首页入口')
  })

  it('[[src:unit_id]] 渲染为 sup.wiki-src——data-unit-id/title 存活', () => {
    const host = renderToHost('来源 [[src:unit-123]]')
    const sup = host.querySelector('sup.wiki-src')
    expect(sup).not.toBeNull()
    expect(sup?.getAttribute('data-unit-id')).toBe('unit-123')
    expect(sup?.getAttribute('title')).toBe('unit-123')
    expect(sup?.textContent).toBe('¶')
  })

  it('key/锚文本/unit_id 含 HTML 特殊字符时先转义——属性值 round-trip 原文且无逃逸标签', () => {
    const host = renderToHost('[[a<b"c]] 与 [[x|锚<>&文本]] 以及 [[src:u"1]]')
    // 属性值按原文读回（转义在序列化层完成，语义不丢）
    const anchor = host.querySelector('a[data-wiki-page]')
    expect(anchor?.getAttribute('data-wiki-page')).toBe('a<b"c')
    const alias = host.querySelectorAll('a[data-wiki-page]')[1]
    expect(alias?.getAttribute('data-wiki-page')).toBe('x')
    expect(alias?.textContent).toBe('锚<>&文本')
    const sup = host.querySelector('sup.wiki-src')
    expect(sup?.getAttribute('data-unit-id')).toBe('u"1')
    // 键里的 < 不产生逃逸元素（不出现 <b>/<c> 标签节点）
    expect(host.querySelector('b')).toBeNull()
    expect(host.querySelectorAll('*').length).toBeGreaterThan(0)
  })

  it('普通 markdown 照常渲染（标记预处理不破坏标题/段落）', () => {
    const host = renderToHost('# 标题\n\n段落 [[home]]')
    expect(host.querySelector('h1')?.textContent).toBe('标题')
    expect(host.querySelector('p a[data-wiki-page="home"]')).not.toBeNull()
  })

  it('键/锚文本 trim（I1）——[[ home ]] 跳转属性与后端 extract_wiki_links 记边口径一致', () => {
    const host = renderToHost('见 [[ home ]] 与 [[ home | 首页 ]] 及空锚 [[ home |  ]]')
    const anchors = host.querySelectorAll('a[data-wiki-page]')
    expect(anchors).toHaveLength(3)
    expect(anchors[0].getAttribute('data-wiki-page')).toBe('home')
    expect(anchors[0].textContent).toBe('home')
    expect(anchors[1].getAttribute('data-wiki-page')).toBe('home')
    expect(anchors[1].textContent).toBe('首页')   // 锚文本 trim 后展示
    expect(anchors[2].textContent).toBe('home')   // 空锚回退键文本
  })

  it('括号口径对齐后端（M1）——[[a[b]] 不产活链、保留纯文本；空键不产链', () => {
    const host = renderToHost('幽灵链 [[a[b]] 与空键 [[ ]]')
    // 后端 _WIKI_LINK_RE 禁括号 → 不记边；前端同口径不产可点击链
    expect(host.querySelector('a[data-wiki-page]')).toBeNull()
    expect(host.textContent).toContain('[[a[b]]')
    expect(host.textContent).toContain('[[ ]]')
  })
})

describe('extractWikiNavTarget / extractWikiSrcUnit（点击委托）', () => {
  function buildHost(): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML =
      '<p><a href="#" data-wiki-page="home"><span>内层文字</span></a>' +
      '<sup class="wiki-src" data-unit-id="u-1">¶</sup></p>'
    return host
  }

  it('命中 a[data-wiki-page]（点击自身或内部节点经 closest 冒泡解析）', () => {
    const host = buildHost()
    expect(extractWikiNavTarget(host.querySelector('a'))).toBe('home')
    // 点击锚内部 span → closest 上溯到锚
    expect(extractWikiNavTarget(host.querySelector('span'))).toBe('home')
  })

  it('未命中双链（sup / 容器 / null）返回 null', () => {
    const host = buildHost()
    expect(extractWikiNavTarget(host.querySelector('sup'))).toBeNull()
    expect(extractWikiNavTarget(host)).toBeNull()
    expect(extractWikiNavTarget(null)).toBeNull()
  })

  it('命中 sup.wiki-src 返回 unit_id；锚/容器/null 返回 null', () => {
    const host = buildHost()
    expect(extractWikiSrcUnit(host.querySelector('sup'))).toBe('u-1')
    expect(extractWikiSrcUnit(host.querySelector('a'))).toBeNull()
    expect(extractWikiSrcUnit(host)).toBeNull()
    expect(extractWikiSrcUnit(null)).toBeNull()
  })
})
