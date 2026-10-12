// 探测器信号卡片的展示口径（det-v2）：定位文案、向上信号「未经验证」、趋势状态、
// 融资拥挤 / 解禁 / 日内追买提示、复盘 20/60 日节点。全部是纯函数，数据来自后端
// signal.annotations、signal.elapsed_days 与详情接口的 review_nodes，缺字段（旧信号行）
// 一律返回空，不报错、不编造。

import { STATUS_LABEL } from '../constants/labels.js'

const isNum = (v) => typeof v === 'number' && Number.isFinite(v)

/** 页面定位：第七轮验证信号出现在波段转向之后，是趋势延续而非转折预判 */
export const POSITIONING_TEXT =
  '趋势延续信号：成交量平稳时监测量价、资金与结构的渐进微变，标出已在进行中的趋势；' +
  '信号通常出现在波段转向之后，不是转折预判'

/** 向上信号：多轮回测都未证明有效，标「未经验证」并降权显示 */
export const UNVERIFIED_TEXT = '未经验证'
export const UNVERIFIED_TITLE =
  '向上信号在多轮前推验证中 20/60 日超额都不显著，仅供参考；向下信号是本探测器验证过的主要用法'

export function isUnverified(signal) {
  return signal?.direction === 'up'
}

/** 趋势状态行：「处于下跌波段第 X 天（自 YYYY-MM-DD）」；无数据返回 '' */
export function trendStateText(annotations) {
  const t = annotations?.trend
  if (!t || !isNum(t.days) || !t.since) return ''
  const side = t.direction === 'down' ? '下跌' : t.direction === 'up' ? '上涨' : ''
  if (!side) return ''
  return `处于${side}波段第 ${t.days} 天（自 ${t.since}）`
}

export function trendStateTitle(annotations) {
  const t = annotations?.trend
  if (!t) return ''
  const base = `${t.method || 'ZigZag 2×ATR20'}，只用信号日及以前的日线`
  return t.confirmed ? `${base}；${t.confirmed} 确认转向` : base
}

function pct(v) {
  return isNum(v) ? v.toFixed(2) : '-'
}

/** 融资拥挤度：分位说明 + 升级/否决原因；无附注返回 null */
export function crowdingInfo(annotations) {
  const m = annotations?.margin_crowding
  if (!m || !isNum(m.mb5_pct)) return null
  let text = `MB5 分位 ${pct(m.mb5_pct)}`
  if (isNum(m.dtc_pct)) text += ` · DTC 分位 ${pct(m.dtc_pct)}`
  if (isNum(m.pool)) text += `（全市场两融 ${m.pool} 只）`
  return {
    text,
    escalated: Boolean(m.escalated),
    vetoed: Boolean(m.vetoed),
    reason: m.veto_reason || m.escalate_reason || '',
  }
}

/** 解禁事件提示：「20 个交易日内解禁 3.20%（首个 2026-10-20，7 个交易日后）」 */
export function liftText(lift) {
  if (!lift || !isNum(lift.ratio_total) || !lift.first_date) return ''
  const horizon = isNum(lift.horizon_days) ? lift.horizon_days : 20
  const when = isNum(lift.days_to) ? `，${lift.days_to} 个交易日后` : ''
  return `${horizon} 个交易日内解禁 ${(lift.ratio_total * 100).toFixed(2)}% 流通股（首个 ${lift.first_date}${when}）`
}

/** 个股页解禁排期一行：「2026-10-20 首发原股东限售股份 1.20%」；比例缺失显示 '-' */
export function releaseRowText(u) {
  if (!u?.date) return ''
  const ratio = isNum(u.ratio) ? `${(u.ratio * 100).toFixed(2)}%` : '-'
  return `${u.date} ${u.share_type || '限售股'} ${ratio}`
}

/** 信号卡片徽标：未经验证 / 融资拥挤 / 日内追买过热 / 解禁 */
export function signalBadges(signal) {
  const s = signal || {}
  const ann = s.annotations || {}
  const out = []
  if (isUnverified(s)) {
    out.push({ key: 'unverified', text: UNVERIFIED_TEXT, cls: 'muted', title: UNVERIFIED_TITLE })
  }
  const crowd = crowdingInfo(ann)
  if (crowd?.escalated || crowd?.vetoed) {
    out.push({ key: 'crowding', text: '融资拥挤', cls: 'warn', title: crowd.reason })
  }
  if (ann.intraday_reversal?.tag) {
    out.push({
      key: 'intraday',
      text: ann.intraday_reversal.tag,
      cls: 'warn',
      title: `ID20 分位 ${pct(ann.intraday_reversal.id20_pct)}：近 20 日日内（开盘→收盘）累计涨幅居截面前 20%，第八轮验证此类股票后续 20–60 日倾向跑输（只展示，不参与判定）`,
    })
  }
  const lift = ann.lift_pressure
  if (lift?.event === 'lift') {
    out.push({
      key: 'lift',
      text: `解禁 ${(lift.ratio_total * 100).toFixed(1)}%`,
      cls: 'event',
      title: liftText(lift),
    })
  }
  return out
}

/** 列表状态：5 日回看判「未兑现」但信号后不足 10 个交易日时，只显示「观察期」 */
export const NO_FAIL_DAYS = 10

export function statusText(row) {
  const r = row || {}
  if (r.status === 'missed' && isNum(r.elapsed_days) && r.elapsed_days < NO_FAIL_DAYS) {
    return '观察期'
  }
  return STATUS_LABEL[r.status] || r.status || '-'
}

export function statusClass(row) {
  return statusText(row) === '观察期' ? 'watching' : row?.status || ''
}

function fmtPp(v) {
  if (!isNum(v)) return '-'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

/** 复盘节点 → 展示单元格：{label, state, value, cls} */
export function nodeCells(reviewNodes) {
  const nodes = reviewNodes?.nodes
  if (!Array.isArray(nodes)) return []
  const noFail = isNum(reviewNodes.no_fail_days) ? reviewNodes.no_fail_days : NO_FAIL_DAYS
  return nodes.map((n) => {
    let state
    let cls = ''
    if (n.state === 'done') {
      if (n.verdict === 'pass') {
        state = '方向正确'
        cls = 'ok'
      } else if (n.verdict === 'fail') {
        state = '方向错误'
        cls = 'bad'
      } else {
        state = '已到期（基准缺失）'
      }
    } else if (n.state === 'pending') {
      state = `进行中（第 ${n.days} 日）`
    } else {
      state = `观察期（前 ${noFail} 日不判定）`
    }
    return { label: `${n.horizon} 日`, state, value: fmtPp(n.dir_excess), cls }
  })
}

/** 否决记录一行文字 */
export function vetoedText(v) {
  if (!v) return ''
  const name = v.target_name || v.target_code || ''
  return `${name} ${v.direction === 'up' ? '向上' : '向下'}信号已否决：${v.reason || '-'}`
}

/** 维度芯片：只展示/只作否决的维度不显示投票方向 */
export function dimRoleText(dim) {
  if (dim?.vote_enabled === false) {
    if (dim.role === 'modifier') return '只否决/升级'
    if (dim.role === 'event') return '事件提示'
    return '只展示'
  }
  return ''
}
