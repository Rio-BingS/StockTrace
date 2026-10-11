"""P1 数据正确性与模型修复回归测试"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import services
from .models import AlertEvent, DailyQuote, MinuteBar, PriceAlert, ScreenerPreset, Stock, StockGroup
from .serializers import DailyQuoteSerializer, DashboardStockSerializer


def _aware(s):
    return timezone.make_aware(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))


class _NoSleep:
    def setUp(self):
        p = patch('stocks.services.time.sleep')
        p.start()
        self.addCleanup(p.stop)
        super().setUp()


class PricePrecisionTests(_NoSleep, TestCase):
    """DAT-11：ETF 三位小数报价存取不失真，成交额仍为 2 位。"""

    def setUp(self):
        super().setUp()
        self.stock = Stock.objects.create(code='510300', name='沪深300ETF')

    def test_daily_quote_keeps_three_decimals(self):
        df = pd.DataFrame([
            {'日期': '2026-09-01', '开盘': 1.230, '收盘': 1.234, '最高': 1.236, '最低': 1.229,
             '成交量': 100, '成交额': 123.456},
            {'日期': '2026-09-02', '开盘': 1.234, '收盘': 1.237, '最高': 1.238, '最低': 1.233,
             '成交量': 100, '成交额': 123.7},
        ])
        with patch('stocks.services._try_sources', return_value=(services.EastMoneySource(), df)):
            services.fetch_daily_data(self.stock, start_date='20260901', end_date='20260902')
        q = DailyQuote.objects.get(stock=self.stock, trade_date=date(2026, 9, 1))
        self.assertEqual(q.close_price, Decimal('1.234'))
        self.assertEqual(q.high_low_diff, Decimal('0.007'))
        self.assertEqual(q.turnover, Decimal('123.46'))
        q2 = DailyQuote.objects.get(stock=self.stock, trade_date=date(2026, 9, 2))
        self.assertEqual(q2.prev_close, Decimal('1.234'))
        self.assertEqual(q2.change_diff, Decimal('0.003'))
        self.assertEqual(DailyQuoteSerializer(q).data['close_price'], '1.234')

    def test_minute_bar_keeps_three_decimals(self):
        df = pd.DataFrame([{
            '时间': '2026-09-02 09:31:00', '开盘': 1.2341, '收盘': 1.2345,
            '最高': 1.236, '最低': 1.231, '成交量': 1000, '成交额': 1234.567,
        }])
        n = services._save_minute_bars(self.stock, df, date(2026, 9, 2), 'eastmoney')
        self.assertEqual(n, 1)
        bar = MinuteBar.objects.get(stock=self.stock)
        self.assertEqual(bar.open, Decimal('1.234'))
        self.assertEqual(bar.high, Decimal('1.236'))
        self.assertEqual(bar.turnover, Decimal('1234.57'))
        # 再写一遍不变的数据：与库内 3 位精度一致，不重复写
        with patch.object(MinuteBar.objects, 'bulk_create') as bc:
            services._save_minute_bars(self.stock, df, date(2026, 9, 2), 'eastmoney')
        bc.assert_not_called()

    def test_dashboard_serializer_three_decimals(self):
        data = DashboardStockSerializer({
            'id': 1, 'code': '510300', 'name': 'x', 'group_id': None, 'group_name': None,
            'cost_price': None, 'quantity': None, 'trade_date': None,
            'open_price': Decimal('1.234'), 'close_price': Decimal('1.235'),
            'high_price': None, 'low_price': None, 'high_time': None, 'low_time': None,
            'open_close_diff': Decimal('0.001'), 'open_close_pct': None,
            'high_low_diff': None, 'high_low_pct': None, 'prev_close': Decimal('1.230'),
            'change_diff': Decimal('0.005'), 'change_pct': None,
            'volume': None, 'turnover': None,
        }).data
        self.assertEqual(data['close_price'], '1.235')
        self.assertEqual(data['change_diff'], '0.005')


class ExchangeRouteTests(SimpleTestCase):
    """DAT-12：沪市 ETF（5 开头）与北交所前缀路由。"""

    def test_exchange(self):
        cases = {
            '600000': 'sh', '510300': 'sh', '588000': 'sh', '900901': 'sh',
            '000001': 'sz', '300750': 'sz', '159915': 'sz', '200002': 'sz',
            '920001': 'bj', '430047': 'bj', '830799': 'bj', '871981': 'bj',
        }
        for code, ex in cases.items():
            self.assertEqual(services._exchange(code), ex, code)
        self.assertEqual(services._to_shsz_prefix('510300'), 'sh510300')
        self.assertEqual(services._to_shsz_prefix('830799'), 'bj830799')

    def test_baostock_skips_bj(self):
        src = services.BaoStockSource()
        self.assertEqual(src._format_code('510300'), 'sh.510300')
        self.assertIsNone(src._format_code('830799'))
        with patch('baostock.login') as login:
            self.assertIsNone(src.fetch_daily('830799', '20260901', '20260902'))
            self.assertIsNone(src.fetch_minute('830799', '2026-09-02 09:30:00',
                                               '2026-09-02 15:00:00'))
            self.assertIsNone(src.fetch_stock_name('830799'))
        login.assert_not_called()


class BaoStockSessionTests(SimpleTestCase):
    """REL-06：多线程 BaoStock 调用串行化，不会互相登出。"""

    def test_sessions_do_not_interleave(self):
        import threading
        import time as _time

        state = {'logged_in': 0, 'max': 0, 'broken': 0}
        lock = threading.Lock()

        def login():
            with lock:
                state['logged_in'] += 1
                state['max'] = max(state['max'], state['logged_in'])

        def logout():
            with lock:
                state['logged_in'] -= 1

        class _RS:
            fields = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']

            def __init__(self):
                self.n = 0

            def next(self):
                _time.sleep(0.005)
                if state['logged_in'] != 1:
                    state['broken'] += 1
                self.n += 1
                return self.n <= 2

            def get_row_data(self):
                return ['2026-09-01', '1', '1', '1', '1', '100', '100']

        src = services.BaoStockSource()
        with patch('baostock.login', side_effect=login), \
                patch('baostock.logout', side_effect=logout), \
                patch('baostock.query_history_k_data_plus', side_effect=lambda *a, **k: _RS()):
            ts = [threading.Thread(target=src.fetch_daily, args=('600000', '20260901', '20260901'))
                  for _ in range(5)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(state['max'], 1)
        self.assertEqual(state['broken'], 0)
        self.assertEqual(state['logged_in'], 0)

    def test_logout_runs_and_lock_released_on_error(self):
        with patch('baostock.login'), patch('baostock.logout') as logout, \
                patch('baostock.query_stock_basic', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                services.BaoStockSource().fetch_stock_name('600000')
        logout.assert_called_once()
        self.assertFalse(services._bs_lock.locked())


class DailyIsFinalTests(_NoSleep, TestCase):
    """DAT-13：盘中拉到的当日日 K 标记为非最终，收盘后重拉置为最终。"""

    def setUp(self):
        super().setUp()
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        self.df = pd.DataFrame([
            {'日期': '2026-09-01', '开盘': 9.5, '收盘': 9.9, '最高': 10, '最低': 9.4,
             '成交量': 100, '成交额': 1000},
            {'日期': '2026-09-02', '开盘': 9.9, '收盘': 10.0, '最高': 10.2, '最低': 9.8,
             '成交量': 200, '成交额': 2000},
        ])

    def _fetch(self, now_str):
        now = _aware(now_str)
        with patch('stocks.services._try_sources',
                   return_value=(services.SinaSource(), self.df)), \
                patch('stocks.services.timezone.now', return_value=now), \
                patch('stocks.services.timezone.localdate', return_value=now.date()):
            services.fetch_daily_data(self.stock, start_date='20260901', end_date='20260902')

    def _final(self, d):
        return DailyQuote.objects.get(stock=self.stock, trade_date=d).is_final

    def test_intraday_today_not_final(self):
        self._fetch('2026-09-02 10:30:00')
        self.assertTrue(self._final(date(2026, 9, 1)))
        self.assertFalse(self._final(date(2026, 9, 2)))
        # 15:00 刚收盘、汇总尚未完成仍视为非最终
        self._fetch('2026-09-02 15:05:00')
        self.assertFalse(self._final(date(2026, 9, 2)))

    def test_after_close_refetch_marks_final(self):
        self._fetch('2026-09-02 10:30:00')
        self._fetch('2026-09-02 15:10:00')
        self.assertTrue(self._final(date(2026, 9, 2)))

    def test_next_day_fetch_final(self):
        self._fetch('2026-09-03 09:00:00')
        self.assertTrue(self._final(date(2026, 9, 2)))


class AlertEventUniqueTests(TestCase):
    """PERF-06：(alert, trade_date) 唯一，评估写入幂等；并发建名返回 409。"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        self.alert = PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('1'))

    def test_db_rejects_duplicate(self):
        AlertEvent.objects.create(alert=self.alert, stock=self.stock, message='a',
                                  trade_date=date(2026, 9, 2))
        with self.assertRaises(IntegrityError):
            AlertEvent.objects.create(alert=self.alert, stock=self.stock, message='b',
                                      trade_date=date(2026, 9, 2))

    def test_evaluate_one_idempotent_when_event_exists(self):
        from . import alerts
        AlertEvent.objects.create(alert=self.alert, stock=self.stock, message='a',
                                  trade_date=date(2026, 9, 2))
        with patch('stocks.alerts._current_price', return_value=(10.0, '现价')):
            self.assertIsNone(alerts._evaluate_one(self.alert, date(2026, 9, 2)))
            ev = alerts._evaluate_one(self.alert, date(2026, 9, 3))
        self.assertIsNotNone(ev)
        self.assertEqual(AlertEvent.objects.filter(alert=self.alert).count(), 2)

    def test_evaluate_one_integrity_error_returns_none(self):
        from . import alerts
        with patch('stocks.alerts._current_price', return_value=(10.0, '现价')), \
                patch.object(AlertEvent.objects, 'get_or_create', side_effect=IntegrityError):
            self.assertIsNone(alerts._evaluate_one(self.alert, date(2026, 9, 2)))

    def test_create_race_returns_409(self):
        spec = {'logic': 'all', 'conditions': [{'field': 'change_pct', 'op': 'gt', 'value': 3}]}
        with patch.object(ScreenerPreset.objects, 'create', side_effect=IntegrityError):
            r = self.client.post('/api/screener/presets/', {'name': 'p', 'spec': spec},
                                 content_type='application/json')
        self.assertEqual(r.status_code, 409)
        with patch.object(StockGroup.objects, 'create', side_effect=IntegrityError):
            r = self.client.post('/api/stock-groups/', {'name': 'g'},
                                 content_type='application/json')
        self.assertEqual(r.status_code, 409)
        g = StockGroup.objects.create(name='g1')
        with patch.object(StockGroup, 'save', side_effect=IntegrityError):
            r = self.client.patch(f'/api/stock-groups/{g.id}/', {'name': 'g2'},
                                  content_type='application/json')
        self.assertEqual(r.status_code, 409)
        with patch('stocks.serializers.StockSerializer.save', side_effect=IntegrityError), \
                patch('stocks.views.start_fetch_one') as fetch:
            r = self.client.post('/api/stocks/', {'code': '000001'},
                                 content_type='application/json')
        self.assertEqual(r.status_code, 409)
        fetch.assert_not_called()


class NegativeCacheTests(SimpleTestCase):
    """DAT-10：空结果只做 60s 负缓存、不覆盖好数据、不走收盘后保鲜；合法空集不计冷却。"""

    def setUp(self):
        import importlib

        from .market import _sources
        # market 包把 _cache（OrderedDict）导出到包级，需按模块名取 _cache 模块
        c = importlib.import_module('stocks.market._cache')
        self.c = c
        c._cache.clear()
        _sources._source_state.clear()
        self.addCleanup(c._cache.clear)
        self.addCleanup(_sources._source_state.clear)

    def test_empty_result_negative_cached_60s_only(self):
        c = self.c
        with patch('stocks.market._cache.time.time', return_value=1000.0):
            c._cache_set('k', {'available': False, 'items': []})
            self.assertEqual(c._cache_get('k', 600), {'available': False, 'items': []})
            self.assertEqual(c._stale_or('k', 'default'), 'default')
            self.assertEqual(c._cache_meta('k', 600, 's', False)['cache_status'], 'unavailable')
        # 61 秒后即失效，即使收盘后保鲜条件成立也不算新鲜
        with patch('stocks.market._cache.time.time', return_value=1061.0), \
                patch('stocks.market._cache._calendar_enabled', return_value=True), \
                patch('stocks.market._cache._data_changing_now', return_value=False), \
                patch('stocks.market._cache._last_session_close') as close:
            close.return_value = timezone.make_aware(datetime(1970, 1, 1))
            self.assertIsNone(c._cache_get('k', 600))

    def test_empty_does_not_overwrite_good_data(self):
        c = self.c
        with patch('stocks.market._cache.time.time', return_value=1000.0):
            c._cache_set('k', [1, 2])
        with patch('stocks.market._cache.time.time', return_value=1200.0):
            self.assertIsNone(c._cache_get('k', 60))  # 好数据已过期
            c._cache_set('k', [])                    # 重拉失败
            self.assertEqual(c._cache_get('k', 60), [1, 2])  # 60s 内沿用旧数据
            self.assertEqual(c._stale_or('k', None), [1, 2])
            self.assertEqual(c._cache_meta('k', 60, 's', True)['cache_status'], 'stale')
        with patch('stocks.market._cache.time.time', return_value=1300.0):
            self.assertIsNone(c._cache_get('k', 60))
            self.assertEqual(c._stale_or('k', None), [1, 2])
            c._cache_set('k', [3])
            self.assertEqual(c._cache_get('k', 60), [3])

    def test_safe_df_call_empty_ok(self):
        from .market import _sources
        empty = pd.DataFrame()
        for _ in range(3):
            r = _sources._safe_df_call(lambda: empty, source_name='legit', empty_ok=True)
            self.assertIsNotNone(r)
            self.assertTrue(r.empty)
        self.assertFalse(_sources._source_is_cool('legit'))
        for _ in range(2):
            self.assertIsNone(_sources._safe_df_call(lambda: empty, source_name='bad'))
        self.assertTrue(_sources._source_is_cool('bad'))
        # None 即使 empty_ok 也是失败
        self.assertIsNone(_sources._safe_df_call(lambda: None, source_name='n', empty_ok=True))
        r, name = _sources._first_ok([(lambda: empty, 'a')], empty_ok=True)
        self.assertEqual(name, 'a')
        self.assertEqual(_sources._first_ok([(lambda: empty, 'b')]), (None, None))

    def test_sentiment_empty_dt_pool_counts_zero(self):
        from .market import sentiment
        zt = pd.DataFrame({'代码': ['600000'], '连板数': [1]})
        # 交易日历桩：候选日推算不得触网（新浪日历 / BaoStock）
        with patch('stocks.market.sentiment.is_trading_day', return_value=True), \
                patch('stocks.market.sentiment.ak.stock_zt_pool_em', return_value=zt), \
                patch('stocks.market.sentiment.ak.stock_zt_pool_dtgc_em',
                      return_value=pd.DataFrame()), \
                patch('stocks.market.sentiment.ak.stock_zt_pool_zbgc_em',
                      return_value=pd.DataFrame()):
            data = sentiment.fetch_zt_sentiment(force=True)
        self.assertEqual(data['dt_count'], 0)
        self.assertEqual(data['zb_count'], 0)
        self.assertEqual(data['seal_rate'], 100.0)


class CacheConcurrencyTests(SimpleTestCase):
    """REL-07：命中即 LRU 提升、同 key single-flight、源状态表有上限。"""

    def setUp(self):
        import importlib

        from .market import _sources
        self.c = importlib.import_module('stocks.market._cache')
        self.s = _sources
        self.c._cache.clear()
        _sources._source_state.clear()
        self.addCleanup(self.c._cache.clear)
        self.addCleanup(_sources._source_state.clear)

    def test_hit_moves_to_end(self):
        c = self.c
        c._cache_set('hot', 1)
        for i in range(255):
            c._cache_set(f'k{i}', i)
        self.assertEqual(c._cache_get('hot', 60), 1)  # 命中提升为最近使用
        c._cache_set('new', 2)                         # 超限淘汰最旧的 k0
        self.assertIn('hot', c._cache)
        self.assertNotIn('k0', c._cache)

    def test_single_flight(self):
        import threading
        import time as _time

        c = self.c
        calls = []
        gate = threading.Event()

        def loader():
            calls.append(1)
            gate.wait(2)
            return {'v': 1}

        results = []
        ts = [threading.Thread(target=lambda: results.append(c._cache_get_or_load('sf', 60, loader)))
              for _ in range(8)]
        for t in ts:
            t.start()
        _time.sleep(0.1)
        gate.set()
        for t in ts:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{'v': 1}] * 8)
        self.assertEqual(c._inflight, {})

    def test_single_flight_loader_error_not_cached(self):
        c = self.c

        def boom():
            raise RuntimeError('x')

        with self.assertRaises(RuntimeError):
            c._cache_get_or_load('e', 60, boom)
        self.assertNotIn('e', c._cache)
        self.assertEqual(c._inflight, {})
        self.assertEqual(c._cache_get_or_load('e', 60, lambda: [1]), [1])

    def test_source_state_bounded(self):
        s = self.s
        s._source_mark_fail('keep', 'e')
        for i in range(300):
            s._source_mark_ok(f'src{i}')
            if i == 100:
                s._source_mark_fail('keep', 'e')  # 最近更新的不被淘汰
        self.assertLessEqual(len(s._source_state), 256)
        self.assertNotIn('src0', s._source_state)
        self.assertIn('src299', s._source_state)
        self.assertIn('keep', s._source_state)
        self.assertEqual(s._source_state['keep']['fails'], 2)


class SnapshotEtfFreshnessTests(TestCase):
    """DAT-09：ETF 份额快照只落该交易日收盘后的新数据，数据日期不符的行剔除。"""

    D = date(2026, 9, 4)

    def setUp(self):
        import importlib
        from .market import _sources
        self.c = importlib.import_module('stocks.market._cache')
        self.c._cache.clear()
        _sources._source_state.clear()
        self.addCleanup(self.c._cache.clear)
        self.addCleanup(_sources._source_state.clear)

    def _df(self, data_date):
        return pd.DataFrame([
            {'代码': '510300', '名称': '沪深300ETF', '最新份额': 100.0, '总市值': 400.0,
             '数据日期': data_date},
            {'代码': '159915', '名称': '创业板ETF', '最新份额': 50.0, '总市值': 100.0,
             '数据日期': '2026-09-03'},
            {'代码': '510500', '名称': '中证500ETF', '最新份额': 30.0, '总市值': 60.0,
             '数据日期': None},
        ])

    def _save(self, spot_side_effect):
        from .market import snapshots
        trading = patch('stocks.services.is_trading_day', return_value=True)
        with trading, patch('stocks.market.sectors.fetch_industry_fund_flow', return_value=[]), \
                patch('stocks.market.sectors.fetch_concept_fund_flow', return_value=[]), \
                patch('stocks.market.etf_flow.fetch_flow_klines', side_effect=RuntimeError('down')), \
                patch('akshare.fund_etf_scale_sse', return_value=pd.DataFrame()), \
                patch('akshare.fund_etf_spot_em', side_effect=spot_side_effect) as spot:
            saved = snapshots.save_daily_snapshots(trade_date=self.D)
        return saved, spot

    def _ts(self, hm):
        return timezone.make_aware(datetime.combine(self.D, datetime.strptime(hm, '%H:%M').time()))

    def test_force_refetch_and_drop_mismatched_date(self):
        from .models import MarketDailySnapshot
        with patch('stocks.market.snapshots.time.time', return_value=self._ts('16:00').timestamp()), \
                patch('stocks.market._cache.time.time', return_value=self._ts('16:00').timestamp()):
            saved, spot = self._save(lambda: self._df('2026-09-04'))
        spot.assert_called_once()  # 缓存为空 → 落库前 force 拉取
        row = MarketDailySnapshot.objects.get(kind=MarketDailySnapshot.KIND_ETF_SHARE,
                                              trade_date=self.D)
        self.assertEqual(sorted(r['code'] for r in row.payload), ['510300', '510500'])
        self.assertEqual(saved[MarketDailySnapshot.KIND_ETF_SHARE], 2)

    def test_stale_cache_before_close_not_written(self):
        from .models import MarketDailySnapshot
        # 上一轮 14:00 写入的缓存（盘中数据），本轮重拉失败 → 退回旧缓存，不能落库
        with patch('stocks.market._cache.time.time', return_value=self._ts('14:00').timestamp()):
            self.c._cache_set('etf_spot_df', self._df('2026-09-04'))
        with patch('stocks.market.snapshots.time.time', return_value=self._ts('16:00').timestamp()), \
                patch('stocks.market._cache.time.time', return_value=self._ts('16:00').timestamp()):
            saved, spot = self._save(RuntimeError('em down'))
        spot.assert_called_once()
        self.assertNotIn(MarketDailySnapshot.KIND_ETF_SHARE, saved)
        self.assertFalse(MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_ETF_SHARE).exists())

    def test_fresh_cache_after_close_no_refetch(self):
        from .models import MarketDailySnapshot
        with patch('stocks.market._cache.time.time', return_value=self._ts('15:50').timestamp()):
            self.c._cache_set('etf_spot_df', self._df('2026-09-04'))
        with patch('stocks.market.snapshots.time.time', return_value=self._ts('15:51').timestamp()), \
                patch('stocks.market._cache.time.time', return_value=self._ts('15:51').timestamp()):
            saved, spot = self._save(RuntimeError('should not be called'))
        spot.assert_not_called()
        self.assertEqual(saved[MarketDailySnapshot.KIND_ETF_SHARE], 2)


class BaoStockLoginFailureTests(SimpleTestCase):
    """BaoStock 不可达时 login 不抛异常只返回 error_code：须识别为失败并进入冷却，
    不能每个日期都重新登录一次。"""

    class _Result:
        def __init__(self, code, msg=''):
            self.error_code = code
            self.error_msg = msg

    def setUp(self):
        services._reset_trade_calendar_cache()
        self.addCleanup(services._reset_trade_calendar_cache)

    def test_login_error_code_enters_cooldown(self):
        with patch('akshare.tool_trade_date_hist_sina', side_effect=RuntimeError('down')), \
                patch('baostock.login', return_value=self._Result('10002007', '网络接收错误。')) as login, \
                patch('baostock.logout') as logout, \
                patch('baostock.query_trade_dates') as query:
            for d in range(5, 10):
                self.assertIsNone(services.is_trading_day(date(2026, 10, d)))
        self.assertEqual(login.call_count, 1)
        query.assert_not_called()
        logout.assert_not_called()  # 未登录成功不登出
        self.assertFalse(services._bs_lock.locked())

    def test_query_error_code_enters_cooldown(self):
        rs = self._Result('10004011', '查询失败')
        rs.next = lambda: False
        with patch('akshare.tool_trade_date_hist_sina', side_effect=RuntimeError('down')), \
                patch('baostock.login', return_value=self._Result('0')) as login, \
                patch('baostock.logout'), \
                patch('baostock.query_trade_dates', return_value=rs):
            for d in range(5, 8):
                self.assertIsNone(services.is_trading_day(date(2026, 10, d)))
        self.assertEqual(login.call_count, 1)

    def test_login_failure_makes_source_fail(self):
        with patch('baostock.login', return_value=self._Result('10002007')), \
                patch('baostock.query_history_k_data_plus') as query:
            with self.assertRaises(RuntimeError):
                services.BaoStockSource().fetch_daily('600000', '20260901', '20260901')
        query.assert_not_called()


class FinalBarReadersTests(TestCase):
    """DAT-13 收尾：探测器、选股、回看只读已收盘（is_final=True）的日线。"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def _bar(self, d, close, change_pct=Decimal('1'), is_final=True):
        return DailyQuote.objects.create(
            stock=self.stock, trade_date=d,
            open_price=Decimal('10'), close_price=Decimal(str(close)),
            high_price=Decimal('11'), low_price=Decimal('9'),
            open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
            high_low_diff=Decimal('2'), high_low_pct=Decimal('22.2222'),
            change_pct=change_pct, volume=100, turnover=Decimal('1000'),
            is_final=is_final,
        )

    @patch('stocks.ai.valuation.fetch_valuation_map', new=lambda codes: {})
    def test_screener_uses_latest_final_bar(self):
        from .ai.screener import run_screener
        self._bar(date(2026, 9, 3), 10, change_pct=Decimal('1'))
        self._bar(date(2026, 9, 4), 11, change_pct=Decimal('9'), is_final=False)
        hit = run_screener({'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 5},
        ]})
        self.assertEqual(hit, [])  # 盘中 9% 不得命中
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 0},
        ]})
        self.assertEqual(rows[0]['trade_date'], '2026-09-03')

    def test_screener_comment_rows_use_final_bar(self):
        from .ai.views import _comment_rows
        self._bar(date(2026, 9, 3), 10)
        self._bar(date(2026, 9, 4), 11, is_final=False)
        rows = _comment_rows(['600000'])
        self.assertEqual(rows[0]['trade_date'], date(2026, 9, 3))
