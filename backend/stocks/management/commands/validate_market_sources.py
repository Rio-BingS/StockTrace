from collections import Counter

import akshare as ak
import pandas as pd
from django.core.management.base import BaseCommand

from stocks.market import etf_row_to_item, parse_fund_flow_table


ETF_FIELDS = (
    '最新份额', '成交额', '总市值', '流通市值', '主力净流入-净额',
    '基金折价率', 'IOPV实时估值', '数据日期', '更新时间',
)
REPRESENTATIVE_ETFS = (
    ('510300', '沪市宽基'),
    ('159915', '深市宽基'),
    ('512480', '行业主题'),
    ('511010', '债券'),
    ('518880', '商品'),
    ('513100', '跨境'),
)


class Command(BaseCommand):
    help = '人工校验 ETF/板块/大盘资金流上游字段、覆盖率与口径（只读）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--history-code',
            action='append',
            dest='history_codes',
            help='仅校验指定 ETF 日线；可重复传入。默认校验代表性样本',
        )

    def _section(self, title):
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(f'== {title} =='))

    def _report_frame_error(self, label, exc):
        self.stdout.write(self.style.ERROR(f'{label}失败（已跳过，不中止其他校验）: {exc}'))

    def _validate_etf_spot(self):
        self._section('ETF 全市场快照')
        try:
            frame = ak.fund_etf_spot_em()
        except Exception as exc:  # noqa: BLE001 校验命令边界：报告任何上游失败后继续其余校验
            self._report_frame_error('ETF 全表', exc)
            return None, []

        self.stdout.write(self.style.SUCCESS(f'返回 {len(frame)} 行，{len(frame.columns)} 列'))
        self.stdout.write(f'字段：{list(frame.columns)}')

        code_series = frame['代码'].astype(str).str.zfill(6) if '代码' in frame.columns else pd.Series(dtype=str)
        missing_codes = int(code_series.isna().sum() + (code_series == '').sum())
        duplicate_codes = int(code_series.duplicated(keep=False).sum())
        self.stdout.write(f'代码缺失：{missing_codes}；重复记录：{duplicate_codes}')

        for col in ETF_FIELDS:
            if col not in frame.columns:
                self.stdout.write(self.style.WARNING(f'{col}: 字段缺失'))
                continue
            series = frame[col]
            non_null = int(series.notna().sum())
            ratio = non_null / len(frame) if len(frame) else 0
            samples = [str(value) for value in series.dropna().head(3).tolist()]
            self.stdout.write(f'{col}: 非空 {non_null}/{len(frame)} ({ratio:.1%})；样本 {samples}')

        normalized = [etf_row_to_item(row) for _, row in frame.iterrows()]
        reason_counts = Counter(item['scope_match_reason'] for item in normalized)
        matched = [item for item in normalized if item['scope_match']]
        unmatched = [item for item in normalized if not item['scope_match']]
        self.stdout.write(
            f'股票/宽基规则命中：{len(matched)}；排除/未判定：{len(unmatched)}；'
            f'覆盖率：{len(matched) / len(normalized):.1%}' if normalized else 'ETF 规范化结果为空'
        )
        self.stdout.write(f'规则原因统计：{dict(reason_counts)}')

        for title, items in (('命中样本', matched), ('排除/未判定样本', unmatched)):
            sample = [
                f"{item['code']} {item['name']} ({item['scope_match_reason']})"
                for item in items[:12]
            ]
            self.stdout.write(f'{title}：{sample}')

        self._section('代表性 ETF 分类检查')
        by_code = {item['code']: item for item in normalized}
        for code, category in REPRESENTATIVE_ETFS:
            item = by_code.get(code)
            if not item:
                self.stdout.write(self.style.WARNING(f'{category} {code}: 快照中不存在'))
                continue
            self.stdout.write(
                f"{category} {code} {item['name']}: scope_match={item['scope_match']}，"
                f"原因={item['scope_match_reason']}"
            )

        return frame, normalized

    def _validate_sectors(self):
        self._section('行业 / 概念资金流')
        for label, loader, candidates in (
            ('行业', ak.stock_fund_flow_industry, ['行业']),
            ('概念', ak.stock_fund_flow_concept, ['行业', '概念']),
        ):
            try:
                frame = loader()
                parsed = parse_fund_flow_table(frame, candidates)
            except Exception as exc:  # noqa: BLE001 校验命令边界：同上
                self._report_frame_error(f'{label}资金', exc)
                continue

            name_col = next((col for col in candidates if col in frame.columns), None)
            duplicate_names = (
                int(frame[name_col].astype(str).duplicated(keep=False).sum())
                if name_col else None
            )
            positive = sum(1 for item in parsed if item['net'] > 0)
            negative = sum(1 for item in parsed if item['net'] < 0)
            self.stdout.write(self.style.SUCCESS(
                f'{label}: 上游 {len(frame)} 行，规范化 {len(parsed)} 行，'
                f'净流入 {positive}，净流出 {negative}，名称重复 {duplicate_names}'
            ))
            self.stdout.write(f'{label}字段：{list(frame.columns)}')
            self.stdout.write(f'{label}样本：{parsed[:3]}')

    def _validate_history(self, requested_codes):
        self._section('单 ETF 日线历史')
        if requested_codes:
            samples = [(str(code).zfill(6), '指定样本') for code in requested_codes]
        else:
            samples = REPRESENTATIVE_ETFS

        for code, category in samples:
            try:
                frame = ak.fund_etf_hist_em(
                    symbol=code,
                    period='daily',
                    start_date='20260101',
                    end_date='20991231',
                    adjust='',
                )
            except Exception as exc:  # noqa: BLE001 校验命令边界：同上
                self._report_frame_error(f'{category} ETF {code} 日线', exc)
                continue

            if frame is None or frame.empty:
                self.stdout.write(self.style.WARNING(f'{category} ETF {code}: 日线为空'))
                continue
            first_date = frame.iloc[0].get('日期')
            last_date = frame.iloc[-1].get('日期')
            self.stdout.write(self.style.SUCCESS(
                f'{category} ETF {code}: {len(frame)} 行，覆盖 {first_date} → {last_date}'
            ))
            self.stdout.write(f'字段：{list(frame.columns)}')

    def _validate_market_flow(self):
        """大盘主力资金流：akshare 主源列映射 + 与自采沪深合计的同日口径/量级对照（只读）。"""
        from stocks.market import flows
        from stocks.market.snapshots import fetch_market_ff_merged_rows

        self._section('大盘主力资金流（口径/单位）')
        try:
            frame = ak.stock_market_fund_flow()
        except Exception as exc:  # noqa: BLE001 校验命令边界：同上
            self._report_frame_error('东财 stock_market_fund_flow', exc)
            return
        self.stdout.write(f'字段：{list(frame.columns)}')
        parsed = flows._normalize_market_fund_hist(frame, 30, flows.MARKET_FUND_SOURCE_EM)
        items = (parsed or {}).get('items', [])
        if not items:
            self.stdout.write(self.style.ERROR('规范化结果为空（列名可能变化）'))
            return
        for key in ('main_net', 'super_net', 'large_net', 'mid_net', 'small_net'):
            non_null = sum(1 for i in items if i.get(key) is not None)
            self.stdout.write(f'{key}: 非空 {non_null}/{len(items)}')
        try:
            merged, errors = fetch_market_ff_merged_rows()
        except Exception as exc:  # noqa: BLE001 校验命令边界：同上
            self._report_frame_error('自采沪深 fflow', exc)
            return
        if errors:
            self.stdout.write(self.style.WARNING(f'自采沪深 fflow 部分失败：{errors}'))
        common = [i for i in items if i['date'] in merged]
        if not common:
            self.stdout.write(self.style.WARNING('主源与自采沪深合计无同日数据，无法对照'))
            return
        last = common[-1]
        ours = merged[last['date']]
        ratio = (last['main_net'] / ours['main_net']) if ours.get('main_net') else None
        self.stdout.write(
            f"{last['date']} 主源 main_net={last['main_net']}，自采沪深合计={ours.get('main_net')}，"
            f'比值={ratio}（应约等于 1；偏离很大说明口径/单位不一致，勿回填）'
        )

    def handle(self, *args, **options):
        self.stdout.write('本命令只读取上游数据：不写数据库、不修改市场缓存、不注册定时任务。')
        self._validate_etf_spot()
        self._validate_sectors()
        self._validate_history(options.get('history_codes'))
        self._validate_market_flow()
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('校验流程结束；请同时检查上方的失败/缺失警告。'))
