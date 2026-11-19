import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { Connection, Filter, FolderOpened, List, MagicStick, Scissor } from '@element-plus/icons-vue'

import WorkspaceStepFlow from '../WorkspaceStepFlow.vue'
import type { GraphNode, WorkspaceOutput } from '@/api/workspaces'

/**
 * 步骤流组件（垂直步骤卡，替代画布）：节点渲染/状态徽章/端口选择/拓扑排序。
 * mount + 全量 el-* stub（vitest 无 unplugin 解析层——本地 stub 承接
 * el-button/el-icon，其余不出现于本组件模板）。
 */

function node(partial: Partial<GraphNode> & Pick<GraphNode, 'id' | 'op' | 'name'>): GraphNode {
  return {
    config: {},
    inputs: [],
    ports: [{ id: 'main', name: '主输出', kind: 'table' }],
    x: 0,
    y: 0,
    ...partial,
  }
}

function output(partial: Partial<WorkspaceOutput> = {}): WorkspaceOutput {
  return {
    run_id: 'run-1', workspace_id: 'ws-1', node_id: 'n', port_id: 'main',
    kind: 'table', content_inline: { cols: [], rows: [], grain: 'row' },
    row_count: 3, content_hash: 'h', quality_json: null, is_current: true,
    stale_since_run: null, created_at: '2026-11-16T08:00:00+00:00',
    ...partial,
  }
}

/** 徽章文案断言用：卡内 data-test=step-badge-{nodeId}。 */
function badgeOf(wrapper: VueWrapper, nodeId: string): string {
  return wrapper.get(`[data-test="step-badge-${nodeId}"]`).text()
}

function cardIds(wrapper: VueWrapper): string[] {
  return wrapper.findAll('.ws-stepflow__card').map(c => c.attributes('data-node-id') ?? '')
}

function mountFlow(nodes: GraphNode[], outputs: Record<string, WorkspaceOutput> = {}, selectedRef: string | null = null): VueWrapper {
  return mount(WorkspaceStepFlow, {
    props: { nodes, outputs, selectedRef },
  })
}

describe('WorkspaceStepFlow', () => {
  it('renders a card per node with name and mapped op icon', () => {
    const nodes = [
      node({ id: 'src1', op: 'source', name: '产品资料' }),
      node({ id: 'sel1', op: 'select', name: '选列' }),
    ]
    const wrapper = mountFlow(nodes, { 'src1:main': output() })

    expect(wrapper.findAll('.ws-stepflow__card')).toHaveLength(2)
    expect(wrapper.get('[data-node-id="src1"] .ws-stepflow__node-name').text()).toBe('产品资料')
    // op → element 图标映射（按算子语义：source=文件夹/select=清单）
    expect(wrapper.findComponent(FolderOpened).exists()).toBe(true)
    expect(wrapper.findComponent(List).exists()).toBe(true)
  })

  it('falls back to the list icon for unknown ops', () => {
    const wrapper = mountFlow([node({ id: 'x1', op: 'mystery', name: '未知算子' })])
    expect(wrapper.findComponent(List).exists()).toBe(true)
  })

  it('shows the four status badges: idle / done / stale / pending', () => {
    const nodes = [
      node({ id: 'idle1', op: 'filter', name: '从未跑过' }),
      node({ id: 'done1', op: 'filter', name: '已跑完' }),
      node({ id: 'stale1', op: 'filter', name: '上游推进' }),
      node({ id: 'pend1', op: 'filter', name: '改了配置', pending: true }),
    ]
    const outputs: Record<string, WorkspaceOutput> = {
      'done1:main': output({ node_id: 'done1' }),
      'stale1:main': output({ node_id: 'stale1', stale_since_run: 'run-0' }),
    }
    const wrapper = mountFlow(nodes, outputs)

    expect(badgeOf(wrapper, 'idle1')).toBe('未执行')
    expect(badgeOf(wrapper, 'done1')).toBe('已完成')
    expect(badgeOf(wrapper, 'stale1')).toBe('待更新')
    // pending 来自 graph 节点级标志（服务端 diff 写回）
    expect(badgeOf(wrapper, 'pend1')).toBe('配置待执行')
    expect(wrapper.get('[data-test="step-badge-pend1"]').classes()).toContain('is-pending')
    expect(wrapper.get('[data-test="step-badge-stale1"]').classes()).toContain('is-stale')
  })

  it('prefers the pending badge over stale when both apply', () => {
    const wrapper = mountFlow(
      [node({ id: 'both', op: 'join', name: '双态', pending: true, stale: true })],
      { 'both:main': output({ node_id: 'both', stale_since_run: 'run-0' }) })
    expect(badgeOf(wrapper, 'both')).toBe('配置待执行')
  })

  it('emits select with the node:port ref when a port button is clicked', async () => {
    const nodes = [
      node({
        id: 'src1', op: 'source', name: '来源',
        ports: [
          { id: 'main', name: '主输出', kind: 'table' },
          { id: 'rejected', name: '过滤剔除', kind: 'table' },
        ],
      }),
    ]
    const wrapper = mountFlow(nodes)

    await wrapper.get('[data-test="port-src1-rejected"]').trigger('click')
    expect(wrapper.emitted('select')).toEqual([['src1:rejected']])
  })

  it('emits select with the FIRST port ref when the card body is clicked', async () => {
    // 整卡选中（用户反馈问题2）：header/卡片体任意处点击 → 选中该节点
    // 首个输出端口（多端口卡点卡片主体 = 快捷选中主输出）
    const nodes = [
      node({
        id: 'src1', op: 'source', name: '来源',
        ports: [
          { id: 'main', name: '主输出', kind: 'table' },
          { id: 'rejected', name: '过滤剔除', kind: 'table' },
        ],
      }),
    ]
    const wrapper = mountFlow(nodes)

    await wrapper.get('[data-test="step-node-src1"]').trigger('click')
    expect(wrapper.emitted('select')).toEqual([['src1:main']])
  })

  it('does not bubble port button clicks into a card-level first-port select', async () => {
    // 端口按钮精确选口：点 rejected 只 emit 一次 rejected（不冒泡触发整卡
    // 的首口选中——否则会追加第二个 main 事件）
    const nodes = [
      node({
        id: 'src1', op: 'source', name: '来源',
        ports: [
          { id: 'main', name: '主输出', kind: 'table' },
          { id: 'rejected', name: '过滤剔除', kind: 'table' },
        ],
      }),
    ]
    const wrapper = mountFlow(nodes)

    await wrapper.get('[data-test="port-src1-rejected"]').trigger('click')
    expect(wrapper.emitted('select')).toEqual([['src1:rejected']])
  })

  it('marks the selected port and shows per-port row counts / stale meta', () => {
    const nodes = [
      node({
        id: 'src1', op: 'source', name: '来源',
        ports: [
          { id: 'main', name: '主输出', kind: 'table' },
          { id: 'aux', name: '辅助', kind: 'table' },
        ],
      }),
    ]
    const outputs: Record<string, WorkspaceOutput> = {
      'src1:main': output({ node_id: 'src1', port_id: 'main', row_count: 512 }),
      'src1:aux': output({ node_id: 'src1', port_id: 'aux', row_count: 8, stale_since_run: 'run-0' }),
    }
    const wrapper = mountFlow(nodes, outputs, 'src1:aux')

    const mainBtn = wrapper.get('[data-test="port-src1-main"]')
    const auxBtn = wrapper.get('[data-test="port-src1-aux"]')
    expect(auxBtn.classes()).toContain('is-selected')
    expect(mainBtn.classes()).not.toContain('is-selected')
    // 有 current 输出 → 行数；stale_since_run 非空 → 已过期标记（main 无过期）
    expect(mainBtn.text()).toContain('512')
    expect(mainBtn.text()).not.toContain('已过期')
    expect(auxBtn.text()).toContain('已过期')
  })

  it('marks ports without current output as 未执行', () => {
    const wrapper = mountFlow([node({ id: 'src1', op: 'source', name: '来源' })])
    expect(wrapper.get('[data-test="port-src1-main"]').text()).toContain('未执行')
  })

  it('orders nodes topologically regardless of input order', () => {
    // 乱序喂入：llm → join → split → 两个 source；同层（两个 source）保持
    // 传入相对序（excel1 在数组中先于 src1）
    const nodes = [
      node({ id: 'llm1', op: 'llm_extract', name: '抽取', inputs: ['join1:main'] }),
      node({ id: 'join1', op: 'join', name: '关联库存', inputs: ['split1:main', 'excel1:main'] }),
      node({ id: 'split1', op: 'split_sections', name: '切章节', inputs: ['src1:main'] }),
      node({ id: 'excel1', op: 'source', name: '库存Excel', inputs: [] }),
      node({ id: 'src1', op: 'source', name: '产品资料', inputs: [] }),
    ]
    const wrapper = mountFlow(nodes)
    expect(cardIds(wrapper)).toEqual(['excel1', 'src1', 'split1', 'join1', 'llm1'])
  })

  it('keeps original order for unrelated nodes (stable sort)', () => {
    const nodes = [
      node({ id: 'b', op: 'source', name: 'B' }),
      node({ id: 'a', op: 'source', name: 'A' }),
      node({ id: 'c', op: 'filter', name: 'C', inputs: ['b:main'] }),
    ]
    const wrapper = mountFlow(nodes)
    // 无依赖的 b/a 保持原相对序；c 排在其上游 b 之后
    expect(cardIds(wrapper)).toEqual(['b', 'a', 'c'])
  })

  it('describes input refs as upstream node · port names', () => {
    const nodes = [
      node({ id: 'src1', op: 'source', name: '产品资料' }),
      node({
        id: 'f1', op: 'filter', name: '筛功率',
        inputs: ['src1:main'],
      }),
    ]
    const wrapper = mountFlow(nodes)
    const chips = wrapper.findAll('.ws-stepflow__input-chip')
    expect(chips).toHaveLength(1)
    expect(chips[0]!.text()).toBe('产品资料 · 主输出')
  })

  it('emits add-source / add-operator from the entry buttons', async () => {
    const wrapper = mountFlow([node({ id: 'src1', op: 'source', name: '来源' })])
    await wrapper.get('[data-test="add-source"]').trigger('click')
    await wrapper.get('[data-test="add-operator"]').trigger('click')
    expect(wrapper.emitted('add-source')).toHaveLength(1)
    expect(wrapper.emitted('add-operator')).toHaveLength(1)
  })

  it('maps every documented op to an icon', () => {
    const wrapper = mountFlow([
      node({ id: 'n1', op: 'source', name: 'a' }),
      node({ id: 'n2', op: 'select', name: 'b' }),
      node({ id: 'n3', op: 'filter', name: 'c' }),
      node({ id: 'n4', op: 'join', name: 'd' }),
      node({ id: 'n5', op: 'split_sections', name: 'e' }),
      node({ id: 'n6', op: 'llm_extract', name: 'f' }),
    ])
    expect(wrapper.findComponent(FolderOpened).exists()).toBe(true)
    expect(wrapper.findComponent(List).exists()).toBe(true)
    expect(wrapper.findComponent(Filter).exists()).toBe(true)
    expect(wrapper.findComponent(Connection).exists()).toBe(true)
    expect(wrapper.findComponent(Scissor).exists()).toBe(true)
    expect(wrapper.findComponent(MagicStick).exists()).toBe(true)
  })
})
