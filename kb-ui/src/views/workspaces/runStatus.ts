/**
 * 工作台 run 生命周期口径（67号 Task 12 首查项修复沉淀）。
 *
 * 契约权威 = mining/kb/workspace_executor._TERMINAL_STATUSES 与
 * db_workspace_runs（012 DDL）：七值 queued/running/awaiting_review →
 * completed/partial/failed/cancelled。注意**没有 'succeeded'**——那是
 * 导出行（kb_workspace_exports）的词表，Task 11 曾把两者混淆导致成功
 * run 永不停轮询；此处集中定义并配 spec 防回归。
 */

/** 后端终态集（db_workspace_runs 生命周期：非终态三值 + 终态四值）。 */
export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  'completed', 'partial', 'failed', 'cancelled',
])

export function isTerminalRunStatus(status: string): boolean {
  return TERMINAL_RUN_STATUSES.has(status)
}

export const RUN_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  awaiting_review: '待审核',
  completed: '上次运行成功',
  partial: '上次运行部分完成',
  failed: '上次运行失败',
  cancelled: '上次运行已取消',
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? `上次运行 ${status}`
}
