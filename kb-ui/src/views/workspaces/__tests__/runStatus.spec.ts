import { describe, expect, it } from 'vitest'

import {
  RUN_STATUS_LABELS,
  TERMINAL_RUN_STATUSES,
  isTerminalRunStatus,
  runStatusLabel,
} from '../runStatus'

/**
 * run 终态口径（Task 12 首查项）：契约权威 = 后端
 * workspace_executor._TERMINAL_STATUSES = completed/partial/failed/
 * cancelled。'succeeded' 是**导出行**词表——run 永不发出；Task 11 曾把
 * 它写进前端终态集合，成功 run 永不停轮询。此 spec 锁死该回归。
 */
describe('runStatus', () => {
  it('treats exactly the four backend terminal statuses as terminal', () => {
    expect([...TERMINAL_RUN_STATUSES].sort()).toEqual(
      ['cancelled', 'completed', 'failed', 'partial'])
  })

  it('marks non-terminal run statuses (including the exports-only succeeded) as in-flight', () => {
    // 非终态三值 + 导出词表误植值——全部不得终止轮询
    for (const status of ['queued', 'running', 'awaiting_review', 'succeeded']) {
      expect(isTerminalRunStatus(status), status).toBe(false)
    }
  })

  it('marks every terminal status as terminal', () => {
    for (const status of ['completed', 'partial', 'failed', 'cancelled']) {
      expect(isTerminalRunStatus(status), status).toBe(true)
    }
  })

  it('labels all seven lifecycle statuses and falls back for unknowns', () => {
    expect(RUN_STATUS_LABELS).toEqual({
      queued: '排队中',
      running: '运行中',
      awaiting_review: '待审核',
      completed: '上次运行成功',
      partial: '上次运行部分完成',
      failed: '上次运行失败',
      cancelled: '上次运行已取消',
    })
    expect(runStatusLabel('mystery')).toBe('上次运行 mystery')
  })
})
