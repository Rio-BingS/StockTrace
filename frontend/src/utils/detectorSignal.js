// 探测器信号卡片的展示口径（det-v2）：定位文案、向上信号「未经验证」、趋势状态、
// 融资拥挤 / 解禁 / 日内追买提示、复盘 20/60 日节点。全部是纯函数，数据来自后端
// signal.annotations、signal.elapsed_days 与详情接口的 review_nodes，缺字段（旧信号行）
// 一律返回空，不报错、不编造。

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
