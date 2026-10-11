"""个股行情入库：日 K、分钟 bar 与高低点时间（从 services 拆出，ARC-01）。

核心逻辑：
1. 获取日K线数据 → open, close, high, low, volume, turnover，计算差值和百分比；
2. 获取分钟K线数据 → 推算最高/最低点的精确时间；
3. 存入数据库（每股一个事务 + 批量 upsert，PERF-01）。

入库单位约定（DAT-03）：成交量统一为「股」，成交额统一为「元」，由 datasources 各
适配器在返回前换算。DailyQuote/MinuteBar.source 记录实际来源，MinuteBar.freq 记录
bar 周期。
"""

import logging
from datetime import datetime, time as dt_time, timedelta
from decimal import Decimal

import pandas as pd
from django.db import transaction
from django.utils import timezone

from .datasources import NAME_SOURCES, _try_sources
from .models import DailyQuote, MinuteBar

logger = logging.getLogger(__name__)


def _day_bounds(date_obj):
    """本地时区某日 [00:00, 次日 00:00) 的 aware 边界"""
    start = timezone.make_aware(datetime.combine(date_obj, dt_time.min))
    return start, start + timedelta(days=1)


def compute_high_low_times(bars):
    """由同一交易日的分钟 bar 求最高/最低点时间（并列取最早一根）。

    bars: 可迭代 (datetime, high, low)，须按时间正序。返回 (high_time, low_time,
    high, low)，无数据返回 None。供 fetch_minute_data 与 rebuild_high_low_times 共用。
    """
    high_bar = low_bar = None
    for dt, high, low in bars:
        if high is not None and (high_bar is None or high > high_bar[1]):
            high_bar = (dt, high)
        if low is not None and (low_bar is None or low < low_bar[1]):
            low_bar = (dt, low)
    if high_bar is None or low_bar is None:
        return None
    return high_bar[0], low_bar[0], high_bar[1], low_bar[1]


def fetch_stock_info(code):
    """获取股票基本信息（名称等），用于添加关注时自动补全"""
    source, name = _try_sources('fetch_stock_name', code, sources=NAME_SOURCES)
    if name:
        return name
    return code


def format_symbol(code):
    """格式化股票代码，AkShare 接口通常需要 6 位代码"""
    return code.zfill(6)


def _daily_start_date(stock, bootstrap_days=365, overlap_days=5):
    """
    日线起点：无历史 bootstrap；有历史则从最近一根往前重叠几天，连续补到今天。
    不再「固定近 60 天」以免最新 bar 很旧时中间留空洞。
    """
    latest = (
        DailyQuote.objects.filter(stock=stock)
        .order_by('-trade_date')
        .values_list('trade_date', flat=True)
        .first()
    )
    if latest is None:
        # 本地日期（timezone.now() 是 UTC，北京时间凌晨会落到前一天，DAT-18）
        return (timezone.localdate() - timedelta(days=bootstrap_days)).strftime('%Y%m%d')
    return (latest - timedelta(days=overlap_days)).strftime('%Y%m%d')


_DAILY_UPDATE_FIELDS = [
    'open_price', 'close_price', 'high_price', 'low_price',
    'open_close_diff', 'open_close_pct', 'high_low_diff', 'high_low_pct',
    'prev_close', 'change_diff', 'change_pct', 'volume', 'turnover', 'source', 'is_final',
]

# 日 K 视为最终（收盘）的时刻：15:00 收盘，留 10 分钟给数据源完成汇总（DAT-13）
_DAILY_FINAL_TIME = dt_time(15, 10)


def _is_final_bar(trade_date, now=None):
    """交易日早于今天，或今天已过 15:10，才算收盘后的最终日 K"""
    local = timezone.localtime(now or timezone.now())
    today = local.date()
    if trade_date < today:
        return True
    return trade_date == today and local.time() >= _DAILY_FINAL_TIME


# 复权基准变化判定容差（DAT-02）：相对 0.2%，绝对 0.006 元兜住库内 2 位小数的舍入误差
_ADJ_REL_TOL = 0.002
_ADJ_ABS_TOL = 0.006


def _adjust_basis_changed(stock, df, today=None):
    """比较重叠窗口新旧收盘价，返回偏差超出容差的交易日列表（升序）。

    前复权以最新价为基准回溯调整全部历史：除权除息后，同一历史交易日的收盘价
    会整体变化。只要重叠窗口内任一日偏差超出容差，就说明库内历史仍是旧基准，
    只做增量会在拼接点留下断层。当日（盘中收盘价本就会变）不参与比较。
    """
    today = today or timezone.localdate()
    new = {}
    for d, c in zip(df['日期'], df['收盘']):
        if pd.isna(c):
            continue
        day = pd.to_datetime(d).date()
        if day < today:
            new[day] = float(c)
    if not new:
        return []
    old = DailyQuote.objects.filter(stock=stock, trade_date__in=list(new)).values_list(
        'trade_date', 'close_price')
    changed = []
    for day, close in old:
        if not close:
            continue
        old_close = float(close)
        if abs(new[day] - old_close) > max(old_close * _ADJ_REL_TOL, _ADJ_ABS_TOL):
            changed.append(day)
    return sorted(changed)


def _full_history_start(stock, bootstrap_days=365):
    """全量重拉起点：库内最早一根与 bootstrap 起点取更早者，确保覆盖全部存量行"""
    boot = timezone.localdate() - timedelta(days=bootstrap_days)
    earliest = (
        DailyQuote.objects.filter(stock=stock)
        .order_by('trade_date')
        .values_list('trade_date', flat=True)
        .first()
    )
    return min(earliest, boot) if earliest else boot


def fetch_daily_data(stock, start_date=None, end_date=None, full=False, sources=None):
    """
    获取日K线数据并保存到 DailyQuote 表（多数据源自动切换）

    Args:
        stock: Stock 模型实例
        start_date: 开始日期 'YYYYMMDD'
        end_date: 结束日期 'YYYYMMDD'
        full: True 时从库内最早日期（至少一年）全量重拉并重算 prev_close/change_pct
        sources: 可选数据源列表（如存量修复时只用新浪单一源），默认 DATA_SOURCES

    增量拉取时会比对重叠窗口的新旧收盘价，判定复权基准已变化（除权除息）就
    自动改为全量重拉（DAT-02 短期方案）。
    """
    symbol = format_symbol(stock.code)

    if end_date is None:
        end_date = timezone.localdate().strftime('%Y%m%d')
    if full:
        start_date = _full_history_start(stock).strftime('%Y%m%d')
    elif start_date is None:
        start_date = _daily_start_date(stock)

    source, df = _try_sources('fetch_daily', symbol, start_date, end_date, sources=sources)

    if df is None or df.empty:
        # 上游故障已由 _try_sources 记 ERROR；这里多为空结果（无成交/冷却），只记 warning
        logger.warning("获取 %s 日K线失败：所有数据源均不可用", stock.code)
        return 0

    # 按日期正序排列，以便计算昨收价
    df = df.sort_values(by='日期').reset_index(drop=True)

    if not full:
        changed = _adjust_basis_changed(stock, df)
        if changed:
            # 增量结果不落库：若全量重拉失败，下次增量仍能再次检测到并重试
            logger.warning(
                "[%s] %s 复权基准已变化：重叠窗口 %s 个交易日"
                "收盘价与库内不一致（%s~%s），触发全量重拉",
                source.name, stock.code, len(changed), changed[0], changed[-1]
            )
            return fetch_daily_data(stock, end_date=end_date, full=True, sources=sources)
    # 同时查库获取该股票最早的日K记录之前的最后一条，作为 prev_close 兜底
    first_date = pd.to_datetime(df.iloc[0]['日期']).date() if len(df) > 0 else None
    db_prev_close = None
    if first_date:
        prev_q = DailyQuote.objects.filter(
            stock=stock, trade_date__lt=first_date
        ).order_by('-trade_date').first()
        if prev_q:
            db_prev_close = prev_q.close_price

    prev_close_val = db_prev_close
    now = timezone.now()
    objs = {}
    for _, row in df.iterrows():
        try:
            trade_date = pd.to_datetime(row['日期']).date()
            # 价格缺失（NaN）的行跳过：Decimal('nan') 不抛错会污染库，价格缺失的日K也无意义
            if not all(pd.notna(row[k]) for k in ('开盘', '收盘', '最高', '最低')):
                logger.warning("%s %s 日线价格缺失，跳过该行", stock.code, trade_date)
                continue
            open_price = Decimal(str(row['开盘']))
            close_price = Decimal(str(row['收盘']))
            high_price = Decimal(str(row['最高']))
            low_price = Decimal(str(row['最低']))
            volume = int(row['成交量']) if pd.notna(row.get('成交量')) else None
            turnover = (Decimal(str(row['成交额'])) if pd.notna(row.get('成交额'))
                        else None)

            open_close_diff = close_price - open_price
            open_close_pct = open_close_diff / open_price * 100 if open_price else Decimal('0')
            high_low_diff = high_price - low_price
            high_low_pct = high_low_diff / low_price * 100 if low_price else Decimal('0')

            # 相对昨收的涨跌
            prev_close = prev_close_val
            if prev_close is not None:
                change_diff = close_price - prev_close
                change_pct = change_diff / prev_close * 100
            else:
                change_diff = None
                change_pct = None

            # 同一日期重复出现时以后一条为准（bulk upsert 单条语句内不能重复键）
            objs[trade_date] = DailyQuote(
                stock=stock,
                trade_date=trade_date,
                open_price=open_price,
                close_price=close_price,
                high_price=high_price,
                low_price=low_price,
                open_close_diff=open_close_diff,
                open_close_pct=open_close_pct,
                high_low_diff=high_low_diff,
                high_low_pct=high_low_pct,
                prev_close=prev_close,
                change_diff=change_diff,
                change_pct=change_pct,
                volume=volume,
                turnover=turnover,
                source=source.name,
                is_final=_is_final_bar(trade_date, now),
            )
            # 当前条目的收盘价作为下一条的昨收
            prev_close_val = close_price
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            # 单行字段缺失/格式异常/昨收为 0（Decimal 除零）时跳过该行
            logger.warning("解析 %s 日K行数据失败: %s", stock.code, e)
            continue

    count = len(objs)
    if objs:
        # 每股一个事务 + 批量 upsert（PERF-01）；high_time/low_time 由分钟线维护，不覆盖
        with transaction.atomic():
            DailyQuote.objects.bulk_create(
                list(objs.values()), batch_size=500, update_conflicts=True,
                unique_fields=['stock', 'trade_date'],
                update_fields=_DAILY_UPDATE_FIELDS,
            )

    if full:
        stale = DailyQuote.objects.filter(
            stock=stock, trade_date__gte=min(objs), trade_date__lte=max(objs),
        ).exclude(trade_date__in=list(objs)).count() if objs else 0
        logger.warning(
            "[%s] %s 日线全量重拉 %s 条（%s~%s），prev_close/change_pct 已重算%s",
            source.name, stock.code, count, start_date, end_date,
            f"；区间内仍有 {stale} 条库内行源端未返回，保留旧值" if stale else '',
        )
    logger.info("[%s] 获取 %s 日K线数据 %s 条", source.name, stock.code, count)
    return count


# 价格 3 位小数（ETF/LOF 报价到 0.001 元，DAT-11）；成交额仍为 2 位
_PRICE_Q = Decimal('0.001')
_AMOUNT_Q = Decimal('0.01')
_MINUTE_VALUE_FIELDS = ['open', 'close', 'high', 'low', 'volume', 'turnover']
_MINUTE_UPDATE_FIELDS = _MINUTE_VALUE_FIELDS + ['source', 'freq']


def _dec2(v):
    """与成交额 DecimalField(decimal_places=2) 入库精度一致，便于和库内值比较"""
    return Decimal(str(v)).quantize(_AMOUNT_Q)


def _dec3(v):
    """与价格 DecimalField(decimal_places=3) 入库精度一致，便于和库内值比较"""
    return Decimal(str(v)).quantize(_PRICE_Q)


def _save_minute_bars(stock, df, date_obj, source_name='', freq=1):
    """把某交易日的分钟 bar 批量 upsert 进 MinuteBar，返回实际写入行数。

    - 一个 transaction.atomic 内完成，避免逐行独立提交长期占用 SQLite 写锁；
    - 先读出当日已有 bar，只写新增或数值有变化的 bar（盘中每轮通常只有几根）；
    - bulk_create(update_conflicts=True) 依赖 (stock, datetime) 唯一约束；
    - 同一交易日只保留一种周期（DAT-03）：写 1 分钟 bar 时清掉当日的 5 分钟 bar；
      当日已有 1 分钟 bar 时不再写入更粗的 5 分钟 bar，避免两种周期混在一张表里。
    """
    day_start, day_end = _day_bounds(date_obj)
    rows = {}
    skipped = 0
    for rec in df.to_dict('records'):
        t = rec.get('时间')
        if t is None or pd.isna(t):
            continue
        # 价格缺失（NaN）的行跳过：Decimal('nan') 不抛错会污染库
        if not all(pd.notna(rec.get(k)) for k in ('开盘', '收盘', '最高', '最低')):
            skipped += 1
            continue
        try:
            dt = timezone.make_aware(pd.Timestamp(t).to_pydatetime())
            vol = rec.get('成交量')
            amt = rec.get('成交额')
            rows[dt] = (
                _dec3(rec['开盘']), _dec3(rec['收盘']), _dec3(rec['最高']), _dec3(rec['最低']),
                int(vol) if vol is not None and pd.notna(vol) else 0,
                _dec2(amt) if amt is not None and pd.notna(amt) else None,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            logger.warning("解析 %s 分钟数据行失败: %s", stock.code, e)
    if skipped:
        logger.warning("%s %s 有 %s 根分钟 bar 价格缺失，已跳过", stock.code, date_obj, skipped)
    if not rows:
        return 0

    with transaction.atomic():
        day_qs = MinuteBar.objects.filter(stock=stock, datetime__gte=day_start, datetime__lt=day_end)
        if freq > 1 and day_qs.filter(freq__lt=freq).exists():
            logger.info("%s %s 已有更细周期分钟 bar，不写入 %s 分钟 bar", stock.code, date_obj, freq)
            return 0
        if day_qs.filter(freq__gt=freq).exists():
            removed, _ = day_qs.filter(freq__gt=freq).delete()
            logger.info("%s %s 用 %s 分钟 bar 替换 %s 根更粗周期 bar", stock.code, date_obj, freq, removed)
        existing = {
            r[0]: tuple(r[1:]) for r in day_qs.values_list(
                'datetime', *_MINUTE_VALUE_FIELDS, 'source')
        }
        objs = [
            MinuteBar(stock=stock, datetime=dt, open=v[0], close=v[1], high=v[2],
                      low=v[3], volume=v[4], turnover=v[5], source=source_name, freq=freq)
            for dt, v in sorted(rows.items()) if existing.get(dt) != v + (source_name,)
        ]
        if objs:
            MinuteBar.objects.bulk_create(
                objs, batch_size=500, update_conflicts=True,
                unique_fields=['stock', 'datetime'],
                update_fields=_MINUTE_UPDATE_FIELDS,
            )
    logger.debug("%s %s 分钟 bar 共 %s 根，写入 %s 根", stock.code, date_obj, len(rows), len(objs))
    return len(objs)


def fetch_minute_data(stock, trade_date=None):
    """
    获取1分钟K线数据，推算最高/最低点精确时间（多数据源自动切换）

    Args:
        stock: Stock 模型实例
        trade_date: 交易日期 'YYYY-MM-DD'，默认今天
    """
    symbol = format_symbol(stock.code)

    if trade_date is None:
        # 本地交易日（timezone.now() 是 UTC，凌晨会落到前一天）
        trade_date = timezone.localdate().strftime('%Y-%m-%d')

    start_dt = f"{trade_date} 09:30:00"
    end_dt = f"{trade_date} 15:00:00"

    source, df = _try_sources('fetch_minute', symbol, start_dt, end_dt)

    if df is None or df.empty:
        # 同 fetch_daily_data：上游故障已由 _try_sources 记 ERROR
        logger.warning("获取 %s 分钟K线失败：所有数据源均不可用", stock.code)
        return None

    # 纵深防御：求极值前再按交易日过滤一次，绝不拿别的交易日的 bar 求高低点
    date_obj = datetime.strptime(trade_date, '%Y-%m-%d').date()
    df = df.copy()
    df['时间'] = pd.to_datetime(df['时间'], errors='coerce')
    df = df[df['时间'].dt.date == date_obj].sort_values('时间').reset_index(drop=True)
    if df.empty:
        logger.warning("[%s] %s %s 无当日分钟数据", source.name, stock.code, trade_date)
        return None

    # 保存分钟数据：每股一个事务 + 批量 upsert，只写新增/变化的 bar（PERF-01）
    freq = getattr(source, 'minute_freq', 1)
    written = _save_minute_bars(stock, df, date_obj, source.name, freq)
    if freq > 1 and not written and MinuteBar.objects.filter(
            stock=stock, datetime__date=date_obj, freq__lt=freq).exists():
        # 粗周期兜底不覆盖已由 1 分钟 bar 算出的更精确高低点时间
        logger.info("[%s] %s %s 保留已有 1 分钟 bar 的高低点时间", source.name, stock.code, trade_date)
        return None

    # 推算最高/最低点精确时间（只用当日 bar，并列取最早一根）
    try:
        hl = compute_high_low_times(
            (timezone.make_aware(t.to_pydatetime()), float(h), float(lo))
            for t, h, lo in zip(df['时间'], df['最高'], df['最低'])
            if pd.notna(t) and pd.notna(h) and pd.notna(lo)
        )
        if hl is None:
            logger.warning("%s %s 分钟数据无有效价格，无法推算高低点", stock.code, trade_date)
            return None
        high_time, low_time, high_val, low_val = hl

        # 只更新两个字段，不再 get()+save() 全字段回写
        updated = DailyQuote.objects.filter(stock=stock, trade_date=date_obj).update(
            high_time=high_time, low_time=low_time,
        )
        if not updated:
            logger.warning(
                "%s %s 日K线数据不存在，无法更新高低点时间", stock.code, trade_date
            )

        logger.info(
            "[%s] %s %s "
            "最高点 %s @ %s, "
            "最低点 %s @ %s",
            source.name, stock.code, trade_date, high_val, high_time, low_val, low_time
        )

        return {
            'high_time': high_time,
            'low_time': low_time,
            'high_price': Decimal(str(high_val)),
            'low_price': Decimal(str(low_val)),
        }
    except Exception as e:
        # 边界：分钟 bar 已入库，推算/回写高低点失败只影响这两个字段，不让整次拉取失败
        logger.exception("推算 %s 高低点时间失败: %s", stock.code, e)
        return None
