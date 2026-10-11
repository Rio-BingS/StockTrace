// 运行：npm test（Node 内置 node:test，无需额外依赖）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_PRIORITY,
  PARAM_STYLE_OPTIONS,
  normalizePriority,
  paramStyleLabel,
  providerForm,
} from '../../constants/aiProvider.js'

test('PARAM_STYLE_OPTIONS：取值与后端 AiProvider.param_style 一致', () => {
  assert.deepEqual(
    PARAM_STYLE_OPTIONS.map((o) => o.value),
    ['auto', 'standard', 'reasoning', 'openai_reasoning'],
  )
})

test('normalizePriority：空值取默认、取整并夹到 0–9999', () => {
  assert.equal(normalizePriority(''), DEFAULT_PRIORITY)
  assert.equal(normalizePriority(null), DEFAULT_PRIORITY)
  assert.equal(normalizePriority('abc'), DEFAULT_PRIORITY)
  assert.equal(normalizePriority('5'), 5)
  assert.equal(normalizePriority(2.6), 3)
  assert.equal(normalizePriority(-3), 0)
  assert.equal(normalizePriority(123456), 9999)
})

test('paramStyleLabel：未知值按自动显示', () => {
  assert.equal(paramStyleLabel('reasoning'), '推理')
  assert.equal(paramStyleLabel('openai_reasoning'), 'OpenAI 推理')
  assert.equal(paramStyleLabel(undefined), '自动')
})

test('providerForm：新增给默认值，编辑带出字段且不回填 Key', () => {
  assert.deepEqual(providerForm(), {
    id: null,
    name: '',
    base_url: '',
    model: '',
    api_key: '',
    is_enabled: true,
    priority: 100,
    param_style: 'auto',
  })
  const f = providerForm({
    id: 3,
    name: 'D',
    base_url: 'https://a.com',
    model: 'm',
    is_enabled: false,
    priority: 0,
    param_style: 'reasoning',
    api_key_masked: '****1234',
  })
  assert.equal(f.priority, 0)
  assert.equal(f.param_style, 'reasoning')
  assert.equal(f.is_enabled, false)
  assert.equal(f.api_key, '')
  // 旧后端没有这两个字段
  assert.equal(providerForm({ id: 1 }).priority, 100)
  assert.equal(providerForm({ id: 1 }).param_style, 'auto')
})
