import { test, mock } from 'node:test'
import assert from 'node:assert/strict'
import {
  confirmDialog, dismissToast, feedback, promptDialog, resolveDialog, toast,
} from '../../stores/feedback.js'

test('toast：加入列表、超时自动移除、最多保留 4 条（FEX-06）', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    feedback.toasts.splice(0)
    const id = toast('已保存')
    assert.equal(feedback.toasts.length, 1)
    assert.equal(feedback.toasts[0].type, 'info')
    mock.timers.tick(3000)
    assert.equal(feedback.toasts.length, 0)
    for (let i = 0; i < 6; i++) toast(`m${i}`, { timeout: 0 })
    assert.deepEqual(feedback.toasts.map(t => t.message), ['m2', 'm3', 'm4', 'm5'])
    dismissToast(feedback.toasts[0].id)
    assert.equal(feedback.toasts.length, 3)
    assert.equal(toast(''), null)
    assert.ok(id)
  } finally {
    feedback.toasts.splice(0)
    mock.timers.reset()
  }
})

test('confirmDialog / promptDialog：按顺序排队，结果回传给调用方', async () => {
  const p1 = confirmDialog({ title: '删除？', danger: true })
  const p2 = promptDialog({ title: '新分组名称', defaultValue: 'A' })
  assert.equal(feedback.dialog.kind, 'confirm')
  assert.equal(feedback.queue.length, 1)
  resolveDialog(true)
  assert.equal(await p1, true)
  assert.equal(feedback.dialog.kind, 'prompt')
  assert.equal(feedback.dialog.defaultValue, 'A')
  resolveDialog(null)
  assert.equal(await p2, null)
  assert.equal(feedback.dialog, null)
  resolveDialog(true) // 无对话框时不抛错
})
