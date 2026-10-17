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
  extra: { draftOpen?: boolean; currentRevision?: number } = {},
): VueWrapper {
  return shallowMount(ArtifactPublishDialog, {
    props: {
      artifactId: 'art-1',
      modelValue: true,
      expectedUpdatedAt: '2026-10-16T08:00:00+00:00',
      usageNote,
      draftOpen: extra.draftOpen,
      currentRevision: extra.currentRevision,
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
})
