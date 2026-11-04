/**
 * Wiki 图谱视图模型（66号 §7 D03.1）——纯函数：pages/links 信封 →
 * vue-flow 节点/边数据 + 过滤。范式=@/utils/documentStructureGraph（图数据
 * 计算与组件解耦，测试主打纯函数）。
 *
 * 铁律：节点=本制品页面，箭头=正文引用方向（from→to）；引用解析以服务端
 * links 为准（别名歧义/去重已在服务端算好，前端只消费，不做浏览器正文
 * 解析）；缺失引用目标=断链伪节点（isBroken 虚线区分，不伪装成有效页面）。
 *
 * 布局：写死 ordinal 分层坐标——ordinal//1000 为层、层内按 ordinal 排列
 * （vue-flow 无内置 auto-layout 且 dagre 不在依赖，零新依赖约束下人工
 * 分层；编译产物 ordinal 按 toc 节段 ×1000 编址，人工页落层 0）。
 */
import type { WikiLink, WikiPageMeta } from '@/types/artifacts'

/** 四页型中文标记（63号 §4.3）——与 Editor/Reader 面板同表独立维护（纯展示映射）。 */
export const WIKI_PAGE_TYPE_LABELS: Record<string, string> = {
  topic: '主题',
  glossary: '术语',
  contradiction: '矛盾',
  open_question: '开放问题',
}

export interface WikiGraphNode {
  /** page_key（断链伪节点=断链目标键）。 */
  id: string
  label: string
  pageType: string
  typeLabel: string
  origin: string
  ordinal: number | null
  /** 断链伪节点：引用目标页不存在（不可打开、虚线渲染）。 */
  isBroken: boolean
  /** ordinal//1000（断链伪节点=页面最大层+1，沉底一列）。 */
  layer: number
  position: { x: number; y: number }
  outCount: number
  inCount: number
}

export interface WikiGraphEdge {
  id: string
  source: string
  target: string
  /** 断链边（目标缺失）——组件渲染虚线。 */
  isBroken: boolean
  anchorText: string
}

export interface WikiGraphModel {
  nodes: WikiGraphNode[]
  edges: WikiGraphEdge[]
}

/** 分层栅格（层宽容纳节点+间距；行高按节点盒高）。 */
const LAYER_WIDTH = 300
const ROW_HEIGHT = 96
const ORIGIN_X = 24
const ORIGIN_Y = 24

/** pages/links 信封 → 图模型：独立页（无引用）也有节点；断链目标补伪节点。 */
export function buildWikiGraph(
  pages: readonly WikiPageMeta[],
  links: readonly WikiLink[],
): WikiGraphModel {
  const pageByKey = new Map(pages.map(page => [page.page_key, page]))
  const brokenTargets = new Set(links
    .filter(link => link.status === 'broken' && !pageByKey.has(link.to_page))
    .map(link => link.to_page))

  const outCounts = new Map<string, number>()
  const inCounts = new Map<string, number>()
  const bump = (map: Map<string, number>, key: string) => {
    map.set(key, (map.get(key) ?? 0) + 1)
  }
  for (const link of links) {
    bump(outCounts, link.from_page)
    bump(inCounts, link.to_page)
  }

  const maxPageLayer = pages.reduce(
    (max, page) => Math.max(max, Math.floor(page.ordinal / 1000)), 0)

  const nodes: WikiGraphNode[] = [
    ...pages.map((page): WikiGraphNode => ({
      id: page.page_key,
      label: page.title || page.page_key,
      pageType: page.page_type,
      typeLabel: WIKI_PAGE_TYPE_LABELS[page.page_type] ?? page.page_type,
      origin: page.origin,
      ordinal: page.ordinal,
      isBroken: false,
      layer: Math.floor(page.ordinal / 1000),
      position: { x: 0, y: 0 },
      outCount: outCounts.get(page.page_key) ?? 0,
      inCount: inCounts.get(page.page_key) ?? 0,
    })),
    ...[...brokenTargets].map((key): WikiGraphNode => ({
      id: key,
      label: key,
      pageType: '',
      typeLabel: '断链',
      origin: '',
      ordinal: null,
      isBroken: true,
      layer: maxPageLayer + 1,
      position: { x: 0, y: 0 },
      outCount: 0,
      inCount: inCounts.get(key) ?? 0,
    })),
  ]

  const byLayer = new Map<number, WikiGraphNode[]>()
  for (const node of nodes) {
    const items = byLayer.get(node.layer) ?? []
    byLayer.set(node.layer, [...items, node])
  }
  for (const [layer, items] of byLayer) {
    const ordered = [...items].sort((left, right) => (
      (left.ordinal ?? Number.MAX_SAFE_INTEGER)
        - (right.ordinal ?? Number.MAX_SAFE_INTEGER)
      || left.id.localeCompare(right.id)
    ))
    ordered.forEach((node, index) => {
      node.position = { x: ORIGIN_X + layer * LAYER_WIDTH, y: ORIGIN_Y + index * ROW_HEIGHT }
    })
  }

  const nodeIds = new Set(nodes.map(node => node.id))
  const edges = links
    .map((link, index): WikiGraphEdge => ({
      id: `link-${index}`,
      source: link.from_page,
      target: link.to_page,
      isBroken: link.status === 'broken',
      anchorText: link.anchor_text,
    }))
    // 防御：两端都必须是已知节点（links 是页内容的派生数据，正常不会悬挂）
    .filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target))

  return { nodes, edges }
}

/** 搜索过滤：按节点标题/键匹配（大小写不敏感；空查询=全量），边只留两端都在的。 */
export function filterWikiGraph(model: WikiGraphModel, query: string): WikiGraphModel {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return model
  const matched = model.nodes.filter(node => (
    node.label.toLocaleLowerCase().includes(needle)
    || node.id.toLocaleLowerCase().includes(needle)
  ))
  return keepSubgraph(model, matched)
}

/** 「仅所选页及一跳」：中心 + 直接出入邻居 + 其间边；无中心（null）=全量。 */
export function restrictToOneHop(
  model: WikiGraphModel,
  centerKey: string | null,
): WikiGraphModel {
  if (!centerKey) return model
  const center = model.nodes.find(node => node.id === centerKey)
  if (!center) return model
  const keep = new Set<string>([centerKey])
  for (const edge of model.edges) {
    if (edge.source === centerKey) keep.add(edge.target)
    if (edge.target === centerKey) keep.add(edge.source)
  }
  return keepSubgraph(model, model.nodes.filter(node => keep.has(node.id)))
}

/** 子图收窄公用：节点子集 + 两端都在子集内的边（不悬挂边）。 */
function keepSubgraph(
  model: WikiGraphModel,
  nodes: readonly WikiGraphNode[],
): WikiGraphModel {
  const ids = new Set(nodes.map(node => node.id))
  return {
    nodes: [...nodes],
    edges: model.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)),
  }
}
