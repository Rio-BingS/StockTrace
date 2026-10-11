"""P3 data-detector 方向回归测试（DAT-17、ALG-13、DetectorSignal 约束、
东财 5d/10d 排行删除、detector 分页参数接入 common/params）。

全部离线：上游请求一律 mock。
"""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase


def _ths_html(names, page_info):
    trs = ''
    for i, name in enumerate(names):
        cells = [str(i + 1), name, '1000', '1.00%', '30', '14', '16.02', '50', '某股', '10.05%', '9.9']
        trs += '<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
    return (f'<html><body><span class="page_info">{page_info}</span>'
            f'<table>{trs}</table></body></html>')


def _resp(text):
    return type('R', (), {'text': text, 'raise_for_status': lambda self: None})()


class ThsStableSortTests(SimpleTestCase):
    """DAT-17：同花顺翻页按稳定字段排序，跨页重复去重并留痕。"""

    def _run(self, pages):
        from .market import sectors

        urls = []

        def fake_get(url, **kwargs):
            urls.append(url)
            return _resp(pages.pop(0))

        with patch('stocks.market.sectors._ths_token_headers', return_value={}), \
                patch('stocks.market.sectors.requests.get', side_effect=fake_get), \
                patch('stocks.market.sectors.time.sleep'):
            df = sectors._ths_fund_flow('concept')
        return df, urls

    def test_pages_sorted_by_board_name(self):
        _df, urls = self._run([_ths_html(['A', 'B'], '1/2'), _ths_html(['C'], '2/2')])
        self.assertEqual(len(urls), 2)
        for page, url in enumerate(urls, start=1):
            self.assertIn(f'field/tradename/order/asc/page/{page}/', url)
            self.assertNotIn('tradezdf', url)

    def test_duplicate_across_pages_is_dropped_and_logged(self):
        pages = [_ths_html(['A', 'B'], '1/2'), _ths_html(['B', 'C'], '2/2')]
        with self.assertLogs('stocks.market.sectors', level='WARNING') as logs:
            df, _urls = self._run(pages)
        self.assertEqual(list(df['概念']), ['A', 'B', 'C'])
        self.assertTrue(any('1 行重复' in m for m in logs.output))

    def test_short_middle_page_is_logged(self):
        pages = [_ths_html(['A', 'B'], '1/3'), _ths_html(['C'], '2/3'), _ths_html(['D'], '3/3')]
        with self.assertLogs('stocks.market.sectors', level='WARNING') as logs:
            df, _urls = self._run(pages)
        self.assertEqual(len(df), 4)
        self.assertTrue(any('疑似漏行' in m and '[2]' in m for m in logs.output))

    def test_clean_pages_log_nothing(self):
        from .market import sectors

        pages = [_ths_html(['A', 'B'], '1/2'), _ths_html(['C'], '2/2')]
        with patch.object(sectors.logger, 'warning') as warn:
            df, _urls = self._run(pages)
        self.assertEqual(len(df), 3)
        warn.assert_not_called()


class EmSectorRankRemovedTests(SimpleTestCase):
    """东财板块 5日/10日排行抓取已删除：多日口径只走日度快照（DAT-15 收尾）。"""

    def test_rank_helpers_gone(self):
        import inspect

        from . import market
        from .market import sectors

        self.assertFalse(hasattr(sectors, '_parse_sector_rank_table'))
        self.assertFalse(hasattr(sectors, '_SECTOR_RANK_INDICATORS'))
        self.assertFalse(hasattr(market, '_parse_sector_rank_table'))
        self.assertNotIn('stock_sector_fund_flow_rank', inspect.getsource(sectors))

    def test_multi_day_period_rejected_without_upstream_call(self):
        from .market import sectors

        for func in (sectors.fetch_industry_fund_flow, sectors.fetch_concept_fund_flow):
            for period in ('5d', '10d'):
                with self.subTest(func=func.__name__, period=period), \
                        patch('stocks.market.sectors._ths_fund_flow_df') as ths:
                    with self.assertRaises(ValueError):
                        func(period=period)
                    ths.assert_not_called()

    def test_day_period_still_uses_ths(self):
        from .market import sectors

        with patch('stocks.market.sectors._ths_fund_flow_df', return_value=None) as ths:
            self.assertEqual(sectors.fetch_industry_fund_flow(), [])
        ths.assert_called_once_with('industry', 'ths_fund_flow_industry')


def _legacy_page_size(raw):
    """接入 common/params 前 detector/views._page_size 的原实现（对照用）。"""
    if raw in (None, ''):
        return 50
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 50
    return max(1, min(value, 100))


def _legacy_positive_int(raw, default, maximum=None):
    """接入前 detector/views._positive_int 的原实现（对照用）。"""
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, maximum) if maximum else value


_PARAM_INPUTS = [None, '', '0', '-5', '1', '2', '50', '99', '100', '101', '1000', 'abc', '1.5', ' 3 ']


class DetectorParamsTests(SimpleTestCase):
    """detector/views 分页参数接入 common/params：逐值与旧实现一致。"""

    def test_page_size_matches_legacy_clamp(self):
        from .common.params import positive_int

        for raw in _PARAM_INPUTS:
            with self.subTest(raw=raw):
                params = {} if raw is None else {'page_size': raw}
                self.assertEqual(
                    positive_int(params, 'page_size', 50, 100, clamp=True), _legacy_page_size(raw),
                )

    def test_page_matches_legacy_fallback(self):
        from .common.params import positive_int

        for raw in _PARAM_INPUTS:
            with self.subTest(raw=raw):
                params = {} if raw is None else {'page': raw}
                self.assertEqual(positive_int(params, 'page', 1), _legacy_positive_int(raw, 1))

    def test_clamp_does_not_change_default_modes(self):
        from .common.params import positive_int
        from .errors import BadRequest

        self.assertEqual(positive_int({'n': '0'}, 'n', 7), 7)
        self.assertEqual(positive_int({'n': '0'}, 'n', 7, clamp=True), 1)
        with self.assertRaises(BadRequest):
            positive_int({'n': '0'}, 'n', 7, strict=True)
        # clamp 只管小于 1 的整数；strict 下非整数仍是 400
        self.assertEqual(positive_int({'n': '-3'}, 'n', 7, strict=True, clamp=True), 1)
        with self.assertRaises(BadRequest):
            positive_int({'n': 'x'}, 'n', 7, strict=True, clamp=True)

    def test_legacy_helpers_removed(self):
        from .detector import views as dv

        for name in ('_page_size', '_positive_int', '_parse_date'):
            self.assertFalse(hasattr(dv, name), name)


@patch('stocks.detector.views.expected_latest_date', lambda *a, **k: None)
class DetectorSignalsParamsApiTests(TestCase):
    """信号列表接口的分页/日期/all_dates 参数对外行为不变。"""

    def setUp(self):
        from datetime import date

        from .models import DetectorRun, DetectorSignal

        self.day = date(2026, 10, 9)
        DetectorRun.objects.create(trade_date=self.day, status='ok')
        for i in range(3):
            DetectorSignal.objects.create(
                target_kind='index', target_code=f'sh00000{i}', target_name=f'指数{i}',
                trade_date=self.day, direction='up', cycles=1, score=10 + i,
            )

    def _get(self, **params):
        from django.urls import reverse

        return self.client.get(reverse('detector-signals'), params)

    def test_page_size_zero_and_negative_clamp_to_one(self):
        for raw in ('0', '-5'):
            with self.subTest(raw=raw):
                body = self._get(page_size=raw).json()
                self.assertEqual(body['pagination']['page_size'], 1)
                self.assertEqual(len(body['items']), 1)

    def test_bad_page_size_and_page_fall_back(self):
        body = self._get(page_size='abc', page='0').json()
        self.assertEqual(body['pagination']['page_size'], 50)
        self.assertEqual(body['pagination']['page'], 1)
        body = self._get(page_size='1000').json()
        self.assertEqual(body['pagination']['page_size'], 100)

    def test_date_and_all_dates_parsing(self):
        self.assertEqual(self._get(date='2026/10/09').status_code, 400)
        self.assertEqual(self._get(date='2026-10-09').json()['summary']['total'], 3)
        # all_dates 只认小写 '1'/'true'：'TRUE' 视为未开启，按最新扫描日返回
        body = self._get(all_dates='TRUE').json()
        self.assertEqual(body['trade_date'], '2026-10-09')


def _signal_kwargs(**overrides):
    from datetime import date

    kw = {
        'target_kind': 'stock', 'target_code': '600000', 'target_name': '浦发银行',
        'trade_date': date(2026, 10, 9), 'direction': 'up', 'cycles': 1, 'score': 30.0,
        'severity': 'low', 'status': 'watching',
    }
    kw.update(overrides)
    return kw


class DetectorSignalConstraintTests(TestCase):
    """DetectorSignal 库级约束（PERF-10）：违规写入直接 IntegrityError，合规写入照常。"""

    def _assert_rejected(self, **overrides):
        from django.db import IntegrityError, transaction

        from .models import DetectorSignal

        with self.assertRaises(IntegrityError), transaction.atomic():
            DetectorSignal.objects.create(**_signal_kwargs(**overrides))

    def test_valid_rows_of_every_kind_accepted(self):
        from .models import DetectorSignal

        for kind, code in (('index', 'sh000300'), ('index', 'sz399006'), ('index', 'bj899050'),
                           ('stock', '000001'), ('industry', '半导体'), ('concept', 'AI视频')):
            DetectorSignal.objects.create(**_signal_kwargs(target_kind=kind, target_code=code))
        for code, score in (('600010', 0.0), ('600011', 100.0)):
            DetectorSignal.objects.create(**_signal_kwargs(target_code=code, score=score))
        self.assertEqual(DetectorSignal.objects.count(), 8)

    def test_enum_fields_rejected(self):
        for field, value in (('target_kind', 'etf'), ('direction', 'flat'),
                             ('severity', 'critical'), ('status', 'done')):
            with self.subTest(field=field):
                self._assert_rejected(**{field: value})

    def test_score_and_cycles_range_rejected(self):
        for overrides in ({'score': -0.1}, {'score': 100.5}, {'cycles': 0}):
            with self.subTest(**overrides):
                self._assert_rejected(**overrides)

    def test_target_code_format_rejected(self):
        for kind, code in (('index', '000300'), ('index', 'hk000300'), ('index', 'sh0003'),
                           ('stock', 'sh600000'), ('stock', '60000'), ('industry', ''), ('concept', '')):
            with self.subTest(kind=kind, code=code):
                self._assert_rejected(target_kind=kind, target_code=code)

    def test_upsert_signal_still_writes(self):
        from .detector.signals import upsert_signal

        data = _signal_kwargs()
        sig, created = upsert_signal(data, {})
        self.assertTrue(created)
        sig, created = upsert_signal({**data, 'score': 55.0, 'cycles': 2, 'status': 'active'}, {})
        self.assertFalse(created)
        self.assertEqual(sig.score, 55.0)


class DetectorSignalCleanMigrationTests(TransactionTestCase):
    """0018 迁移：加约束前把存量违规行修正或删除，迁移本身不失败。"""

    _before = ('stocks', '0017_aijob')
    _after = ('stocks', '0018_detectorsignal_check_constraints')

    def tearDown(self):
        from django.core.management import call_command

        call_command('migrate', 'stocks', verbosity=0)

    def _migrate(self, target):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([target])
        executor.loader.build_graph()
        return executor.loader.project_state([target]).apps

    def test_violations_fixed_or_deleted(self):
        import io
        from contextlib import redirect_stdout
        from datetime import date

        old_apps = self._migrate(self._before)
        Sig = old_apps.get_model('stocks', 'DetectorSignal')
        day = date(2026, 10, 9)
        base = {'target_name': '', 'trade_date': day, 'direction': 'up', 'cycles': 1,
                'score': 10.0, 'severity': 'low', 'status': 'watching', 'dimensions': {}}
        keep = Sig.objects.create(**{**base, 'target_kind': 'index', 'target_code': 'sh000300'})
        fix = Sig.objects.create(**{**base, 'target_kind': 'stock', 'target_code': '600000',
                                    'score': 120.0, 'cycles': 0, 'severity': 'x', 'status': 'y'})
        neg = Sig.objects.create(**{**base, 'target_kind': 'industry', 'target_code': '银行', 'score': -3.0})
        Sig.objects.create(**{**base, 'target_kind': 'etf', 'target_code': '510300'})
        Sig.objects.create(**{**base, 'target_kind': 'stock', 'target_code': 'sh600000'})
        Sig.objects.create(**{**base, 'target_kind': 'concept', 'target_code': ''})
        Sig.objects.create(**{**base, 'target_kind': 'index', 'target_code': 'sh000001', 'direction': 'flat'})

        out = io.StringIO()
        with redirect_stdout(out):
            new_apps = self._migrate(self._after)
        Sig = new_apps.get_model('stocks', 'DetectorSignal')
        self.assertEqual(sorted(Sig.objects.values_list('id', flat=True)), sorted([keep.id, fix.id, neg.id]))
        fixed = Sig.objects.get(id=fix.id)
        self.assertEqual((fixed.score, fixed.cycles, fixed.severity, fixed.status), (100.0, 1, 'low', 'watching'))
        self.assertEqual(Sig.objects.get(id=neg.id).score, 0.0)
        self.assertIn('修正 2 行，删除 4 行', out.getvalue())


_SINA_TEXT = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    'var KKE_ShareAmount_sh600519=([{"date":"2015-07-20","amount":125619.78},'
    '{"date":"2025-09-01","amount":125227.0215},{"date":"2025-09-01","amount":125300},'
    '{"date":"2026-05-28","amount":125008.1601}]);'
)


class FloatShareSourceTests(SimpleTestCase):
    """ALG-13：新浪流通股本历史的解析、对齐、缓存与熔断（上游全 mock）。"""

    def test_parse_converts_wan_to_shares_and_dedups_dates(self):
        from datetime import date

        from .datasources.float_shares import parse_share_history

        hist = parse_share_history(_SINA_TEXT)
        self.assertEqual([d for d, _ in hist], [date(2015, 7, 20), date(2025, 9, 1), date(2026, 5, 28)])
        self.assertAlmostEqual(hist[1][1], 125300 * 1e4)  # 同日多条取后一条
        self.assertEqual(round(hist[-1][1]), 1250081601)  # 与腾讯第 72 字段一致

    def test_parse_rejects_malformed(self):
        from .datasources.float_shares import parse_share_history

        for text in ('null', '<html>502</html>', 'var x=([{"date":"2026-01-01","amount":0}]);',
                     'var x=([{"day":"2026-01-01"}]);'):
            with self.subTest(text=text), self.assertRaises((ValueError, KeyError)):
                parse_share_history(text)

    def test_null_payload_means_no_data_not_failure(self):
        from django.test import override_settings

        from .datasources import float_shares

        text = "/*<script>location.href='//sina.com';</script>*/\nvar KKE_ShareAmount_sh510300=(null);"
        self.assertEqual(float_shares.parse_share_history(text), [])
        with override_settings(STOCKTRACE_FLOAT_SHARES_ENABLED=True), \
                patch('stocks.datasources.float_shares.requests.get', return_value=_resp(text)) as get:
            for code in ('510300', '510500', '159915', '510300'):
                self.assertIsNone(float_shares.fetch_float_share_history(code))
        # 无数据不计入熔断：3 只 ETF 各请求一次，重复的那只走缓存
        self.assertEqual(get.call_count, 3)
        self.assertEqual(float_shares._fail_state['until'], 0.0)

    def test_shares_on_dates_is_step_function(self):
        from datetime import date

        from .datasources.float_shares import shares_on_dates

        hist = [(date(2026, 1, 5), 100.0), (date(2026, 1, 8), 200.0)]
        got = shares_on_dates(hist, ['2026-01-02', '2026-01-05', '2026-01-07', '2026-01-08', '2026-02-01'])
        self.assertEqual(got, [None, 100.0, 100.0, 200.0, 200.0])
        self.assertEqual(shares_on_dates(None, ['2026-01-05']), [None])

    def test_disabled_by_runner_makes_no_request(self):
        from .datasources import float_shares

        with patch('stocks.datasources.float_shares.requests.get') as get:
            self.assertIsNone(float_shares.fetch_float_share_history('600519'))
        get.assert_not_called()

    def test_fetch_caches_and_uses_exchange_prefix(self):
        from django.test import override_settings

        from .datasources import float_shares

        with override_settings(STOCKTRACE_FLOAT_SHARES_ENABLED=True), \
                patch('stocks.datasources.float_shares.requests.get', return_value=_resp(_SINA_TEXT)) as get:
            first = float_shares.fetch_float_share_history('600519')
            second = float_shares.fetch_float_share_history('600519')
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)
        self.assertIn('KKE_ShareAmount_sh600519', get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs['params']['symbol'], 'sh600519')

    def test_failures_return_none_and_trip_breaker(self):
        from django.test import override_settings

        from .datasources import float_shares

        with override_settings(STOCKTRACE_FLOAT_SHARES_ENABLED=True), \
                patch('stocks.datasources.float_shares.requests.get', side_effect=OSError('down')) as get, \
                self.assertLogs('stocks.datasources.float_shares', level='WARNING'):
            for code in ('600000', '600001', '600002', '600003', '600000'):
                self.assertIsNone(float_shares.fetch_float_share_history(code))
        # 前 3 只各请求一次后熔断；第 4 只与重复的第 1 只都不再请求
        self.assertEqual(get.call_count, 3)


def _turnover_ctx(n=70, split_at=None, unlock_at=None, shares=True):
    """合成个股序列。split_at：10 转 10（前复权价减半、量与股本翻倍）；
    unlock_at：解禁（价格不变，流通股本翻倍，成交量跟着放大一倍）。"""
    import random

    from .tests_p2_detector import _price_ctx

    rng = random.Random(7)
    closes, vols, amounts, float_sh = [], [], [], []
    for i in range(n):
        price = 10.0 * (1 + rng.gauss(0, 0.01))
        vol = 1e6 * (1 + rng.gauss(0, 0.1))
        sh = 1e8
        if split_at is not None:
            if i < split_at:
                amounts.append(price * vol)
                price = price / 2
            else:
                price, vol, sh = price / 2, vol * 2, sh * 2
                amounts.append(price * vol)
        else:
            if unlock_at is not None and i >= unlock_at:
                vol, sh = vol * 2, sh * 2
            amounts.append(price * vol)
        closes.append(price)
        vols.append(vol)
        float_sh.append(sh)
    ctx = _price_ctx(vols, closes)
    ctx['amount'] = amounts
    ctx['float_shares'] = float_sh if shares else None
    return ctx


class TurnoverFloatShareTests(SimpleTestCase):
    """ALG-13：有流通股本时用真实换手率，缺失时如实标注。"""

    def _eval(self, ctx):
        from .detector.dimensions import eval_turnover_pct
        from .detector.registry import DIMENSION_MAP

        return eval_turnover_pct(ctx, dict(DIMENSION_MAP['turnover_pct']['defaults']))

    def test_split_with_share_data_stays_available(self):
        res = self._eval(_turnover_ctx(split_at=60))
        self.assertTrue(res['available'], res)
        self.assertNotIn('股本数据缺失', res['note'])
        # 没有股本数据时仍按旧口径暂停
        res = self._eval(_turnover_ctx(split_at=60, shares=False))
        self.assertFalse(res['available'])
        self.assertIn('送转', res['note'])

    def test_unlock_uses_real_turnover(self):
        # 解禁：价格不变、成交量翻倍。真实换手率口径下等价于没有放量
        ctx = _turnover_ctx(unlock_at=62)
        res = self._eval(ctx)
        self.assertTrue(res['available'], res)
        turnover = [v / s for v, s in zip(ctx['volume'], ctx['float_shares'])]
        reference = self._eval({**ctx, 'kind': 'index', 'volume': turnover, 'float_shares': None})
        self.assertEqual(res['series'], reference['series'])
        self.assertEqual(res['value'], reference['value'])
        # 成交量口径会把解禁误读成分位跳升：两条曲线不同
        legacy = self._eval({**ctx, 'float_shares': None})
        self.assertNotEqual(legacy['series'][-1]['value'], res['series'][-1]['value'])

    def test_break_without_share_change_is_unavailable(self):
        ctx = _turnover_ctx(split_at=60)
        ctx['float_shares'] = [1e8] * len(ctx['float_shares'])
        res = self._eval(ctx)
        self.assertFalse(res['available'])
        self.assertIn('流通股本未见变动', res['note'])

    def test_missing_share_data_is_labelled(self):
        from .detector.dimensions import SHARE_DATA_MISSING_NOTE

        res = self._eval(_turnover_ctx(shares=False))
        self.assertTrue(res['available'], res)
        self.assertIn(SHARE_DATA_MISSING_NOTE, res['note'])
        # 窗口内缺一天股本也按缺失处理，不混用两种口径
        ctx = _turnover_ctx()
        ctx['float_shares'][-3] = None
        self.assertIn(SHARE_DATA_MISSING_NOTE, self._eval(ctx)['note'])

    def test_index_has_no_share_note(self):
        ctx = _turnover_ctx(shares=False)
        ctx['kind'] = 'index'
        self.assertNotIn('股本', self._eval(ctx)['note'])


class StockContextFloatShareTests(TestCase):
    """build_stock_context 挂上与日线对齐的流通股本；取不到时为 None。"""

    def setUp(self):
        from datetime import date, timedelta
        from decimal import Decimal

        from .models import DailyQuote, Stock

        self.stock = Stock.objects.create(code='600519', name='贵州茅台')
        day = date(2026, 5, 20)
        for i in range(40):
            DailyQuote.objects.create(
                stock=self.stock, trade_date=day + timedelta(days=i), open_price=Decimal('10'),
                high_price=Decimal('10.2'), low_price=Decimal('9.8'), close_price=Decimal('10'),
                open_close_diff=Decimal('0'), open_close_pct=Decimal('0'), high_low_diff=Decimal('0.4'),
                high_low_pct=Decimal('4'), volume=1000000, turnover=Decimal('10000000'), is_final=True,
            )

    def test_shares_aligned_to_dates(self):
        from datetime import date

        from .detector.dimensions import build_stock_context

        hist = [(date(2026, 1, 1), 100.0), (date(2026, 6, 1), 200.0)]
        with patch('stocks.datasources.float_shares.fetch_float_share_history', return_value=hist) as fetch:
            ctx, err = build_stock_context(self.stock)
        fetch.assert_called_once_with('600519')
        self.assertEqual(err, '')
        self.assertEqual(len(ctx['float_shares']), len(ctx['dates']))
        self.assertEqual(ctx['float_shares'][0], 100.0)
        self.assertEqual(ctx['float_shares'][ctx['dates'].index('2026-06-01')], 200.0)

    def test_missing_shares_is_none(self):
        from .detector.dimensions import build_stock_context

        ctx, _err = build_stock_context(self.stock)  # runner 默认关闭取数
        self.assertIsNone(ctx['float_shares'])
