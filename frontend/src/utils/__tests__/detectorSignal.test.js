import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  crowdingInfo,
  dimRoleText,
  isUnverified,
  liftText,
  nodeCells,
  POSITIONING_TEXT,
  releaseRowText,
  signalBadges,
  statusClass,
  statusText,
  trendStateText,
  trendStateTitle,
  UNVERIFIED_TEXT,
  vetoedText,
} from '../detectorSignal.js'

test('isUnverified：只有向上信号标「未经验证」', () => {
  assert.equal(isUnverified({ direction: 'up' }), true)
  assert.equal(isUnverified({ direction: 'down' }), false)
  assert.equal(isUnverified(null), false)
  assert.equal(UNVERIFIED_TEXT, '未经验证')
})

test('POSITIONING_TEXT：定位为趋势延续，不再说预判转折', () => {
  assert.match(POSITIONING_TEXT, /^趋势延续信号/)
  assert.match(POSITIONING_TEXT, /不是转折预判/)
  assert.doesNotMatch(POSITIONING_TEXT, /早期迹象|新阶段/)
})

test('trendStateText：处于下跌/上涨波段第 X 天（自 日期）；旧信号无附注返回空', () => {
  const ann = {
    trend: { direction: 'down', days: 12, since: '2026-09-20', confirmed: '2026-09-25' },
  }
  assert.equal(trendStateText(ann), '处于下跌波段第 12 天（自 2026-09-20）')
  assert.equal(
    trendStateText({ trend: { direction: 'up', days: 0, since: '2026-10-09' } }),
    '处于上涨波段第 0 天（自 2026-10-09）',
  )
  assert.equal(trendStateText({}), '')
  assert.equal(trendStateText(null), '')
  assert.match(trendStateTitle(ann), /2026-09-25 确认转向/)
  assert.doesNotMatch(trendStateTitle({ trend: { direction: 'up', days: 1, since: 'x' } }), /；/)
})

test('crowdingInfo：分位、池内样本与升级/否决原因', () => {
  const info = crowdingInfo({
    margin_crowding: {
      mb5_pct: 0.82,
      dtc_pct: 0.4,
      pool: 18,
      escalated: true,
      escalate_reason: '融资拥挤，严重度升一档',
    },
  })
  assert.equal(info.text, 'MB5 分位 0.82 · DTC 分位 0.40（池内 18 只）')
  assert.equal(info.escalated, true)
  assert.equal(info.vetoed, false)
  assert.equal(info.reason, '融资拥挤，严重度升一档')
  assert.equal(crowdingInfo({}), null)
})

test('liftText：解禁比例、首个日期与距今交易日', () => {
  const lift = {
    event: 'lift',
    ratio_total: 0.032,
    first_date: '2026-10-20',
    days_to: 7,
    horizon_days: 20,
  }
  assert.equal(liftText(lift), '20 个交易日内解禁 3.20% 流通股（首个 2026-10-20，7 个交易日后）')
  assert.equal(liftText(null), '')
})

test('signalBadges：向上未经验证、融资拥挤、日内追买、解禁；只有附注命中才出徽标', () => {
  const keys = (s) => signalBadges(s).map((b) => b.key)
  assert.deepEqual(keys({ direction: 'down', annotations: {} }), [])
  assert.deepEqual(keys({ direction: 'up' }), ['unverified'])
  assert.deepEqual(
    keys({
      direction: 'down',
      annotations: {
        margin_crowding: { mb5_pct: 0.9, escalated: true },
        intraday_reversal: { tag: '日内追买过热', id20_pct: 0.85 },
        lift_pressure: { event: 'lift', ratio_total: 0.05, first_date: '2026-10-20' },
      },
    }),
    ['crowding', 'intraday', 'lift'],
  )
  // 分位不高、未升级：不出「融资拥挤」徽标
  assert.deepEqual(
    keys({ direction: 'down', annotations: { margin_crowding: { mb5_pct: 0.3 } } }),
    [],
  )
  const lift = signalBadges({
    direction: 'down',
    annotations: { lift_pressure: { event: 'lift', ratio_total: 0.05, first_date: '2026-10-20' } },
  })[0]
  assert.equal(lift.text, '解禁 5.0%')
})

test('vetoedText 与 dimRoleText', () => {
  assert.equal(
    vetoedText({
      target_name: '浦发银行',
      direction: 'up',
      reason: '融资拥挤，已否决：MB5 分位 0.91 ≥ 0.70',
    }),
    '浦发银行 向上信号已否决：融资拥挤，已否决：MB5 分位 0.91 ≥ 0.70',
  )
  assert.equal(dimRoleText({ vote_enabled: false, role: 'modifier' }), '只否决/升级')
  assert.equal(dimRoleText({ vote_enabled: false, role: 'event' }), '事件提示')
  assert.equal(dimRoleText({ vote_enabled: false, role: 'display' }), '只展示')
  assert.equal(dimRoleText({ vote_enabled: true }), '')
  assert.equal(dimRoleText({}), '')
})

test('statusText：5 日回看判未兑现但不足 10 个交易日时只显示观察期', () => {
  assert.equal(statusText({ status: 'missed', elapsed_days: 6 }), '观察期')
  assert.equal(statusClass({ status: 'missed', elapsed_days: 6 }), 'watching')
  assert.equal(statusText({ status: 'missed', elapsed_days: 10 }), '未兑现')
  assert.equal(statusClass({ status: 'missed', elapsed_days: 12 }), 'missed')
  // 旧接口没有 elapsed_days：照旧显示
  assert.equal(statusText({ status: 'missed' }), '未兑现')
  assert.equal(statusText({ status: 'active', elapsed_days: 2 }), '维持中')
})

test('nodeCells：20/60 日节点——观察期不判定、进行中、到期判方向', () => {
  const cells = nodeCells({
    elapsed: 25,
    no_fail_days: 10,
    nodes: [
      { horizon: 20, state: 'done', days: 20, dir_excess: 3.456, verdict: 'pass' },
      { horizon: 60, state: 'pending', days: 25, dir_excess: -1.2, verdict: null },
    ],
  })
  assert.deepEqual(cells, [
    { label: '20 日', state: '方向正确', value: '+3.46%', cls: 'ok' },
    { label: '60 日', state: '进行中（第 25 日）', value: '-1.20%', cls: '' },
  ])
  const early = nodeCells({
    no_fail_days: 10,
    nodes: [{ horizon: 20, state: 'observe', days: 4, dir_excess: -5, verdict: null }],
  })
  assert.equal(early[0].state, '观察期（前 10 日不判定）')
  assert.equal(early[0].cls, '')
  const fail = nodeCells({
    nodes: [{ horizon: 60, state: 'done', dir_excess: -2, verdict: 'fail' }],
  })
  assert.equal(fail[0].state, '方向错误')
  const noBench = nodeCells({
    nodes: [{ horizon: 20, state: 'done', dir_excess: null, verdict: null }],
  })
  assert.equal(noBench[0].value, '-')
  assert.deepEqual(nodeCells(null), [])
})

test('releaseRowText：个股页解禁排期一行', () => {
  assert.equal(
    releaseRowText({ date: '2026-10-20', share_type: '首发原股东限售股份', ratio: 0.012 }),
    '2026-10-20 首发原股东限售股份 1.20%',
  )
  assert.equal(releaseRowText({ date: '2026-10-20', ratio: null }), '2026-10-20 限售股 -')
  assert.equal(releaseRowText(null), '')
})
