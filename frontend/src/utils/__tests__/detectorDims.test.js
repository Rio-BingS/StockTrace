import { test } from 'node:test'
import assert from 'node:assert/strict'
import { dimScopeText, dimThresholdText, disabledText, voteRuleText } from '../detectorDims.js'
import { fmtChanges } from '../detectorReview.js'

test('dimScopeText：按后端 targets 显示，融资余额只在沪深300、资金净额只在板块', () => {
  assert.equal(dimScopeText({ targets: ['industry', 'concept'] }), '板块')
  assert.equal(dimScopeText({ targets: ['index'], vote_codes: ['sh000300'] }), '沪深300')
  assert.equal(dimScopeText({ targets: ['index', 'stock'] }), '指数/自选股')
  assert.equal(dimScopeText({ targets: ['index', 'stock', 'industry', 'concept'] }), '全部')
  assert.equal(dimScopeText({ targets: [] }), '-')
  assert.equal(dimScopeText(null), '-')
})

test('dimThresholdText：价格类显示 z 区间，经验闸门类显示 min', () => {
  assert.equal(dimThresholdText({ params: { z_lo: 1.282, z_hi: 2.0, min: 0.08 } }), 'z 1.282~2')
  assert.equal(dimThresholdText({ params: { min: 0.02 } }), '0.02')
  assert.equal(dimThresholdText({ params: {} }), '-')
  assert.equal(dimThresholdText({}), '-')
})

test('disabledText：候选维度与无数据源分开', () => {
  assert.equal(disabledText({ disabled_kind: 'candidate' }), '候选（停用）')
  assert.equal(disabledText({ disabled_kind: 'no_source' }), '无数据源')
  assert.equal(disabledText({}), '停用')
})

test('voteRuleText：分组加权投票规则一句话；旧后端缺字段不显示', () => {
  const text = voteRuleText({
    min_dims: 2,
    min_weight: 1.5,
    oppose_ratio: 0.5,
    sector_signal_cap: 10,
    groups: {
      price: { role: 'direction', dims: ['vol_compress', 'ma_relation'] },
      funds: { role: 'direction', dims: ['big_order_net'] },
      volume: { role: 'strength', dims: ['vol_ratio_slope'] },
    },
  })
  assert.match(text, /至少 2 个信息组/)
  assert.match(text, /≥ 1\.5/)
  assert.match(text, /2 个方向组/)
  assert.match(text, /1 个量能组/)
  assert.match(text, /最多保留 10 个/)
  assert.match(
    voteRuleText({ min_dims: 2, min_weight: 1.5, oppose_ratio: 0.5, sector_signal_cap: 0 }),
    /不截断/,
  )
  assert.equal(voteRuleText({ min_dims: 2 }), '')
  assert.equal(voteRuleText(null), '')
})

test('fmtChanges：迁移重置整组参数时只说变化项数', () => {
  assert.equal(
    fmtChanges({
      name: ['大单净流入', '板块资金净额'],
      params: [
        { a: 1, b: 2 },
        { a: 1, b: 3, c: 4 },
      ],
    }),
    'name 大单净流入→板块资金净额、params 重置（2 项变化）',
  )
  assert.equal(fmtChanges({ weight: [1, 1.2] }), 'weight 1→1.2')
})
