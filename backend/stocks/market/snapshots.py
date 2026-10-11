"""市场日度快照：收盘后落库 + 多日趋势指标计算。

设计要点：
- 快照由收盘后预热任务（warm_post_close_lagging）末尾写入，幂等（同日重跑覆盖）；
- 多日指标按「窗口内快照齐全才算数」：缺任何一天就如实不可用，不拿 0/旧值补天；
- 板块 20 日轮动、ETF 份额 1/5/20 日变化由本模块供数（上游只给当日/原生 5d/10d）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dt_time

from django.db.models import Count, Max
from django.utils import timezone

from ..models import MarketDailySnapshot

logger = logging.getLogger(__name__)

# 快照落库失败重试无意义（预热任务每 30 分钟自然重试），单次失败只记日志
_SECTOR_SNAPSHOT_KINDS = {
    MarketDailySnapshot.KIND_INDUSTRY_FF: 'fetch_industry_fund_flow',
    MarketDailySnapshot.KIND_CONCEPT_FF: 'fetch_concept_fund_flow',
}
# turnover 自 2026-09 起纳入：为将来「成交额异动」信号积累历史；旧快照缺该键由读取方如实按 None 处理
_ETF_SNAPSHOT_FIELDS = ('code', 'name', 'share', 'market_cap', 'turnover')


# 大盘资金流口径：沪（上证 1.000001）+ 深（深证成指 0.399001）两 secid 同日合计，
# 单位元（东财 fflow f52-f56 原值）。快照落库与窗口聚合共用同一合并逻辑（DAT-05）。
MARKET_FF_SECIDS = ('1.000001', '0.399001')
MARKET_FF_FLOW_KEYS = ('main_net', 'small_net', 'mid_net', 'large_net', 'super_net')
MARKET_FF_SCOPE = 'sh+sz'
MARKET_FF_UNIT = '元'
MARKET_FF_SRC_FFLOW = 'em_fflow'


def merge_market_ff_rows(by_secid):
    """{secid: {date: row}} -> {date: 合计行}；仅沪深两边都有、且同日齐全的日期参与合计。

    单一市场缺失（某 secid 整体失败或某日缺行）时该日不出现在结果里——
    不把单市场数值冒充两市合计。某字段两边都为 None 时合计为 None（不拿 0 补）。
    """
    if set(by_secid) != set(MARKET_FF_SECIDS):
        return {}
    sides = [by_secid[secid] for secid in MARKET_FF_SECIDS]
    dates = set(sides[0]) & set(sides[1])
    merged_rows = {}
    for date_str in sorted(dates):
        merged = {'date': date_str}
        for key in MARKET_FF_FLOW_KEYS:
            vals = [side[date_str].get(key) for side in sides]
            merged[key] = None if all(v is None for v in vals) else round(sum(v or 0 for v in vals), 2)
        merged.update({'src': MARKET_FF_SRC_FFLOW, 'unit': MARKET_FF_UNIT, 'scope': MARKET_FF_SCOPE})
        merged_rows[date_str] = merged
    return merged_rows


def fetch_market_ff_merged_rows():
    """抓取沪深两 secid 的 fflow 日线并按同日合并；返回 ({date: 合计行}, [错误信息])。

    push2his 被掐时 fetch_flow_klines 退 push2delay（当日一根），两边各一根照样可合并；
    任一 secid 失败则整体返回空（错误列表注明哪一边），由调用方决定是否用快照兜底。
    """
    from .etf_flow import fetch_flow_klines, parse_flow_kline  # 延迟导入：便于测试打桩

    by_secid, errors = {}, []
    for secid in MARKET_FF_SECIDS:
        try:
            klines = fetch_flow_klines(secid)
        except Exception as e:  # noqa: BLE001  单边失败如实记录
            logger.warning("大盘资金流 %s 抓取失败: %s", secid, e)
            errors.append(f'{secid}: {e}')
            continue
        rows = {}
        for raw in klines:
            row = parse_flow_kline(raw)
            if row:
                rows[row['date']] = row
        if rows:
            by_secid[secid] = rows
        else:
            errors.append(f'{secid}: 无可解析数据')
    return merge_market_ff_rows(by_secid), errors


# 落库前 ETF 现货缓存可接受的最大年龄（秒），超过则 force 重拉（DAT-09）
_ETF_SPOT_MAX_AGE = 180
_UNKNOWN_DATES = ('', 'NaT', 'nan', 'None')


def _etf_spot_fresh_for(trade_date):
    """确保 ETF 现货缓存是 trade_date 收盘（15:00）后写入的新数据（DAT-09）。

    缓存过旧或不存在时 force 重拉；重拉失败时 _fetch_etf_spot_df 会退回旧缓存
    （stale_or），写入时间早于该交易日 15:00 就判为不新鲜，调用方不得落库。
    """
    from . import etf
    from ._cache import _cache_written_at

    close_ts = timezone.make_aware(datetime.combine(trade_date, dt_time(15, 0))).timestamp()
    ts = _cache_written_at('etf_spot_df')
    if ts is None or ts < close_ts or time.time() - ts > _ETF_SPOT_MAX_AGE:
        etf._fetch_etf_spot_df(force=True)
        ts = _cache_written_at('etf_spot_df')
    return ts is not None and ts >= close_ts


def _etf_items_on(items, trade_date):
    """剔除「数据日期」明确不等于 trade_date 的行；上游未给日期的行无法校验，保留"""
    want = trade_date.isoformat()
    kept = []
    dropped = 0
    for item in items:
        d = str(item.get('data_date') or '')[:10]
        if d not in _UNKNOWN_DATES and d != want:
            dropped += 1
            continue
        kept.append(item)
    if dropped:
        logger.warning("etf_share 快照剔除 %s 行数据日期不是 %s 的 ETF", dropped, want)
    return kept


def save_daily_snapshots(trade_date=None):
    """把当日板块资金流与 ETF 份额写入快照表；返回 {'kind': 条数} 与失败项。

    幂等：同一 (kind, trade_date) 重跑覆盖。数据不可用时该 kind 当天没有行，
    不写空快照（保证多日指标的「齐全才算数」不被空行污染）。
    """
    from . import etf  # 延迟导入避免与 etf.py 的快照读路径成环

    trade_date = trade_date or timezone.localdate()
    # 交易日守卫：非交易日（周末/节假日手动触发）落库会产生「最新快照不是最近
    # 已完成交易日」的脏行，卡死所有窗口指标的时效门槛。日历不可用时不拦截。
    try:
        from ..trading_calendar import is_trading_day

        if is_trading_day(trade_date) is False:
            logger.info("%s 非交易日，跳过快照落库", trade_date)
            return {}
    except Exception:
        # 日历判定意外失败时不拦截（与「日历不可用不拦截」同口径），留痕
        logger.warning("%s 交易日守卫判定失败，不拦截快照落库", trade_date, exc_info=True)

    # 15:30 守卫：交易日收盘前写「当日」快照，会把上一交易日的盘面数据标成
    # 今天（2026-09-07 凌晨手动触发踩坑），与 _expected_latest_date 的时效语义一致
    try:
        now = timezone.localtime()
        if trade_date == now.date() and now.time() < dt_time(15, 30):
            logger.info("%s 未到 15:30 收盘，跳过当日快照落库", trade_date)
            return {}
    except Exception:
        logger.warning("15:30 守卫判定失败，不拦截快照落库", exc_info=True)

    saved = {}

    for kind, func_name in _SECTOR_SNAPSHOT_KINDS.items():
        try:
            from . import sectors
            items = getattr(sectors, func_name)(period='day')
            payload = [
                {'name': item['name'], 'net': item['net'], 'change_pct': item.get('change_pct')}
                for item in items if item.get('name')
            ]
            if payload:
                MarketDailySnapshot.objects.update_or_create(
                    kind=kind, trade_date=trade_date, defaults={'payload': payload},
                )
                saved[kind] = len(payload)
            else:
                logger.warning("%s 快照当日无数据，不写行（%s）", kind, trade_date)
        except Exception as e:  # 边界：单个 kind 失败不影响其余快照
            logger.exception("%s 快照落库失败: %s", kind, e)

    try:
        # 东财现货须为该交易日收盘后的新数据，否则旧数据会被标成当日（DAT-09）；
        # 不新鲜时东财部分不写，仍可用上交所官方份额补齐沪市
        if _etf_spot_fresh_for(trade_date):
            em_items = _etf_items_on(etf._normalized_etf_items(), trade_date)
        else:
            logger.warning("ETF 现货缓存早于 %s 15:00（重拉失败），东财份额不落库", trade_date)
            em_items = []
        rows = [
            {field: item.get(field) for field in _ETF_SNAPSHOT_FIELDS}
            for item in em_items
            if item.get('code') and item.get('share') is not None
        ]
        rows = _prefer_sse_share_rows(rows, trade_date)
        if rows:
            MarketDailySnapshot.objects.update_or_create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE,
                trade_date=trade_date,
                defaults={'payload': rows},
            )
            saved[MarketDailySnapshot.KIND_ETF_SHARE] = len(rows)
            _clear_etf_share_memo()
        else:
            logger.warning("etf_share 快照当日无数据，不写行（%s）", trade_date)
    except Exception as e:  # 边界：etf_share 失败不影响其余快照
        logger.exception("etf_share 快照落库失败: %s", e)

    # 大盘主力资金流：自采（push2his 全史 / push2delay 当日一根），沪深两 secid
    # 同日口径合并；逐行 upsert——push2his 恢复时自动回补历史。单一市场缺失时
    # 该日不写（不把单市场合计冒充两市）。
    # 只 upsert 库里缺失或数值有变化的日期（PERF-07）：push2his 可用时返回全史，
    # 每 30 分钟对全史逐行 upsert 是纯写放大
    try:
        merged_rows, _errors = fetch_market_ff_merged_rows()
        existing = {
            row.trade_date.isoformat(): row.payload
            for row in MarketDailySnapshot.objects.filter(
                kind=MarketDailySnapshot.KIND_MARKET_FF, trade_date__in=list(merged_rows),
            )
        }
        written = 0
        for date_str in sorted(merged_rows):
            merged = merged_rows[date_str]
            if existing.get(date_str) == [merged]:
                continue
            MarketDailySnapshot.objects.update_or_create(
                kind=MarketDailySnapshot.KIND_MARKET_FF,
                trade_date=date_str,
                defaults={'payload': [merged]},
            )
            written += 1
        if written:
            saved[MarketDailySnapshot.KIND_MARKET_FF] = written
        elif merged_rows:
            logger.debug("market_ff 快照 %s 天均无变化，不重写（%s）", len(merged_rows), trade_date)
        else:
            logger.warning("market_ff 快照无沪深同日齐全的数据，不写行（%s）", trade_date)
    except Exception as e:  # 边界：market_ff 失败不影响已写入的其余快照
        logger.exception("market_ff 快照落库失败: %s", e)

    return saved


# 深市两融按日探测的候选天数上界：正常每交易日披露，取 8 天足以覆盖
# 节假日空档；已写过的日期会被跳过，稳态下每天只需探测 1-2 天。
_MARGIN_PROBE_CANDIDATES = 8


def margin_snapshot_row(day, sh_row, sz_row):
    """两融快照行（亿元）：沪深同日合计；rz=融资余额，探测器 margin_north 用它（#9）。

    融资余额只有两市都给出时才合计，单边缺失为 None——旧实现按 0 相加，会把
    沪市单边冒充两市合计。total（融资融券余额）保留给其他展示用途。
    """
    sh_rz, sz_rz = sh_row.get('rz'), sz_row.get('rz')
    return {
        'date': day.isoformat(),
        'total': round((sh_row.get('total') or 0.0) + (sz_row.get('total') or 0.0), 2),
        'sh_total': sh_row.get('total'),
        'sz_total': sz_row.get('total'),
        'rz': round(sh_rz + sz_rz, 2) if sh_rz is not None and sz_rz is not None else None,
        'sh_rz': sh_rz,
        'sz_rz': sz_rz,
    }


def write_margin_snapshot(day, sh_row, sz_row):
    """按 (KIND_MARGIN, day) 幂等写入一行两融快照，返回写入的 payload 行。"""
    row = margin_snapshot_row(day, sh_row, sz_row)
    MarketDailySnapshot.objects.update_or_create(
        kind=MarketDailySnapshot.KIND_MARGIN, trade_date=day, defaults={'payload': [row]},
    )
    return row


def save_margin_snapshot():
    """融资余额（沪深合计口径）日度快照落库，探测器「融资余额渐进」维度供数。

    为什么不并入 save_daily_snapshots：两融为 T+1 披露且深市只能按日探测
    （深交所对未披露日期直接抛错），放进每 30 分钟一轮的收盘预热链会产生
    大量无效探测请求；探测器 17:15 的日度扫描每天调用一次即可（幂等覆盖）。
    为什么只写沪深同日齐全的行：口径纪律与 sentiment 两融卡片一致——
    不把单市场数值冒充合计、不混用不同日期的数据硬凑序列，某日深市未
    披露该日就没有行，读取方按「窗口齐全才算数」如实降级。
    """
    from .sentiment import _recent_day_candidates, _sh_margin_rows, _sz_margin_row

    try:
        sh = _sh_margin_rows()
    except Exception as e:  # noqa: BLE001 上游边界：akshare 异常类型不固定，本轮不写
        logger.warning("融资余额快照：沪市历史拉取失败: %s", e)
        return 0
    if not sh:
        return 0

    # 已落库的日期不再探测（沪市数据不变，重探纯浪费请求）
    existing = set(
        MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARGIN)
        .values_list('trade_date', flat=True)
    )
    written = 0
    for day in _recent_day_candidates(_MARGIN_PROBE_CANDIDATES):
        if day in existing:
            continue
        sh_row = sh.get(day.strftime('%Y%m%d'))
        if not sh_row:
            continue
        sz_row = _sz_margin_row(day)
        if not sz_row or sz_row.get('total') is None:
            # 当日深市未披露（T+1 属预期状态），不是错误，静默跳过
            continue
        write_margin_snapshot(day, sh_row, sz_row)
        written += 1
    if written:
        logger.info("融资余额快照新增 %s 行", written)
    return written


def verify_daily_snapshots(trade_date=None):
    """快照落库自检：各 kind 均应有当日非空行；缺了如实列出（供预热任务推送告警）。

    仅交易日 15:30 后有意义——之前/非交易日/查询异常返回 None（视为「无可校验」，
    不告警）。etf_share 为全市场千行级，行数骤减同样视为异常。
    """
    from ..trading_calendar import is_trading_day

    trade_date = trade_date or timezone.localdate()
    try:
        if is_trading_day(trade_date) is False:
            return None
        if trade_date == timezone.localdate() and timezone.localtime().time() < dt_time(15, 30):
            return None
    except Exception:
        logger.warning("快照自检的交易日判定失败，跳过自检", exc_info=True)
        return None

    min_rows = {
        MarketDailySnapshot.KIND_INDUSTRY_FF: 1,
        MarketDailySnapshot.KIND_CONCEPT_FF: 1,
        MarketDailySnapshot.KIND_MARKET_FF: 1,
        MarketDailySnapshot.KIND_ETF_SHARE: 100,
    }
    missing = []
    try:
        for kind, floor in min_rows.items():
            row = MarketDailySnapshot.objects.filter(kind=kind, trade_date=trade_date).first()
            if row is None or len(row.payload or []) < floor:
                missing.append(kind)
    except Exception:
        logger.warning("快照自检查库失败，跳过自检", exc_info=True)
        return None
    return missing


def market_ff_snapshot_rows(start_date=None, end_date=None):
    """读取大盘资金流日度快照（升序 items 列表）；查库异常按无快照降级。"""
    try:
        qs = MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARKET_FF)
        if start_date is not None:
            qs = qs.filter(trade_date__gte=start_date)
        if end_date is not None:
            qs = qs.filter(trade_date__lte=end_date)
        rows = []
        for snap in qs.order_by('trade_date'):
            for item in (snap.payload or []):
                if item.get('date'):
                    rows.append(item)
        return rows
    except Exception:
        logger.warning("读取大盘资金流快照失败，按无快照降级", exc_info=True)
        return []


# 量级巡检阈值：两市合计主力净流入（元）日常在 1e9~1e11 量级；绝对值不足 1e6 元
# 基本可判定是「亿元」口径（北向兜底源）误写进来的污染行（DAT-06）。
MARKET_FF_SUSPECT_ABS_LT = 1e6
_MARKET_FF_TRUSTED_SRCS = (MARKET_FF_SRC_FFLOW, 'eastmoney_market_fund_flow')


def audit_market_ff_snapshots(start_date=None, end_date=None):
    """巡检 market_ff 快照，返回疑似污染行列表（只读，不改库）。

    每项 {'trade_date', 'id', 'reasons': [...], 'main_net', 'src', 'unit'}；
    reasons 取值：
    - empty_payload：payload 为空或无 date；
    - main_net_missing：主力净流入为 None；
    - magnitude：0 < |main_net| < MARKET_FF_SUSPECT_ABS_LT（疑似亿元口径）；
    - untrusted_src / bad_unit：payload 自带的来源/单位标注与本表口径不符；
    - parts_all_none：主力有值但小/中/大/超大单全为 None（DS-02 撞名症状，仅提示）。
    """
    qs = MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARKET_FF)
    if start_date is not None:
        qs = qs.filter(trade_date__gte=start_date)
    if end_date is not None:
        qs = qs.filter(trade_date__lte=end_date)
    suspects = []
    for snap in qs.order_by('trade_date'):
        items = [i for i in (snap.payload or []) if isinstance(i, dict) and i.get('date')]
        item = items[0] if items else {}
        reasons = []
        main = item.get('main_net')
        if not items:
            reasons.append('empty_payload')
        elif main is None:
            reasons.append('main_net_missing')
        else:
            try:
                main = float(main)
            except (TypeError, ValueError):
                main = None
                reasons.append('main_net_missing')
            if main is not None and 0 < abs(main) < MARKET_FF_SUSPECT_ABS_LT:
                reasons.append('magnitude')
        src, unit = item.get('src'), item.get('unit')
        if src is not None and src not in _MARKET_FF_TRUSTED_SRCS:
            reasons.append('untrusted_src')
        if unit is not None and unit != MARKET_FF_UNIT:
            reasons.append('bad_unit')
        if items and main is not None and all(
            item.get(k) is None for k in MARKET_FF_FLOW_KEYS if k != 'main_net'
        ):
            reasons.append('parts_all_none')
        if reasons:
            suspects.append({
                'trade_date': snap.trade_date, 'id': snap.pk, 'reasons': reasons,
                'main_net': item.get('main_net'), 'src': src, 'unit': unit,
            })
    return suspects


def _recent_trading_days(n, end_date=None):
    """截止 end_date（含）往前数 n 个交易日（薄包装，实现见 trading_calendar，ARC-02）"""
    from .. import trading_calendar

    return trading_calendar.recent_trading_days(n, end_date=end_date)


def _latest_snapshot(kind):
    """最新一行快照（只取标量列，不反序列化整市场 payload，PERF-07）"""
    return (
        MarketDailySnapshot.objects.filter(kind=kind)
        .only('id', 'kind', 'trade_date', 'created_at', 'updated_at')
        .order_by('-trade_date').first()
    )


def _expected_latest_date():
    """最近一个已完成交易日（薄包装，实现见 trading_calendar，ARC-02）；日历不可用返回 None"""
    from .. import trading_calendar

    return trading_calendar.expected_latest_date()


def _window_payloads(kind, n):
    """最近 n 个交易日的快照（以最新快照日为窗口末日）。

    返回 (payloads 按日期升序, latest, 日期列表)；窗口不齐、最新快照不是最近已完成
    交易日（即已过期）或日历不可用时返回 (None, latest, None)。
    """
    latest = _latest_snapshot(kind)
    if latest is None:
        return None, None, None
    expected = _recent_trading_days(n, end_date=latest.trade_date)
    if expected is None:
        return None, latest, None
    expected_today = _expected_latest_date()
    if expected_today is not None and latest.trade_date != expected_today:
        return None, latest, None
    rows = MarketDailySnapshot.objects.filter(
        kind=kind, trade_date__gte=expected[0], trade_date__lte=expected[-1],
    )
    by_date = {row.trade_date: row.payload for row in rows}
    if any(day not in by_date for day in expected):
        return None, latest, None
    return [by_date[day] for day in expected], latest, expected


def sector_multiday_nets(kind, n):
    """板块 n 日净额合计：{name: net_sum}；窗口快照不齐返回 (None, covered_days)。

    按名字级别要求完整：某行业在窗口内缺任一天的行就不计入（回填可能只补到
    部分行业，不能让缺数据的行业被静默算少）。查库异常按「无快照」降级。
    """
    try:
        payloads, _latest, _dates = _window_payloads(kind, n)
        if payloads is None:
            covered = MarketDailySnapshot.objects.filter(kind=kind).count()
            return None, min(covered, n)
    except Exception:
        logger.warning("读取 %s 快照窗口失败，按无快照降级", kind, exc_info=True)
        return None, 0
    totals = {}
    seen_days = {}
    for payload in payloads:
        names_in_day = set()
        for row in payload:
            name = row.get('name')
            net = row.get('net')
            if not name or net is None:
                continue
            totals[name] = totals.get(name, 0.0) + net
            names_in_day.add(name)
        for name in names_in_day:
            seen_days[name] = seen_days.get(name, 0) + 1
    complete = {name: total for name, total in totals.items() if seen_days[name] == n}
    if not complete:
        return None, n
    return complete, n


# ETF 份额派生结果的进程内缓存（PERF-07）：etf_share 快照是整市场一行 JSON，
# share_change_map(1/5/20) 每次请求要反序列化约 30 个大 payload。结果按「快照版本」
# 缓存——版本由行数、最近更新时间、最新交易日与预期最新交易日组成，任一进程写入
# 快照（含同日覆盖）都会改变版本，其他进程下次读取时自动重算；只保留当前版本。
_etf_share_memo = {'version': None, 'values': {}}
_etf_share_memo_lock = threading.Lock()


def _etf_share_version():
    agg = MarketDailySnapshot.objects.filter(
        kind=MarketDailySnapshot.KIND_ETF_SHARE,
    ).aggregate(n=Count('id'), updated=Max('updated_at'), latest=Max('trade_date'))
    return (agg['n'], agg['updated'], agg['latest'], _expected_latest_date())


def _etf_share_memoized(name, compute):
    """按快照版本缓存 compute() 的结果；版本查询失败时直接计算（与原降级口径一致）"""
    try:
        version = _etf_share_version()
    except Exception:
        logger.warning("ETF 份额快照版本查询失败，直接计算", exc_info=True)
        return compute()
    with _etf_share_memo_lock:
        if _etf_share_memo['version'] != version:
            _etf_share_memo['version'] = version
            _etf_share_memo['values'] = {}
        if name in _etf_share_memo['values']:
            return _etf_share_memo['values'][name]
    result = compute()
    with _etf_share_memo_lock:
        if _etf_share_memo['version'] == version:
            _etf_share_memo['values'][name] = result
    return result


def _clear_etf_share_memo():
    with _etf_share_memo_lock:
        _etf_share_memo['version'] = None
        _etf_share_memo['values'] = {}


def share_change_map(n):
    """ETF 份额 n 日变化（按快照版本缓存，结果只读，PERF-07）。"""
    return _etf_share_memoized(('share_change', n), lambda: _share_change_map(n))


def _share_change_map(n):
    """ETF 份额 n 日变化：{code: latest_share - n个交易日前_share}；窗口不齐返回 (None, covered)。

    「对比 n 个交易日前」需要 n+1 个快照点（首尾各一）；按代码级别要求首点有值。
    查库异常一律按「无快照」降级（雷达/详情主流程不因快照问题失败）。
    """
    try:
        payloads, latest, _dates = _window_payloads(MarketDailySnapshot.KIND_ETF_SHARE, n + 1)
        covered = MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_ETF_SHARE,
        ).count()
    except Exception:
        logger.warning("读取 ETF 份额快照窗口失败，按无快照降级", exc_info=True)
        return None, 0
    if payloads is None:
        return None, min(covered, n)
    base = {row.get('code'): row.get('share') for row in payloads[0] if row.get('code')}
    changes = {}
    for row in payloads[-1]:
        code, share = row.get('code'), row.get('share')
        if not code or share is None or base.get(code) is None:
            continue
        changes[code] = share - base[code]
    return changes, n


def latest_pair_change():
    """最近两个快照日的逐 ETF 份额对比（按快照版本缓存，结果只读，PERF-07）。"""
    return _etf_share_memoized('latest_pair', _latest_pair_change)


def _latest_pair_change():
    """最近两个快照日的逐 ETF 份额对比（份额异动信号检测用）。

    返回 (changes, meta)：changes 为 {code: {name, share, chg, chg_pct, market_cap, turnover}}，
    meta 为 {'date', 'prev_date'}；窗口不齐/最新快照已过期/查库异常一律 (None, None) 降级。
    只统计有前一快照基期的 ETF（新上市首日无基期，跳过以免把建仓期当成申赎异动）；
    turnover 为新增字段，旧快照缺该键如实 None。
    """
    try:
        payloads, _latest, dates = _window_payloads(MarketDailySnapshot.KIND_ETF_SHARE, 2)
    except Exception:
        logger.warning("读取最近两日 ETF 份额快照失败，按无快照降级", exc_info=True)
        return None, None
    if payloads is None or not dates:
        return None, None
    prev_rows = {row.get('code'): row for row in payloads[0] if row.get('code')}
    changes = {}
    for row in payloads[-1]:
        code = row.get('code')
        base = prev_rows.get(code)
        if not code or base is None:
            continue
        share, base_share = row.get('share'), base.get('share')
        if share is None or base_share is None or base_share <= 0:
            continue
        changes[code] = {
            'name': row.get('name'),
            'share': share,
            'chg': share - base_share,
            'chg_pct': round((share / base_share - 1) * 100, 3),
            'market_cap': row.get('market_cap'),
            'turnover': row.get('turnover'),
        }
    return changes, {'date': dates[-1].isoformat(), 'prev_date': dates[0].isoformat()}


def _prefer_sse_share_rows(rows, trade_date):
    """沪市 ETF 份额以上交所官方日度文件为准（披露责任主体），东财 spot 兜底。

    上交所文件仅提供份额，故只覆盖/补齐沪市行的 share，市值与成交额保留东财
    口径；官方文件当日未出（披露时点不定）或拉取失败时整表沿用东财。
    """
    try:
        sse_rows = fetch_sse_share_rows(trade_date)
    except Exception as e:  # noqa: BLE001 边界：官方文件解析异常类型不固定，沿用东财
        logger.warning("上交所份额合并失败，etf_share 快照沿用东财: %s", e)
        return rows
    if not sse_rows:
        return rows
    if not rows:
        return sse_rows
    by_code = {r['code']: r for r in rows}
    for sse in sse_rows:
        row = by_code.get(sse['code'])
        if row is None:
            rows.append(sse)  # 东财缺份额的沪市 ETF 用官方份额补齐（市值/成交额如实 None）
        elif sse['share']:
            row['share'] = sse['share']
    return rows


def fetch_sse_share_rows(date_obj):
    """上交所官方 ETF 份额（任意历史日期可取，仅沪市）。

    返回快照行 [{code, name, share, market_cap, turnover}]；上游失败或该日无数据
    返回 []（调用方不写空快照）。market_cap/turnover 该源不提供，如实 None——
    份额类指标（share_change_map / 异动信号 / 份额曲线）不受影响。
    """
    from .etf import _to_float

    try:
        import akshare as ak

        df = ak.fund_etf_scale_sse(date=date_obj.strftime('%Y%m%d'))
    except Exception as e:  # noqa: BLE001 上游边界：akshare 异常类型不固定
        logger.warning("上交所 ETF 份额拉取失败（%s）: %s", date_obj, e)
        return []
    if df is None or df.empty:
        return []
    rows = []
    for _, row in df.iterrows():
        raw_code = str(row.get('基金代码', '')).strip()
        share = _to_float(row.get('基金份额'))
        if not raw_code or not share or share <= 0:
            continue
        rows.append({
            'code': raw_code.zfill(6),
            'name': str(row.get('基金简称', '')).strip(),
            'share': share,
            'market_cap': None,
            'turnover': None,
        })
    return rows


def share_history_series(code, start_date, end_date):
    """某 ETF 在 [start, end] 的逐日份额（本站快照，含上交所历史回填）。

    返回 [{'date', 'share'}] 按日期升序；该区间无快照返回 []；查库异常返回 None。
    """
    try:
        rows = MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_ETF_SHARE,
            trade_date__gte=start_date, trade_date__lte=end_date,
        ).order_by('trade_date').values_list('trade_date', 'payload')
        out = []
        for trade_date, payload in rows:
            for row in payload or []:
                if row.get('code') == code and row.get('share') is not None:
                    out.append({'date': trade_date.isoformat(), 'share': row['share']})
                    break
        return out
    except Exception:
        logger.warning("读取 %s 份额历史失败", code, exc_info=True)
        return None


def snapshot_meta(source, available, latest=None, message=''):
    """快照供数端点用的 meta（不走进程内缓存，状态只有 fresh/unavailable）。"""
    return {
        'available': bool(available),
        'source': source,
        'source_data_date': latest.trade_date.isoformat() if latest else None,
        'data_as_of': latest.trade_date.isoformat() if latest else None,
        'fetched_at': latest.created_at.isoformat(timespec='seconds') if latest else None,
        'cache_status': 'fresh' if available else 'unavailable',
        'disclaimer': '由本站收盘后日度快照计算；窗口内缺任一交易日即如实不可用。',
        **({'message': message} if message else {}),
    }
