import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { defineComponent } from 'vue'

/**
 * 导出制品草稿对话框：端口复选（不可导出禁用+原因）/ 名称默认与编辑 /
 * request_id 幂等键与 items 组装 / 结果回执（未发布+查看制品路由、失败
 * 码中文化、duplicate 重放说明）。
 */

const api = vi.hoisted(() => ({ exportDrafts: vi.fn() }))
const router = vi.hoisted(() => ({ push: vi.fn() }))

vi.mock('@/api/workspaces', () => ({ useWorkspacesApi: () => api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => '错误详情' }))
vi.mock('vue-router', () => ({ useRouter: () => router }))

import WorkspaceExportDialog from '../WorkspaceExportDialog.vue'
import type { ExportPortOption } from '../WorkspaceExportDialog.vue'
import type { ExportResult } from '@/api/workspaces'

// ── 测试观察层 stub ──

const ElDialogStub = {
  name: 'ElDialog',
  props: ['modelValue', 'title', 'width'],
  emits: ['update:modelValue'],
  template: '<div v-if="modelValue" class="el-dialog-stub"><slot /><slot name="footer" /></div>',
}

const ElCheckboxStub = defineComponent({
  name: 'ElCheckbox',
  props: { modelValue: { type: Boolean, default: false }, disabled: Boolean },
  emits: ['update:modelValue'],
  template: `
    <label class="el-checkbox-stub" :data-disabled="disabled || null">
      <input type="checkbox" :checked="modelValue" :disabled="disabled" @change="$emit('update:modelValue', !modelValue)" />
      <slot />
    </label>`,
})

const ElInputStub = defineComponent({
  name: 'ElInput',
  props: { modelValue: { type: String, default: '' }, placeholder: { type: String, default: '' }, disabled: Boolean },
  emits: ['update:modelValue'],
  template: `
    <input class="el-input-stub" :placeholder="placeholder" :disabled="disabled" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" />`,
})

const ElTooltipStub = {
  name: 'ElTooltip',
  props: ['content', 'placement', 'disabled'],
  template: '<div class="el-tooltip-stub" :data-content="disabled ? null : content"><slot /></div>',
}

const ElAlertStub = {
  name: 'ElAlert',
  props: ['title', 'type', 'closable'],
  template: '<div class="el-alert-stub" :data-type="type">{{ title }}</div>',
}

const ElTagStub = {
  name: 'ElTag',
  props: ['size', 'type', 'effect'],
  template: '<span class="el-tag-stub"><slot /></span>',
}

const ElButtonStub = {
  name: 'ElButton',
  props: ['disabled', 'type', 'size', 'text', 'loading'],
  emits: ['click'],
  template: '<button class="el-button-stub" :disabled="disabled || loading" @click="$emit(\'click\')"><slot /></button>',
}

const stubs = {
  ElDialog: ElDialogStub,
  ElCheckbox: ElCheckboxStub,
  ElInput: ElInputStub,
  ElTooltip: ElTooltipStub,
  ElAlert: ElAlertStub,
  ElTag: ElTagStub,
  ElButton: ElButtonStub,
}

// ── 工具 ──

function port(partial: Partial<ExportPortOption> & Pick<ExportPortOption, 'nodeId' | 'portId' | 'name'>): ExportPortOption {
  return {
    ref: `${partial.nodeId}:${partial.portId}`,
    nodeLabel: '节点',
    rowCount: 10,
    exportable: true,
    reason: '',
    ...partial,
  }
}

function mountExport(ports: ExportPortOption[]): VueWrapper {
  return mount(WorkspaceExportDialog, {
    props: {
      modelValue: true, workspaceId: 'wsp_1', ports, ownerLabel: '张三',
    },
    global: { stubs },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  router.push.mockResolvedValue(undefined)
})

describe('WorkspaceExportDialog', () => {
  it('lists ports with row counts; non-exportable ports are disabled with a reason', () => {
    const wrapper = mountExport([
      port({ nodeId: 'src_1', portId: 'main', name: '底表', nodeLabel: '产品资料' }),
      port({ nodeId: 'op_1', portId: 'main', name: '筛选结果', nodeLabel: '筛功率', exportable: false, reason: '输出已过期，需重新运行', rowCount: 5 }),
      port({ nodeId: 'op_2', portId: 'main', name: '抽取结果', nodeLabel: '抽取', exportable: false, reason: '该输出尚未执行', rowCount: null }),
    ])

    const line = wrapper.get('[data-test="export-port-op_1-main"]')
    expect(line.text()).toContain('输出已过期')
    expect((wrapper.get('[data-test="export-check-op_1-main"]').find('input').element as HTMLInputElement).disabled).toBe(true)
    expect(wrapper.get('[data-test="export-port-op_2-main"]').text()).toContain('尚未执行')
    // 未选任何项 → 导出按钮禁用
    expect((wrapper.get('[data-test="export-submit"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('selecting a port reveals a name input defaulting to the port name', async () => {
    const wrapper = mountExport([port({ nodeId: 'src_1', portId: 'main', name: '底表' })])
    await wrapper.get('[data-test="export-check-src_1-main"]').find('input').setValue(true)
    const nameInput = wrapper.get('[data-test="export-name-src_1-main"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('底表')

    await nameInput.setValue('网络设备台账')
    await wrapper.get('[data-test="export-submit"]').trigger('click')

    expect(api.exportDrafts).toHaveBeenCalledTimes(1)
    const [wsId, payload] = api.exportDrafts.mock.calls[0]!
    expect(wsId).toBe('wsp_1')
    // request_id 为幂等键（UUID 形态），重开/重选不固定
    expect(payload.request_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(payload.items).toEqual([{ node_id: 'src_1', port_id: 'main', artifact_name: '网络设备台账' }])
  })

  it('blocks submit with a warning when a selected port has a blank name', async () => {
    const wrapper = mountExport([port({ nodeId: 'src_1', portId: 'main', name: '底表' })])
    await wrapper.get('[data-test="export-check-src_1-main"]').find('input').setValue(true)
    await wrapper.get('[data-test="export-name-src_1-main"]').setValue('   ')
    await wrapper.get('[data-test="export-submit"]').trigger('click')
    expect(api.exportDrafts).not.toHaveBeenCalled()
  })

  it('renders the receipt: unpublished tag, view-artifact navigation and Chinese error mapping', async () => {
    const result: ExportResult = {
      exports: [
        { node_id: 'src_1', port_id: 'main', artifact_id: 'art_9', name: '网络设备台账', status: 'succeeded' },
        { node_id: 'op_1', port_id: 'main', artifact_id: null, name: '抽取结果', status: 'failed', error: 'stale_output' },
      ],
    }
    api.exportDrafts.mockResolvedValue(result)
    const wrapper = mountExport([port({ nodeId: 'src_1', portId: 'main', name: '底表' })])

    await wrapper.get('[data-test="export-check-src_1-main"]').find('input').setValue(true)
    await wrapper.get('[data-test="export-submit"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[data-test="export-result-src_1-main"]').exists()).toBe(true))

    const okRow = wrapper.get('[data-test="export-result-src_1-main"]')
    expect(okRow.text()).toContain('网络设备台账')
    expect(okRow.text()).toContain('未发布')
    expect(wrapper.get('[data-test="export-result-op_1-main"]').text()).toContain('输出已过期')

    await wrapper.get('[data-test="export-view"]').trigger('click')
    expect(router.push).toHaveBeenCalledWith({ name: 'artifact-detail', params: { artifactId: 'art_9' } })
    expect(wrapper.emitted('exported')).toEqual([[result]])

    // 再导出一批 → 回到选择面（可复选）
    await wrapper.get('[data-test="export-again"]').trigger('click')
    expect(wrapper.find('[data-test="export-result-src_1-main"]').exists()).toBe(false)
  })

  it('marks replayed duplicate receipts', async () => {
    api.exportDrafts.mockResolvedValue({
      exports: [{ node_id: 'src_1', port_id: 'main', artifact_id: 'art_9', name: '底表', status: 'succeeded' }],
      duplicate: true,
    })
    const wrapper = mountExport([port({ nodeId: 'src_1', portId: 'main', name: '底表' })])
    await wrapper.get('[data-test="export-check-src_1-main"]').find('input').setValue(true)
    await wrapper.get('[data-test="export-submit"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('.el-alert-stub').exists()).toBe(true))
    expect(wrapper.get('.el-alert-stub').text()).toContain('重复提交')
  })

  it('keeps the dialog open with an error toast when the whole export fails', async () => {
    api.exportDrafts.mockRejectedValue(Object.assign(new Error('all failed'), {
      response: { status: 422, data: { code: 'export_all_failed', message: 'export_all_failed: empty_output' } },
    }))
    const wrapper = mountExport([port({ nodeId: 'src_1', portId: 'main', name: '底表' })])
    await wrapper.get('[data-test="export-check-src_1-main"]').find('input').setValue(true)
    await wrapper.get('[data-test="export-submit"]').trigger('click')
    await vi.waitFor(() => expect(api.exportDrafts).toHaveBeenCalled())
    expect(wrapper.find('[data-test="export-result-src_1-main"]').exists()).toBe(false)
    expect(wrapper.emitted('exported')).toBeUndefined()
  })
})
