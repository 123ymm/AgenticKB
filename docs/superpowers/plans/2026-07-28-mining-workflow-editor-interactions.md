# Mining Workflow 编辑器信息架构与连线交互 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让挖掘 Workflow 编辑器按业务能力组织全部 16 个算子，动态展示当前有效编辑状态，并提供可发现、可撤销的连线删除与重连能力。

**Architecture:** 服务端 Catalog 继续提供算子事实数据，前端新增纯表现层分组映射；图级工具函数作为动态编辑策略的唯一来源。编辑器自行管理选中边、删除和重连，并通过现有 `recordMutation` 统一进入业务图和撤销栈，不启用 Vue Flow 的默认键盘删除。

**Tech Stack:** Vue 3、TypeScript、Vue Flow 1.48、Element Plus、Vitest、Vue Test Utils。

---

## 1. 范围与已确认规则

### 1.1 算子目录

目录按业务能力分组，不按执行 `zone` 分组：

| 分组 | operator type |
|---|---|
| 输入与解析 | `input_ingest`, `parse_segment` |
| 篇章与检索 | `enrich`, `discourse_line`, `contextual_retrieval_enrich`, `retrieval_unit_build`, `embedding` |
| 实体与图谱 | `entity_extract`, `entity_resolve`, `entity_relation_extract`, `entity_review_gate`, `graph_write` |
| 本体演化 | `ontology_induction`, `ontology_review_gate` |
| 资产与发布 | `asset_persist`, `mining_finalize` |

每张目录卡片展示 `输入阶段 / 逐文档 / 整批次` 范围标签。卡片主色直接复用画布节点的 Catalog `category` 色表。

### 1.2 动态编辑状态

对用户只展示三个有效状态：

- `fixed` → 固定骨架；
- `required` → 当前必需；
- `optional` → 可选。

`required` 根据当前未禁用节点实时计算：存在任意实体能力算子时需要实体审核和图谱写入；存在本体归纳时需要实体审核、本体审核和图谱写入。本体归纳本身始终是可选算子。

### 1.3 连线规则

- 固定策略只约束节点，不约束与节点相连的边；
- 草稿态的所有边均可选中、删除、移动 source 端或 target 端；
- 删除允许草稿暂时不完整，本地与服务端校验负责阻止发布；
- 删除和重连必须进入同一套撤销/重做历史；
- 重连只有在端口类型兼容时提交；失败或取消时保留原边；
- 非 variadic 输入最多保留一条入边，新连接或重连成功后原占用边被原子替换；
- 历史版本预览为只读，不能删除或重连；
- Delete 与 Backspace 只在已选中边且焦点不在输入控件时生效。

## 2. 文件职责

- `kb-ui/src/utils/miningWorkflowPresentation.ts`：算子业务分组、分组顺序和 zone 文案，不包含图编辑规则。
- `kb-ui/src/utils/miningWorkflowGraph.ts`：动态图级编辑状态、删除权限、Vue Flow 映射类型。
- `kb-ui/src/components/mining/workflow/MiningOperatorPalette.vue`：业务分组、范围标签、动态状态标签。
- `kb-ui/src/components/mining/workflow/MiningOperatorNode.vue`：接收有效状态并生成节点徽标。
- `kb-ui/src/components/workflow/WorkflowOperatorNodeBase.vue`：为徽标提供解释性 `title`。
- `kb-ui/src/views/mining/WorkflowEditorView.vue`：选择边、右侧连接检查器、删除、快捷键、重连和撤销集成。
- `kb-ui/src/utils/__tests__/miningWorkflowGraph.spec.ts`：动态图规则单元测试。
- `kb-ui/src/components/mining/workflow/__tests__/MiningWorkflowEditor.spec.ts`：分组、范围和徽标组件测试。
- `kb-ui/src/views/mining/__tests__/WorkflowEditorView.spec.ts`：边删除、撤销、快捷键、固定节点端点重连和只读回归测试。

## 3. 状态与数据流

```text
Catalog + graph.nodes
  → effectiveEditState(definition, nodes)
  → 目录标签 / 画布徽标 / 检查器说明 / 节点删除权限

Vue Flow edge-click
  → selectedEdgeId
  → selectedEdge（从 flowEdges 派生）
  → 连接检查器
  → deleteSelectedEdge()
  → recordMutation()
  → graph.edges
  → applyGraph()
  → flowEdges + undoStack
```

边的稳定身份继续使用：

```text
fromNode.fromSlot->toNode.toSlot
```

每次 `applyGraph` 后，如果选中边已不存在，必须清空 `selectedEdgeId`。

## 4. 实施任务

### Task 1: 动态编辑状态成为唯一规则源

**Files:**
- Modify: `kb-ui/src/utils/miningWorkflowGraph.ts`
- Test: `kb-ui/src/utils/__tests__/miningWorkflowGraph.spec.ts`

- [ ] **Step 1: 写动态状态失败测试**

```ts
expect(effectiveEditState(fixed, [])).toBe('fixed')
expect(effectiveEditState(graphWrite, [])).toBe('optional')
expect(effectiveEditState(graphWrite, [entityExtractNode])).toBe('required')
expect(effectiveEditState(ontologyReview, [ontologyInductionNode])).toBe('required')
expect(effectiveEditState(ontologyInduction, [ontologyInductionNode])).toBe('optional')
```

- [ ] **Step 2: 运行测试并确认因缺少 `effectiveEditState` 失败**

Run: `npx vitest run src/utils/__tests__/miningWorkflowGraph.spec.ts --pool=threads --maxWorkers=1`

Expected: FAIL，提示导出不存在或断言无法满足。

- [ ] **Step 3: 实现有效状态和原因**

```ts
export type MiningEffectiveEditState = 'fixed' | 'required' | 'optional'

export function effectiveEditState(
  definition: MiningOperatorDef,
  nodes: MiningWorkflowNode[],
): MiningEffectiveEditState {
  if (definition.editPolicy === 'fixed') return 'fixed'
  return requiredProtectedOperatorTypes(nodes).has(definition.type) ? 'required' : 'optional'
}

export function effectiveEditReason(
  definition: MiningOperatorDef,
  nodes: MiningWorkflowNode[],
): string {
  const state = effectiveEditState(definition, nodes)
  if (state === 'fixed') return '系统固定骨架节点，不能删除'
  if (state !== 'required') return '当前 Workflow 中可选'
  if (definition.type === 'ontology_review_gate') return '当前存在本体归纳，发布前必须完成本体审核'
  if (definition.type === 'entity_review_gate') return '当前存在实体或本体能力线，发布前必须完成实体审核'
  return '当前存在实体或本体能力线，发布前必须写入图谱'
}
```

`canDeleteNodeInGraph` 改为仅当 `effectiveEditState(...) === 'optional'` 时返回 `true`。

- [ ] **Step 4: 运行单元测试确认通过**

Run: `npx vitest run src/utils/__tests__/miningWorkflowGraph.spec.ts --pool=threads --maxWorkers=1`

Expected: PASS。

### Task 2: 业务分组、范围标签和动态徽标

**Files:**
- Create: `kb-ui/src/utils/miningWorkflowPresentation.ts`
- Modify: `kb-ui/src/components/mining/workflow/MiningOperatorPalette.vue`
- Modify: `kb-ui/src/components/mining/workflow/MiningOperatorNode.vue`
- Modify: `kb-ui/src/components/workflow/WorkflowOperatorNodeBase.vue`
- Test: `kb-ui/src/components/mining/workflow/__tests__/MiningWorkflowEditor.spec.ts`

- [ ] **Step 1: 写目录和徽标失败测试**

测试必须断言：五个分组按既定顺序出现；`entity_review_gate` 位于“实体与图谱”；卡片展示“整批次”；加入实体节点后图谱写入展示“当前必需”；没有实体节点时展示“可选”；固定节点展示“固定骨架”。

- [ ] **Step 2: 运行组件测试确认失败**

Run: `npx vitest run src/components/mining/workflow/__tests__/MiningWorkflowEditor.spec.ts --pool=threads --maxWorkers=1`

Expected: FAIL，当前组件仍按 zone 分组并显示静态 Catalog 策略。

- [ ] **Step 3: 新增纯表现层映射**

```ts
export const MINING_OPERATOR_FAMILIES = [
  { key: 'input_parse', label: '输入与解析', types: ['input_ingest', 'parse_segment'] },
  { key: 'discourse_retrieval', label: '篇章与检索', types: ['enrich', 'discourse_line', 'contextual_retrieval_enrich', 'retrieval_unit_build', 'embedding'] },
  { key: 'entity_graph', label: '实体与图谱', types: ['entity_extract', 'entity_resolve', 'entity_relation_extract', 'entity_review_gate', 'graph_write'] },
  { key: 'ontology_evolution', label: '本体演化', types: ['ontology_induction', 'ontology_review_gate'] },
  { key: 'asset_publish', label: '资产与发布', types: ['asset_persist', 'mining_finalize'] },
] as const

export const MINING_ZONE_LABELS = {
  input: '输入阶段',
  document: '逐文档',
  global: '整批次',
} as const
```

未知扩展算子统一落入末尾“其他”组，防止服务端新增 Catalog 项后在目录中消失。

- [ ] **Step 4: 组件接收当前节点并展示有效状态**

`MiningOperatorPalette` 增加 `nodes: MiningWorkflowNode[]` prop；分组使用 `MINING_OPERATOR_FAMILIES`；状态使用 `effectiveEditState`；范围使用 `MINING_ZONE_LABELS`。固定节点保持不可拖动，`required` 节点仍可从目录添加，以便修复缺失的必需节点。

`MiningOperatorNode` 增加 `editState` 和 `editReason` prop；徽标文案映射为“固定骨架 / 当前必需 / 可选”。`WorkflowOperatorNodeBase` 将解释写入徽标 `title`。

- [ ] **Step 5: 运行组件测试确认通过**

Run: `npx vitest run src/components/mining/workflow/__tests__/MiningWorkflowEditor.spec.ts --pool=threads --maxWorkers=1`

Expected: PASS。

### Task 3: 边选择、检查器和显式删除

**Files:**
- Modify: `kb-ui/src/utils/miningWorkflowGraph.ts`
- Modify: `kb-ui/src/views/mining/WorkflowEditorView.vue`
- Test: `kb-ui/src/views/mining/__tests__/WorkflowEditorView.spec.ts`

- [ ] **Step 1: 写删除和撤销失败测试**

```ts
vm.selectEdge({ edge: { id: 'input.out->edit.in' } })
vm.deleteSelectedEdge()
expect(vm.graph.edges).toEqual([])
vm.undo()
expect(vm.graph.edges).toEqual([
  { fromNode: 'input', fromSlot: 'out', toNode: 'edit', toSlot: 'in' },
])
```

再断言右侧连接检查器展示 `input.out → edit.in`，删除后选中状态清空。

- [ ] **Step 2: 运行视图测试确认失败**

Run: `npx vitest run src/views/mining/__tests__/WorkflowEditorView.spec.ts --pool=threads --maxWorkers=1`

Expected: FAIL，当前不存在边选中与删除方法。

- [ ] **Step 3: 扩充边视图类型**

```ts
export interface MiningVueFlowEdge {
  id: string
  source: string
  sourceHandle: string
  target: string
  targetHandle: string
  selected?: boolean
  selectable?: boolean
  deletable?: boolean
  updatable?: boolean
  interactionWidth?: number
}
```

- [ ] **Step 4: 实现选中和删除**

编辑器新增 `selectedEdgeId`、`selectedEdge`、`selectEdge` 和 `deleteSelectedEdge`。`selectNode` 清空边选中，`selectEdge` 清空节点选中，pane click 同时清空。`deleteSelectedEdge` 通过 `recordMutation` 从 `graph.edges` 删除精确四元组。

模板新增 `@edge-click="selectEdge"`；右侧新增“连接”区，展示来源/目标端口和 `data-test="delete-edge"` 的危险按钮。只读时按钮禁用。

- [ ] **Step 5: 运行视图测试确认通过**

Run: `npx vitest run src/views/mining/__tests__/WorkflowEditorView.spec.ts --pool=threads --maxWorkers=1`

Expected: PASS。

### Task 4: 快捷键删除与固定节点边重连

**Files:**
- Modify: `kb-ui/src/views/mining/WorkflowEditorView.vue`
- Test: `kb-ui/src/views/mining/__tests__/WorkflowEditorView.spec.ts`

- [ ] **Step 1: 写快捷键与重连失败测试**

测试覆盖：Delete 删除选中边；Backspace 删除选中边；输入框焦点下不删除；只读预览不删除；连接固定节点的边可以把任一端拖到兼容端口；不兼容端口保持原边。

- [ ] **Step 2: 运行视图测试确认失败**

Run: `npx vitest run src/views/mining/__tests__/WorkflowEditorView.spec.ts --pool=threads --maxWorkers=1`

Expected: FAIL，当前快捷键被禁用，且 `onEdgeUpdate` 拒绝固定端点。

- [ ] **Step 3: 实现受控快捷键**

```ts
function isTextInput(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null
  return Boolean(element?.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(element?.tagName ?? ''))
}

function onEditorKeydown(event: KeyboardEvent) {
  if (!selectedEdge.value || readOnly.value || isTextInput(event.target)) return
  if (event.key !== 'Delete' && event.key !== 'Backspace') return
  event.preventDefault()
  deleteSelectedEdge()
}
```

在 mounted/unmounted 生命周期注册和移除 `window.keydown`。继续保持 `delete-key-code="null"`，避免 Vue Flow 绕过 `recordMutation`。

- [ ] **Step 4: 原子重连**

`onEdgeUpdate` 不再调用节点级 `canReconnectNode`。`canApplyConnection(connection, replacingEdgeId)` 校验端口类型时排除当前被移动的边；成功后一次 mutation 删除旧四元组、清理非 variadic 目标的旧入边并写入新四元组。失败时不修改 `graph`。

所有草稿边设置 `selectable: true`、`deletable: true`、`updatable: true`、`interactionWidth: 20`；只读预览中 `deletable` 和 `updatable` 为 `false`。

- [ ] **Step 5: 运行视图测试确认通过**

Run: `npx vitest run src/views/mining/__tests__/WorkflowEditorView.spec.ts --pool=threads --maxWorkers=1`

Expected: PASS。

### Task 5: 集成、全量验证与人工验收

**Files:**
- Modify if needed: `kb-ui/src/views/mining/WorkflowEditorView.vue`
- Verify: all files above

- [ ] **Step 1: 运行三个目标测试文件**

Run: `npx vitest run src/utils/__tests__/miningWorkflowGraph.spec.ts src/components/mining/workflow/__tests__/MiningWorkflowEditor.spec.ts src/views/mining/__tests__/WorkflowEditorView.spec.ts --pool=threads --maxWorkers=1`

Expected: all PASS。

- [ ] **Step 2: 运行全部前端测试**

Run: `npx vitest run --pool=threads --maxWorkers=1`

Expected: all PASS，0 failures。

- [ ] **Step 3: 运行生产构建**

Run: `npm run build`

Expected: `vue-tsc -b` 与 `vite build` 均以 exit code 0 完成。

- [ ] **Step 4: 浏览器人工验收**

1. 打开 `system-full-baseline` 草稿；
2. 确认五个目录分组、范围标签和颜色；
3. 删除本体归纳，确认本体审核从“当前必需”切换为“可选”；
4. 单击一条连接固定节点的边，确认右侧显示连接信息；
5. 点击删除并用撤销恢复；
6. 使用 Delete/Backspace 删除；
7. 拖动边端点到兼容端口并确认重连；
8. 拖到不兼容端口，确认原边保留；
9. 删除必需边后确认校验报错且发布被阻止；
10. 打开历史版本，确认边操作全部只读。

## 5. 非目标

- 不修改后端 Workflow JSON 结构；
- 不新增边级持久化权限字段；
- 不自动修复用户主动删除的边；
- 不改变 Workflow 发布版本不可变语义；
- 不改变资产持久化、审核、图谱写入和挖掘收尾的既有执行顺序；
- 不在本次改造中增加框选、多边批量删除或边标签编辑。

## 6. 验收门槛

只有以下条件同时满足才算完成：

1. 三类有效编辑状态由同一图规则函数产生；
2. 五个业务分组完整覆盖 Catalog 的 16 个内置算子，未知算子不会丢失；
3. 目录、节点、检查器和删除权限同步更新；
4. 所有草稿边均可发现地删除和重连，包括固定节点关联边；
5. 边删除和重连支持撤销/重做；
6. 键盘操作不影响输入控件；
7. 历史版本保持只读；
8. 全量测试和生产构建通过。
