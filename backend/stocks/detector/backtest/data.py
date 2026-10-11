"""回测数据加载 + 本地文件缓存。

缓存目录结构（与 detector-research/backtest/data 原型一致，可直接把原型目录当缓存用）：
    index/{code}.csv          指数日线 date,open,high,low,close,volume（新浪，与生产同源）
    stocks/{code}.csv         个股日线 date,open,high,low,close,volume,amount
                              （baostock adjustflag=2：前复权价 + 未复权量/额，与生产 DailyQuote 同口径）
    shares/{code}.json        流通股本变动历史 [[iso_date, 股数], ...]（新浪 getAmountBySymbol，生产同源）
    margin.csv                两融余额 date,sh,sz,total（元/元/亿元；akshare 宏观表沪+深）
    universe/hs300_{date}.csv 指定日期的沪深300成分 code,name（baostock）
    universe.csv              原型的抽样股票池 code,name,bs_code（无成分表时的离线回退）
    manifest.json             每个文件的来源、复权口径、拉取时间与日期范围

为什么不复用 DailyQuote：生产表只保留需要的窗口，取数链路默认先走东财（沙箱/部分
网络不可达）；回测需要按需整段拉取、可离线重放的独立缓存。

前复权价会随除权整体重新缩放，增量追加会出现断层：联网模式下文件缺失或 refresh=True
时整段重拉，不做增量拼接。所有维度只用比值，缩放不影响结果。

offline=True 时只读缓存：缺失的日线抛 CacheMiss（引擎跳过该标的并记入报告），
缺失的股本按生产语义返回 None（换手率维度回退成交量分位）。
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# 默认缓存目录（已加入 .gitignore）；可用 --cache-dir 或 settings.DETECTOR_BACKTEST_CACHE_DIR 覆盖
DEFAULT_CACHE_DIR = Path(settings.BASE_DIR) / '.cache' / 'detector_backtest'
# 联网拉取的默认起始日：给 70 根上下文 + 60 日噪声基线留足预热
DEFAULT_FETCH_START = '2015-01-01'


class CacheMiss(LookupError):
    """离线模式下缓存缺失（或联网拉取失败）。"""


def default_cache_dir():
    return Path(getattr(settings, 'DETECTOR_BACKTEST_CACHE_DIR', None) or DEFAULT_CACHE_DIR)


def _num(v):
    """CSV 字段 → float；空串/NaN → None。"""
    if v is None or v == '':
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def read_bars_csv(path, with_amount):
    """读日线 CSV → [{date, open, high, low, close, volume, amount}] 升序、日期去重。"""
    by_date = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            d = str(row.get('date') or '')[:10]
            if not d:
                continue
            by_date[d] = {
                'date': d,
                'open': _num(row.get('open')),
                'high': _num(row.get('high')),
                'low': _num(row.get('low')),
                'close': _num(row.get('close')),
                'volume': _num(row.get('volume')),
                'amount': _num(row.get('amount')) if with_amount else None,
            }
    return [by_date[d] for d in sorted(by_date)]


def write_bars_csv(path, items, with_amount):
    cols = ['date', 'open', 'high', 'low', 'close', 'volume'] + (['amount'] if with_amount else [])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(cols)
        for it in items:
            w.writerow([it.get(c) if it.get(c) is not None else '' for c in cols])
    os.replace(tmp, path)


class DataStore:
    """带文件缓存的回测数据源。fetchers 可注入（测试用假实现，绝不联网）。"""

    def __init__(self, cache_dir=None, offline=False, refresh=False,
                 fetch_start=DEFAULT_FETCH_START, fetch_end=None, fetchers=None):
        self.root = Path(cache_dir) if cache_dir else default_cache_dir()
        self.offline = offline
        self.refresh = refresh and not offline
        self.fetch_start = fetch_start
        self.fetch_end = fetch_end or datetime.now().strftime('%Y-%m-%d')
        self.fetchers = fetchers or NetworkFetchers()
        self.notes = []      # 数据层面的降级/回退说明，写进报告
        self.missing = []    # 缺失（离线或拉取失败）的文件
        self._manifest = None

    # ---------- 通用 ----------
    def path(self, *parts):
        return self.root.joinpath(*parts)

    def _need_fetch(self, p):
        if self.offline:
            return False
        return self.refresh or not p.exists()

    def _manifest_update(self, rel, **info):
        if self._manifest is None:
            mp = self.path('manifest.json')
            self._manifest = json.loads(mp.read_text(encoding='utf-8')) if mp.exists() else {}
        self._manifest[rel] = {**info, 'fetched_at': datetime.now().isoformat(timespec='seconds')}
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path('manifest.json.tmp')
        tmp.write_text(json.dumps(self._manifest, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.path('manifest.json'))

    def _bars(self, sub, code, with_amount, fetch, source):
        p = self.path(sub, f'{code}.csv')
        if self._need_fetch(p):
            try:
                items = fetch(code, self.fetch_start, self.fetch_end)
            except Exception as e:  # noqa: BLE001 上游边界：akshare/baostock 异常类型不固定，按缺失处理
                logger.warning("回测数据拉取失败 %s/%s: %s", sub, code, e)
                items = None
            if items:
                write_bars_csv(str(p), items, with_amount)
                self._manifest_update(f'{sub}/{code}.csv', source=source, rows=len(items),
                                      first=items[0]['date'], last=items[-1]['date'])
        if not p.exists():
            self.missing.append(f'{sub}/{code}.csv')
            raise CacheMiss(f'{sub}/{code}.csv 不在缓存中' + ('（--offline）' if self.offline else '（拉取失败）'))
        return read_bars_csv(p, with_amount)

    # ---------- 各类数据 ----------
    def index_bars(self, code):
        return self._bars('index', code, False, self.fetchers.index_bars, 'sina stock_zh_index_daily')

    def stock_bars(self, code):
        return self._bars('stocks', code, True, self.fetchers.stock_bars,
                          'baostock query_history_k_data_plus adjustflag=2')

    def share_history(self, code):
        """流通股本历史 [(date, 股数)]；缺失返回 None（与生产「股本数据缺失」同语义）。"""
        from datetime import date as _date

        p = self.path('shares', f'{code}.json')
        if self._need_fetch(p):
            try:
                hist = self.fetchers.share_history(code)
            except Exception as e:  # noqa: BLE001 上游边界：新浪接口偶发 SSL EOF/超时，按股本缺失处理
                logger.warning("回测流通股本拉取失败 %s: %s", code, e)
                hist = None
            if hist is not None:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps([[d.isoformat(), v] for d, v in hist]), encoding='utf-8')
                self._manifest_update(f'shares/{code}.json', source='sina getAmountBySymbol', rows=len(hist))
        if not p.exists():
            self.missing.append(f'shares/{code}.json')
            return None
        rows = json.loads(p.read_text(encoding='utf-8'))
        return [(_date.fromisoformat(d), float(v)) for d, v in rows] or None

    def margin_rows(self):
        """两融余额 [(iso_date, total亿元)] 升序；剔除单边为 0 的坏行（源数据如 2024-08-08 深市=0）。"""
        p = self.path('margin.csv')
        if self._need_fetch(p):
            try:
                rows = self.fetchers.margin_rows()
            except Exception as e:  # noqa: BLE001 上游边界：akshare 异常类型不固定，按缺失处理
                logger.warning("回测两融数据拉取失败: %s", e)
                rows = None
            if rows:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(p, 'w', newline='', encoding='utf-8') as f:
                    w = csv.writer(f)
                    w.writerow(['date', 'sh', 'sz', 'total'])
                    w.writerows(rows)
                self._manifest_update('margin.csv', source='akshare macro_china_market_margin_sh/sz',
                                      rows=len(rows))
        if not p.exists():
            self.missing.append('margin.csv')
            self.notes.append('两融余额缓存缺失：margin_north 维度全程不可用')
            return []
        out = []
        with open(p, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sh, sz, total = _num(row.get('sh')), _num(row.get('sz')), _num(row.get('total'))
                if not sh or not sz or total is None or sh <= 0 or sz <= 0:
                    continue
                out.append((str(row['date'])[:10], total))
        out.sort()
        return out

    def hs300_members(self, on_date):
        """on_date 当日的沪深300成分 [(code, name)]，code 形如 sh600000。

        按时点取成分避免「用后来的成分倒推」的幸存者偏差。离线且无成分表时回退到
        原型的 universe.csv（那是 2023-10-09 成分的 40 只抽样，报告里会注明）。
        """
        p = self.path('universe', f'hs300_{on_date}.csv')
        if self._need_fetch(p):
            try:
                members = self.fetchers.hs300_members(on_date)
            except Exception as e:  # noqa: BLE001 上游边界：baostock 异常类型不固定，按缺失处理
                logger.warning("回测沪深300成分拉取失败 %s: %s", on_date, e)
                members = None
            if members:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, 'w', newline='', encoding='utf-8') as f:
                    w = csv.writer(f)
                    w.writerow(['code', 'name'])
                    w.writerows(members)
                self._manifest_update(f'universe/hs300_{on_date}.csv', source='baostock query_hs300_stocks',
                                      rows=len(members))
        if p.exists():
            with open(p, newline='', encoding='utf-8') as f:
                return [(r['code'], r.get('name') or r['code']) for r in csv.DictReader(f)]
        legacy = self.path('universe.csv')
        if legacy.exists():
            with open(legacy, newline='', encoding='utf-8') as f:
                rows = [(r['code'], r.get('name') or r['code']) for r in csv.DictReader(f)]
            self.notes.append(
                f'缺少 {on_date} 沪深300成分表，股票池回退为缓存中的 universe.csv（{len(rows)} 只，'
                '原型按 2023-10-09 成分抽样）'
            )
            return rows
        self.missing.append(f'universe/hs300_{on_date}.csv')
        raise CacheMiss(f'沪深300成分 {on_date} 不在缓存中')


class NetworkFetchers:
    """联网取数实现（数据源选择见 03_backtest_feasibility.md §2）。测试中从不实例化调用。"""

    @staticmethod
    def stock_bars(code, start, end):
        from ...baostock_session import bs_session

        bs_code = f'{code[:2]}.{code[2:]}'
        rows = []
        with bs_session() as bs:
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,open,high,low,close,volume,amount,tradestatus',
                start_date=start, end_date=end, frequency='d', adjustflag='2',
            )
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())
        items = []
        for d, o, h, lo, c, v, a, status in rows:
            # 停牌日不成 bar（生产 DailyQuote 同样没有停牌日的行）
            if status != '1' or not _num(v):
                continue
            items.append({'date': d, 'open': _num(o), 'high': _num(h), 'low': _num(lo),
                          'close': _num(c), 'volume': _num(v), 'amount': _num(a)})
        return items

    @staticmethod
    def index_bars(code, start, end):
        import akshare as ak

        df = ak.stock_zh_index_daily(symbol=code)
        items = []
        for r in df.itertuples(index=False):
            d = str(r.date)[:10]
            if start <= d <= end:
                items.append({'date': d, 'open': float(r.open), 'high': float(r.high), 'low': float(r.low),
                              'close': float(r.close), 'volume': float(r.volume), 'amount': None})
        return items

    @staticmethod
    def share_history(code):
        import requests

        from ...datasources.float_shares import SINA_SHARE_URL, parse_share_history

        resp = requests.get(
            SINA_SHARE_URL.format(symbol=code), params={'_': '20', 'symbol': code},
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'}, timeout=15,
        )
        resp.raise_for_status()
        return parse_share_history(resp.text)

    @staticmethod
    def margin_rows():
        import akshare as ak
        import pandas as pd

        sh = ak.macro_china_market_margin_sh()[['日期', '融资融券余额']].rename(columns={'融资融券余额': 'sh'})
        sz = ak.macro_china_market_margin_sz()[['日期', '融资融券余额']].rename(columns={'融资融券余额': 'sz'})
        m = sh.merge(sz, on='日期', how='inner')  # 沪深同日齐全才成行（与 save_margin_snapshot 同口径）
        m['date'] = pd.to_datetime(m['日期']).dt.strftime('%Y-%m-%d')
        return [(r.date, float(r.sh), float(r.sz), round((float(r.sh) + float(r.sz)) / 1e8, 2))
                for r in m.itertuples(index=False)]

    @staticmethod
    def hs300_members(on_date):
        from ...baostock_session import bs_session

        with bs_session() as bs:
            df = bs.query_hs300_stocks(date=on_date).get_data()
        return [(str(c).replace('.', ''), n) for c, n in df[['code', 'code_name']].values.tolist()]
