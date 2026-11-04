import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: {
    diff: vi.fn(),
    publish: vi.fn(),
    updateMeta: vi.fn(),
  },
  ui: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('element-plus', () => ({
  ElMessage: {
    success: state.ui.success, error: state.ui.error, warning: state.ui.warning,
  },
}))

import ArtifactPublishDialog from '../ArtifactPublishDialog.vue'
import type { WikiDiffResult } from '@/types/artifacts'

interface DialogVm {
  diffResult: {
    added_count: number
    removed_count: number
    changed_count: number
  } | null
  usageNote: string
  changeSummary: string
  canPublish: boolean
  noPendingChanges: boolean
  confirmPublish: () => Promise<void>
}

function mountDialog(
  usageNote: string,
  extra: {
    draftOpen?: boolean
    currentRevision?: number
    kind?: 'table' | 'wiki'
    unitLabel?: string
  } = {},
): VueWrapper {
  return shallowMount(ArtifactPublishDialog, {
    props: {
      artifactId: 'art-1',
      modelValue: true,
      expectedUpdatedAt: '2026-10-16T08:00:00+00:00',
      usageNote,
      draftOpen: extra.draftOpen,
      currentRevision: extra.currentRevision,
      kind: extra.kind,
      unitLabel: extra.unitLabel,
    },
    global: {
      // 折叠面板渲染 title 插槽——三段计数文本断言需要
      stubs: {
        ElCollapse: { template: '<div><slot /></div>' },
        ElCollapseItem: {
          template: '<div class="collapse-item"><slot name="title" /><slot /></div>',
        },
        ElEmpty: true,
        ElAlert: { props: ['title'], template: '<div class="el-alert">{{ title }}</div>' },
      },
    },
  })
}

const diffResult = {
  added: ['K1', 'K2'],
  removed: ['K3'],
  changed: [{ row_key: 'R1', column: 'L2分类', old: '网络部署/扩容', new: '扩容/网络部署' }],
  added_count: 2,
  removed_count: 1,
  changed_count: 1,
}

const publishResult = {
  revision: 2, row_count: 9,
  diff: { added_count: 2, removed_count: 1, changed_count: 1 },
}

/** wiki 页级差异形态（66号 Task 1.5 契约）：changed=页卡（字段 old/new
 * 直给 + body_md 走 old/new_preview/diff_hunks/truncated）。 */
const wikiDiffResult: WikiDiffResult = {
  added: ['intro.md', 'guide/install.md'],
  removed: ['old-page.md'],
  changed: [
    {
      page_key: 'guide/quickstart.md',
      fields: ['title', 'page_type', 'body_md'],
      changes: {
        title: { old: '快速开始', new: '快速上手' },
        page_type: { old: 'doc', new: 'concept' },
        body_md: {
          old_preview: '旧正文预览',
          new_preview: '新正文预览',
          // 头两行 ---/+++ 带尾随空格——按原始行文本判断前缀（不 trim）
          diff_hunks: [
            '--- current ',
            '+++ draft ',
            '@@ -1,3 +1,4 @@',
            ' 上下文行保持不变',
            '-旧正文第一段',
            '+新正文第一段',
          ],
          truncated: true,
        },
      },
    },
    {
      page_key: 'glossary.md',
      fields: ['body_md'],
      changes: {
        body_md: {
          old_preview: '术语旧预览',
          new_preview: '术语新预览',
          diff_hunks: ['@@ -1,1 +1,1 @@', '-超长旧行内容…[行超长已截断]'],
        },
      },
    },
  ],
  added_count: 2,
  removed_count: 1,
  changed_count: 2,
}

beforeEach(() => {
  state.api.diff.mockReset().mockResolvedValue(diffResult)
  state.api.publish.mockReset().mockResolvedValue(publishResult)
  state.api.updateMeta.mockReset()
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
})

describe('ArtifactPublishDialog', () => {
  it('loads and renders the three diff sections on open', async () => {
    const wrapper = mountDialog('查责任域')
    await flushPromises()

    expect(state.api.diff).toHaveBeenCalledWith('art-1')
    const vm = wrapper.vm as unknown as DialogVm
    expect(vm.diffResult?.added_count).toBe(2)
    expect(vm.diffResult?.removed_count).toBe(1)
    expect(vm.diffResult?.changed_count).toBe(1)
    expect(wrapper.text()).toContain('新增 2 行')
    expect(wrapper.text()).toContain('修改 1 处')
    expect(wrapper.text()).toContain('删除 1 行')
    expect(wrapper.text()).toContain('K1')
    // 列级明细的 old→new 走 el-table 行作用域（浅桩不注入行数据）——以状态断言
    expect(vm.diffResult && (vm.diffResult as unknown as {
      changed: Array<{ row_key: string; column: string; old: string; new: string }>
    }).changed[0]).toMatchObject({
      row_key: 'R1', column: 'L2分类', old: '网络部署/扩容', new: '扩容/网络部署',
    })
  })

  it('blocks publishing while usage_note is empty', async () => {
    const wrapper = mountDialog('')
    await flushPromises()
    const vm = wrapper.vm as unknown as DialogVm

    expect(vm.canPublish).toBe(false)
    expect(wrapper.text()).toContain('发布必填')
    await vm.confirmPublish()
    expect(state.api.publish).not.toHaveBeenCalled()
    expect(wrapper.emitted('published')).toBeUndefined()
  })

  it('publishes with expected_updated_at when usage_note already on record', async () => {
    const wrapper = mountDialog('查责任域')
    await flushPromises()
    const vm = wrapper.vm as unknown as DialogVm
    vm.changeSummary = '  首版发布  '

    await vm.confirmPublish()
    await flushPromises()

    expect(state.api.updateMeta).not.toHaveBeenCalled()
    expect(state.api.publish).toHaveBeenCalledWith('art-1', {
      change_summary: '首版发布',
      expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    expect(wrapper.emitted('published')?.[0]).toEqual([publishResult])
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('patches usage_note first and publishes with the fresh lock token', async () => {
    state.api.updateMeta.mockResolvedValueOnce({
      artifact_id: 'art-1', updated_at: '2026-10-16T09:30:00+00:00',
    })
    const wrapper = mountDialog('')
    await flushPromises()
    const vm = wrapper.vm as unknown as DialogVm
    vm.usageNote = '割接前查 MOP 责任域'

    await vm.confirmPublish()
    await flushPromises()

    expect(state.api.updateMeta).toHaveBeenCalledWith('art-1', {
      usage_note: '割接前查 MOP 责任域',
      expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    expect(state.api.publish).toHaveBeenCalledWith('art-1', {
      change_summary: '',
      expected_updated_at: '2026-10-16T09:30:00+00:00',
    })
    expect(wrapper.emitted('changed')).toBeTruthy()
    expect(wrapper.emitted('published')).toBeTruthy()
  })

  it('surfaces stale_artifact 409 with refresh guidance', async () => {
    state.api.publish.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'stale_artifact' } } },
    })
    const wrapper = mountDialog('查责任域')
    await flushPromises()

    await (wrapper.vm as unknown as DialogVm).confirmPublish()
    await flushPromises()

    expect(state.ui.error).toHaveBeenCalledWith(expect.stringContaining('刷新'))
    expect(wrapper.emitted('published')).toBeUndefined()
  })

  it('blocks publishing when no pending changes (draft closed, revision > 0)', async () => {
    const wrapper = mountDialog('查责任域', { draftOpen: false, currentRevision: 2 })
    await flushPromises()
    const vm = wrapper.vm as unknown as DialogVm

    expect(vm.noPendingChanges).toBe(true)
    expect(vm.canPublish).toBe(false)
    expect(wrapper.text()).toContain('无待发布变更')
    await vm.confirmPublish()
    expect(state.api.publish).not.toHaveBeenCalled()
    expect(wrapper.emitted('published')).toBeUndefined()
  })

  it('allows first publish even with draft closed when revision is 0', async () => {
    const wrapper = mountDialog('查责任域', { draftOpen: false, currentRevision: 0 })
    await flushPromises()
    const vm = wrapper.vm as unknown as DialogVm

    expect(vm.noPendingChanges).toBe(false)
    expect(vm.canPublish).toBe(true)
  })

  it('renders wiki page-level diff (fields old→new + body hunks) instead of table rows/columns', async () => {
    state.api.diff.mockResolvedValueOnce(wikiDiffResult)
    const wrapper = mountDialog('查责任域', { kind: 'wiki', unitLabel: '页' })
    await flushPromises()

    expect(state.api.diff).toHaveBeenCalledWith('art-1')
    // 三段计数按「页」计数，changed 段走页级口径（不再是列级明细）
    expect(wrapper.text()).toContain('新增 2 页')
    expect(wrapper.text()).toContain('修改 2 页')
    expect(wrapper.text()).toContain('删除 1 页')
    expect(wrapper.text()).toContain('页级明细')
    // 新增/删除=页 key tag 列表（无页型映射数据——只显示 page_key，不伪造页型）
    expect(wrapper.text()).toContain('intro.md')
    expect(wrapper.text()).toContain('guide/install.md')
    expect(wrapper.text()).toContain('old-page.md')
    // 修改=每页折叠卡：page_key + 字段名 + 直显 old→new
    expect(wrapper.text()).toContain('guide/quickstart.md')
    expect(wrapper.text()).toContain('glossary.md')
    expect(wrapper.text()).toContain('title')
    expect(wrapper.text()).toContain('快速开始')
    expect(wrapper.text()).toContain('快速上手')
    expect(wrapper.text()).toContain('page_type')
    expect(wrapper.text()).toContain('doc')
    expect(wrapper.text()).toContain('concept')
    // body_md=后端 hunks 行级渲染（纯文本，含 @@ 头）
    expect(wrapper.text()).toContain('body_md')
    expect(wrapper.text()).toContain('@@ -1,3 +1,4 @@')
    expect(wrapper.text()).toContain('-旧正文第一段')
    expect(wrapper.text()).toContain('+新正文第一段')
    // 整体截断提示（body hunks truncated）
    expect(wrapper.text()).toContain('差异过长已截断')
    // 不再套表格行/列格式（66号 §10）
    expect(wrapper.html()).not.toContain('el-table')
    expect(wrapper.text()).not.toContain('列级明细')
    expect(wrapper.text()).not.toContain('行键')
  })

  it('colors hunk lines by raw prefix (no trim) and renders row-truncation suffix verbatim', async () => {
    state.api.diff.mockResolvedValueOnce(wikiDiffResult)
    const wrapper = mountDialog('查责任域', { kind: 'wiki', unitLabel: '页' })
    await flushPromises()

    // 页1 6 行 + 页2 2 行；头两行 ---/+++ 带尾随空格也按首字符判前缀
    const lines = wrapper.findAll('.publish-diff__hunk-line')
    expect(lines).toHaveLength(8)
    const classes = lines.map((line) => line.classes())
    expect(classes[0]).toContain('is-del') // '--- current '
    expect(classes[1]).toContain('is-add') // '+++ draft '
    expect(classes[2]).toContain('is-meta') // '@@ -1,3 +1,4 @@'
    expect(classes[3]).toEqual(['publish-diff__hunk-line']) // 上下文行不着色
    expect(classes[4]).toContain('is-del') // '-旧正文第一段'
    expect(classes[5]).toContain('is-add') // '+新正文第一段'
    expect(classes[7]).toContain('is-del') // '-超长旧行内容…[行超长已截断]'

    // 行超长截断以行尾后缀文本呈现（不写死 line_truncated 分支）——原样可见
    expect(wrapper.text()).toContain('超长旧行内容…[行超长已截断]')
    // 「差异过长已截断」只出现一次（仅 truncated:true 的页1；页2 行级后缀不触发）
    expect(wrapper.text().match(/差异过长已截断/g)).toHaveLength(1)
  })
})
