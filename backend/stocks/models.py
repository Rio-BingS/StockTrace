from django.db import models
from django.db.models.functions import Length
from django.db.models.lookups import Exact
from rest_framework.utils.encoders import JSONEncoder as DrfJSONEncoder


class AiProvider(models.Model):
    """LLM 服务商配置（OpenAI 兼容协议，一套客户端通吃 DeepSeek/Kimi/Qwen/GLM 等）

    api_key 落库前用 DJANGO_SECRET_KEY 派生密钥加密（stocks/ai/crypto.py），
    序列化输出只回尾四位脱敏。
    """
    name = models.CharField('名称', max_length=50)
    base_url = models.CharField('接口地址', max_length=200,
                                help_text='OpenAI 兼容 base_url，如 https://api.deepseek.com')
    api_key_encrypted = models.TextField('加密的 API Key')
    model = models.CharField('模型名', max_length=100)
    is_enabled = models.BooleanField('启用', default=True)
    # 多服务商选择/回退顺序（AIX-07）：数字越小越优先，相同时按录入顺序
    priority = models.IntegerField('优先级', default=100,
                                   help_text='数字越小越优先；相同时按录入顺序')
    # 请求参数风格（AIX-07）：auto 按模型名判断；推理模型不传 temperature，
    # OpenAI o 系列/gpt-5 还要把 max_tokens 换成 max_completion_tokens
    PARAM_STYLE_AUTO = 'auto'
    PARAM_STYLE_STANDARD = 'standard'
    PARAM_STYLE_REASONING = 'reasoning'
    PARAM_STYLE_OPENAI_REASONING = 'openai_reasoning'
    PARAM_STYLE_CHOICES = [
        (PARAM_STYLE_AUTO, '自动（按模型名判断）'),
        (PARAM_STYLE_STANDARD, '标准（temperature + max_tokens）'),
        (PARAM_STYLE_REASONING, '推理模型（不传 temperature）'),
        (PARAM_STYLE_OPENAI_REASONING, 'OpenAI 推理（max_completion_tokens）'),
    ]
    param_style = models.CharField('参数风格', max_length=20, choices=PARAM_STYLE_CHOICES,
                                   default=PARAM_STYLE_AUTO)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = 'AI 服务商'
        verbose_name_plural = 'AI 服务商'
        # 与调用时的选择/回退顺序一致：启用的在前，再按优先级、录入顺序
        ordering = ['-is_enabled', 'priority', 'id']

    def __str__(self):
        return f"{self.name} ({self.model})"


class AiCallLog(models.Model):
    """AI 调用流水：用于同股冷却与每日调用上限节流（防 Basic Auth 泄露后额度被烧）"""
    PURPOSE_ANALYSIS = 'analysis'
    PURPOSE_SCREENER_TRANSLATE = 'screener_translate'
    PURPOSE_SCREENER_COMMENT = 'screener_comment'
    PURPOSE_CHOICES = [
        (PURPOSE_ANALYSIS, '个股分析'),
        (PURPOSE_SCREENER_TRANSLATE, '选股条件翻译'),
        (PURPOSE_SCREENER_COMMENT, '选股结果点评'),
    ]

    provider = models.ForeignKey(
        AiProvider, on_delete=models.SET_NULL, null=True, verbose_name='服务商'
    )
    stock = models.ForeignKey(
        'Stock', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='股票'
    )
    purpose = models.CharField('用途', max_length=30, choices=PURPOSE_CHOICES)
    STATUS_PENDING = 'pending'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, '进行中'),
        (STATUS_SUCCESS, '成功'),
        (STATUS_FAILED, '失败'),
    ]

    success = models.BooleanField('成功', default=True)
    # 调用状态（AIX-06）：占位流水先记 pending，回填为 success/failed；
    # 节流的同股冷却只算 pending + success，跨进程也能看到"进行中"
    status = models.CharField('状态', max_length=10, choices=STATUS_CHOICES,
                              default=STATUS_SUCCESS, db_index=True)
    prompt_tokens = models.IntegerField('输入 tokens', null=True, blank=True)
    completion_tokens = models.IntegerField('输出 tokens', null=True, blank=True)
    # 可观测性字段（AIX-08）：均可空，旧行与未知值保持 NULL，不拿 0 冒充
    model = models.CharField('模型名', max_length=100, null=True, blank=True)
    duration_ms = models.PositiveIntegerField('耗时(毫秒)', null=True, blank=True)
    error_reason = models.CharField('失败原因', max_length=200, null=True, blank=True,
                                    help_text='失败时的简短原因，写入前截断到 200 字')
    total_tokens = models.IntegerField('总 tokens', null=True, blank=True)
    cost = models.DecimalField('估算费用', max_digits=12, decimal_places=6, null=True, blank=True,
                               help_text='按服务商计价币种估算；无单价配置时为空')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 调用记录'
        verbose_name_plural = 'AI 调用记录'
        ordering = ['-created_at']
        # 节流按 created_at 计当日次数、按 (stock, purpose, created_at) 查冷却，
        # 清理任务按 created_at 删旧行（PERF-10）
        indexes = [
            models.Index(fields=['created_at'], name='aicalllog_created_idx'),
            models.Index(fields=['stock', 'purpose', 'created_at'], name='aicalllog_stock_purpose_idx'),
        ]


class AiJob(models.Model):
    """AI 后台任务（AIX-08 收尾：原进程内存队列落库）。

    视图同步完成校验与额度预占后写一行 pending 任务并返回 202 + job_id；
    Web 进程内的后台线程用条件 UPDATE 抢占（pending→running，多 worker 不重复执行），
    结果与原 HTTP 状态码写回本表，前端轮询 /api/ai/jobs/<job_id>/ 读取。
    进程重启后 pending 任务由任一 worker 继续执行；running 超过租约的按中断标失败。
    """
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_DONE = 'done'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, '排队中'),
        (STATUS_RUNNING, '执行中'),
        (STATUS_DONE, '完成'),
        (STATUS_FAILED, '失败'),
    ]

    job_id = models.CharField('任务 ID', max_length=32, unique=True)
    kind = models.CharField('任务类型', max_length=30)
    # 执行所需参数（提示词消息、占位流水 id 等）；结果按 DRF 的编码规则存，轮询返回与同步接口同形
    params = models.JSONField('参数', default=dict, encoder=DrfJSONEncoder)
    status = models.CharField('状态', max_length=10, choices=STATUS_CHOICES,
                              default=STATUS_PENDING)
    http_status = models.PositiveSmallIntegerField('结果状态码', null=True, blank=True)
    result = models.JSONField('结果', null=True, blank=True, encoder=DrfJSONEncoder)
    error = models.CharField('失败原因', max_length=200, null=True, blank=True)
    # 认领者（主机:进程:随机串）与租约到期时刻：租约过期仍是 running 视为执行进程已退出
    owner = models.CharField('执行者', max_length=80, null=True, blank=True)
    lease_until = models.DateTimeField('租约到期', null=True, blank=True)
    attempts = models.PositiveSmallIntegerField('认领次数', default=0)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    started_at = models.DateTimeField('开始时间', null=True, blank=True)
    finished_at = models.DateTimeField('结束时间', null=True, blank=True)

    class Meta:
        verbose_name = 'AI 后台任务'
        verbose_name_plural = 'AI 后台任务'
        ordering = ['id']
        # 认领按 (status, id) 取最早的 pending；清理按 created_at 删旧行
        indexes = [
            models.Index(fields=['status', 'id'], name='aijob_status_idx'),
            models.Index(fields=['created_at'], name='aijob_created_idx'),
        ]

    def __str__(self):
        return f"{self.kind} {self.job_id} ({self.status})"


class StockGroup(models.Model):
    """自选股分组；股票可不分组（group 为空）。删除分组时股票自动变回未分组。"""
    name = models.CharField('分组名称', max_length=50, unique=True)
    order = models.IntegerField('显示顺序', default=0)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '自选分组'
        verbose_name_plural = '自选分组'
        ordering = ['order', 'id']

    def __str__(self):
        return self.name


class Stock(models.Model):
    """关注的股票"""
    code = models.CharField('股票代码', max_length=10, unique=True)
    name = models.CharField('股票名称', max_length=50, blank=True, default='')
    is_active = models.BooleanField('是否监控中', default=True)
    group = models.ForeignKey(
        StockGroup, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='stocks', verbose_name='分组',
    )
    # 可选持仓成本：填了才参与看板盈亏统计（None 表示纯观察，不算钱）
    cost_price = models.DecimalField('成本价', max_digits=10, decimal_places=3, null=True, blank=True)
    quantity = models.IntegerField('持股数(股)', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '关注股票'
        verbose_name_plural = '关注股票'
        ordering = ['code']

    def __str__(self):
        return f"{self.code} {self.name}"


class MarketDailySnapshot(models.Model):
    """市场日度快照：收盘后把当日关键横截面落库一行，用于多日趋势指标。

    数据诚信：只存上游当日真实返回；某天上游不可用就没有该行，
    多日指标按「窗口内快照齐全才算数」计算，绝不拿 0 或旧值补天。
    """
    KIND_INDUSTRY_FF = 'industry_ff'
    KIND_CONCEPT_FF = 'concept_ff'
    KIND_ETF_SHARE = 'etf_share'
    KIND_MARKET_FF = 'market_ff'
    # 探测器「融资余额渐进变化」维度的序列供数：两融 T+1 披露且无逐日历史接口
    # （沪市 21 天回看、深市仅单日探测），只能靠日度快照积累出窗口。
    KIND_MARGIN = 'margin'
    KIND_CHOICES = [
        (KIND_INDUSTRY_FF, '行业资金流'),
        (KIND_CONCEPT_FF, '概念资金流'),
        (KIND_ETF_SHARE, 'ETF份额'),
        (KIND_MARKET_FF, '大盘主力资金流'),
        (KIND_MARGIN, '融资余额'),
    ]

    kind = models.CharField('快照类型', max_length=30, choices=KIND_CHOICES)
    trade_date = models.DateField('交易日期')
    payload = models.JSONField('快照数据')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    # 同日重跑 update_or_create 会覆盖 payload：读方据此判断快照是否变化（PERF-07）
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '市场日度快照'
        verbose_name_plural = '市场日度快照'
        ordering = ['-trade_date']
        constraints = [
            models.UniqueConstraint(fields=['kind', 'trade_date'], name='uniq_snapshot_kind_day'),
        ]

    def __str__(self):
        return f"{self.kind} {self.trade_date}"


class PriceAlert(models.Model):
    """自选股价格提醒规则（收盘汇总与盘中任务顺带评估，无独立轮询）"""
    PRICE_ABOVE = 'price_above'
    PRICE_BELOW = 'price_below'
    DAILY_PCT_ABOVE = 'daily_pct_above'
    DAILY_PCT_BELOW = 'daily_pct_below'
    RULE_CHOICES = [
        (PRICE_ABOVE, '价格上穿'),
        (PRICE_BELOW, '价格下穿'),
        (DAILY_PCT_ABOVE, '日涨幅达到'),
        (DAILY_PCT_BELOW, '日跌幅达到'),
    ]

    stock = models.ForeignKey(
        Stock, on_delete=models.CASCADE, related_name='alerts', verbose_name='股票'
    )
    rule_type = models.CharField('规则类型', max_length=30, choices=RULE_CHOICES)
    threshold = models.DecimalField('阈值', max_digits=12, decimal_places=4)
    note = models.CharField('备注', max_length=100, blank=True, default='')
    is_active = models.BooleanField('启用', default=True)
    last_triggered_at = models.DateTimeField('最近触发时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '价格提醒'
        verbose_name_plural = '价格提醒'
        ordering = ['stock__code', 'id']

    def __str__(self):
        return f"{self.stock.code} {self.get_rule_type_display()} {self.threshold}"


class AlertEvent(models.Model):
    """提醒触发记录；同一规则同一交易日只触发一次（trade_date 去重）"""
    alert = models.ForeignKey(
        PriceAlert, on_delete=models.CASCADE, related_name='events', verbose_name='提醒规则'
    )
    stock = models.ForeignKey(
        Stock, on_delete=models.SET_NULL, null=True, verbose_name='股票'
    )
    message = models.CharField('提醒内容', max_length=200)
    trade_date = models.DateField('触发交易日')
    is_read = models.BooleanField('已读', default=False)
    created_at = models.DateTimeField('触发时间', auto_now_add=True)

    class Meta:
        verbose_name = '提醒记录'
        verbose_name_plural = '提醒记录'
        ordering = ['-created_at']
        # 库级保证同规则同日只一条，防并发评估重复触发（PERF-06）
        constraints = [
            models.UniqueConstraint(fields=['alert', 'trade_date'], name='uniq_alert_day'),
        ]
        # 评估时按当日取已触发规则、列表按未读筛选（PERF-10）
        indexes = [
            models.Index(fields=['trade_date', 'is_read'], name='alertevent_day_read_idx'),
        ]

    def __str__(self):
        return f"{self.message} ({self.trade_date})"


class ScreenerPreset(models.Model):
    """条件选股预设：保存结构化条件 spec，供一键重跑（单用户系统，无归属字段）"""
    name = models.CharField('预设名称', max_length=50, unique=True)
    spec = models.JSONField('筛选条件')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '选股预设'
        verbose_name_plural = '选股预设'
        ordering = ['name']

    def __str__(self):
        return self.name


class DailyQuote(models.Model):
    """每日行情数据（含最高/最低点精确时间）"""
    stock = models.ForeignKey(
        Stock, on_delete=models.CASCADE,
        related_name='daily_quotes', verbose_name='股票'
    )
    trade_date = models.DateField('交易日期')

    # 价格类字段 3 位小数：ETF/LOF 报价精确到 0.001 元，2 位会被舍入失真（DAT-11）
    open_price = models.DecimalField('开盘价', max_digits=10, decimal_places=3)
    close_price = models.DecimalField('收盘价', max_digits=10, decimal_places=3)
    high_price = models.DecimalField('最高价', max_digits=10, decimal_places=3)
    low_price = models.DecimalField('最低价', max_digits=10, decimal_places=3)

    high_time = models.DateTimeField('最高点时间', null=True, blank=True)
    low_time = models.DateTimeField('最低点时间', null=True, blank=True)

    # 计算字段
    open_close_diff = models.DecimalField(
        '收盘-开盘差值', max_digits=10, decimal_places=3
    )
    open_close_pct = models.DecimalField(
        '收盘-开盘百分比', max_digits=8, decimal_places=4
    )
    high_low_diff = models.DecimalField(
        '最高-最低差值', max_digits=10, decimal_places=3
    )
    high_low_pct = models.DecimalField(
        '最高-最低百分比', max_digits=8, decimal_places=4
    )

    # 相对昨收的涨跌
    prev_close = models.DecimalField('昨收价', max_digits=10, decimal_places=3, null=True, blank=True)
    change_diff = models.DecimalField('涨跌额', max_digits=10, decimal_places=3, null=True, blank=True)
    change_pct = models.DecimalField('涨跌幅', max_digits=8, decimal_places=4, null=True, blank=True)

    # 单位约定：成交量「股」、成交额「元」（各数据源在适配层换算，见 datasources/）
    volume = models.BigIntegerField('成交量(股)', null=True, blank=True)
    turnover = models.DecimalField(
        '成交额(元)', max_digits=15, decimal_places=2, null=True, blank=True
    )
    # 实际数据来源（eastmoney/sina/baostock/tencent）；空串为 0009 迁移前的存量行，来源未知
    source = models.CharField('数据来源', max_length=16, blank=True, default='')
    # 是否为收盘后的最终日 K（DAT-13）：盘中拉到的当日 bar 为 False，收盘汇总后置 True；
    # 存量行默认 True。探测器/选股/回看等需要完整日 K 的读方应过滤 is_final=True
    is_final = models.BooleanField('已收盘', default=True)

    class Meta:
        verbose_name = '每日行情'
        verbose_name_plural = '每日行情'
        ordering = ['-trade_date']
        constraints = [
            models.UniqueConstraint(fields=['stock', 'trade_date'], name='uniq_quote_stock_day'),
        ]

    def __str__(self):
        return f"{self.stock.code} {self.trade_date}"

    def compute_derived_fields(self):
        """计算差值和百分比"""
        self.open_close_diff = self.close_price - self.open_price
        self.open_close_pct = (
            self.open_close_diff / self.open_price * 100
            if self.open_price else 0
        )
        self.high_low_diff = self.high_price - self.low_price
        self.high_low_pct = (
            self.high_low_diff / self.low_price * 100
            if self.low_price else 0
        )
        if self.prev_close is not None:
            self.change_diff = self.close_price - self.prev_close
            self.change_pct = (
                self.change_diff / self.prev_close * 100
                if self.prev_close else 0
            )


class MinuteBar(models.Model):
    """分钟K线数据（用于推算最高/最低点精确时间）"""
    stock = models.ForeignKey(
        Stock, on_delete=models.CASCADE,
        related_name='minute_bars', verbose_name='股票'
    )
    datetime = models.DateTimeField('分钟时间戳')
    open = models.DecimalField('开盘价', max_digits=10, decimal_places=3)
    close = models.DecimalField('收盘价', max_digits=10, decimal_places=3)
    high = models.DecimalField('最高价', max_digits=10, decimal_places=3)
    low = models.DecimalField('最低价', max_digits=10, decimal_places=3)
    volume = models.BigIntegerField('成交量(股)')
    turnover = models.DecimalField(
        '成交额(元)', max_digits=15, decimal_places=2, null=True, blank=True
    )
    source = models.CharField('数据来源', max_length=16, blank=True, default='')
    # bar 周期（分钟）：东财/新浪为 1，BaoStock 兜底为 5；同一交易日只保留一种周期
    freq = models.PositiveSmallIntegerField('周期(分钟)', default=1)

    class Meta:
        verbose_name = '分钟K线'
        verbose_name_plural = '分钟K线'
        ordering = ['datetime']
        # bulk_create(update_conflicts) 依赖此唯一约束
        constraints = [
            models.UniqueConstraint(fields=['stock', 'datetime'], name='uniq_minutebar_stock_dt'),
        ]

    def __str__(self):
        return f"{self.stock.code} {self.datetime}"


class StockMarginDaily(models.Model):
    """个股融资明细日表（沪深交易所官方披露，T+1）：每只两融标的每个信用交易日一行。

    为什么落库：探测器融资拥挤度（MB5/DTC）要在扫描当日的股票池内取截面分位，
    需要逐股 T−5..T−1 的融资买入额与 T−1 融资余额；接口按日返回全市场明细，
    逐股现拉既慢又不可回放。金额一律为元（整数），与交易所口径一致。
    - 沪市明细自带融资偿还额；深市只有余额与买入额，偿还额由
      前一交易日余额 + 当日买入 − 当日余额 推出，rz_repay_derived=True；
      前一日余额缺失时推不出，留空（不补 0）。
    """
    EXCHANGE_SSE = 'sse'
    EXCHANGE_SZSE = 'szse'
    EXCHANGE_CHOICES = [
        (EXCHANGE_SSE, '上交所'),
        (EXCHANGE_SZSE, '深交所'),
    ]

    code = models.CharField('证券代码', max_length=6)
    trade_date = models.DateField('信用交易日')
    exchange = models.CharField('交易所', max_length=4, choices=EXCHANGE_CHOICES)
    rz = models.BigIntegerField('融资余额(元)')
    rz_buy = models.BigIntegerField('融资买入额(元)', null=True, blank=True)
    rz_repay = models.BigIntegerField('融资偿还额(元)', null=True, blank=True)
    rz_repay_derived = models.BooleanField('偿还额为推导值', default=False)
    source = models.CharField('数据来源', max_length=32, blank=True, default='')
    fetched_at = models.DateTimeField('拉取时间', auto_now=True)

    class Meta:
        verbose_name = '个股融资明细'
        verbose_name_plural = '个股融资明细'
        ordering = ['-trade_date', 'code']
        constraints = [
            models.UniqueConstraint(fields=['code', 'trade_date'], name='uniq_stock_margin_day'),
        ]
        indexes = [models.Index(fields=['trade_date'], name='stockmargin_date_idx')]

    def __str__(self):
        return f"{self.code} {self.trade_date} 融资余额 {self.rz}"


class StockRestrictedRelease(models.Model):
    """限售股解禁日程（东财数据中心 RPT_LIFT_STAGE）：一只股票一个解禁日一类限售股一行。

    解禁日程在发行/定增时即已公开，所以未来日期的行是「已排期」而非预测。
    - free_ratio：占解禁前流通股的比例（小数，0.0123 = 1.23%），探测器 lift_pressure
      按 T 之后 20 个交易日内的合计比例判定；
    - 同一（代码、解禁日、类型）上游偶有多批次，入库时合并（股数、市值、比例相加）；
    - 日程会因追加承诺等推迟：每日任务按窗口整段覆盖，旧排期随之消失。
    """
    code = models.CharField('证券代码', max_length=6)
    name = models.CharField('证券简称', max_length=32, blank=True, default='')
    free_date = models.DateField('解禁日')
    share_type = models.CharField('限售股类型', max_length=100, default='')
    free_shares = models.FloatField('解禁数量(股)', null=True, blank=True)
    able_free_shares = models.FloatField('实际可流通数量(股)', null=True, blank=True)
    free_ratio = models.FloatField('占解禁前流通股比例', null=True, blank=True)
    lift_market_cap = models.FloatField('解禁市值(元)', null=True, blank=True)
    holder_num = models.IntegerField('解禁股东数', null=True, blank=True)
    source = models.CharField('数据来源', max_length=32, blank=True, default='')
    fetched_at = models.DateTimeField('拉取时间', auto_now=True)

    class Meta:
        verbose_name = '限售解禁日程'
        verbose_name_plural = '限售解禁日程'
        ordering = ['free_date', 'code']
        constraints = [
            models.UniqueConstraint(fields=['code', 'free_date', 'share_type'],
                                    name='uniq_restricted_release'),
        ]
        indexes = [
            models.Index(fields=['free_date'], name='restricted_date_idx'),
            models.Index(fields=['code', 'free_date'], name='restricted_code_date_idx'),
        ]

    def __str__(self):
        return f"{self.code} {self.free_date} {self.share_type}"


class IndexConstituent(models.Model):
    """指数当前成分（沪深300、中证500）：探测器成分池与 DTC/ID20 截面参照池。

    只存「当前」成分，不存历史：每周整表刷新一次（中证指数官网 csindex 优先，
    新浪成分接口兜底），任一指数拉取失败或数量明显不对时保留旧值，不清空。
    """
    index_code = models.CharField('指数代码', max_length=6)
    code = models.CharField('成分券代码', max_length=6)
    name = models.CharField('成分券名称', max_length=32, blank=True, default='')
    # 上游给出的成分日期（csindex 的「日期」列）；兜底源没有时为空
    as_of = models.DateField('成分日期', null=True, blank=True)
    source = models.CharField('数据来源', max_length=32, blank=True, default='')
    fetched_at = models.DateTimeField('刷新时间', auto_now=True)

    class Meta:
        verbose_name = '指数成分'
        verbose_name_plural = '指数成分'
        ordering = ['index_code', 'code']
        constraints = [
            models.UniqueConstraint(fields=['index_code', 'code'], name='uniq_index_constituent'),
        ]
        indexes = [models.Index(fields=['code'], name='constituent_code_idx')]

    def __str__(self):
        return f"{self.index_code} {self.code} {self.name}"


class DetectorDimConfig(models.Model):
    """探测器微变维度的可调配置（权重 / 阈值 / 启停）。

    为什么独立成表：探测器的迭代优化要按回看命中率微调各维度的权重与
    灵敏度阈值，调整结果必须跨重启持久化且可审计（调整流水在
    DetectorTuneLog，本表 updated_at 记录最后生效时间）。
    detector/registry.py 提供维度定义、默认参数与计算实现；本表只存
    「当前生效值」，首次扫描时按注册表种子化。
    """
    key = models.CharField('维度标识', max_length=40, unique=True)
    name = models.CharField('维度名称', max_length=50)
    weight = models.FloatField('权重', default=1.0)
    params = models.JSONField('维度参数（窗口/阈值等）', default=dict, blank=True)
    is_enabled = models.BooleanField('启用', default=True)
    disabled_reason = models.CharField('停用原因', max_length=200, blank=True, default='')
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '探测器维度配置'
        verbose_name_plural = '探测器维度配置'
        ordering = ['key']

    def __str__(self):
        return f"{self.name} ({self.key}) w={self.weight:.2f}"


class DetectorSignal(models.Model):
    """探测器信号：每个目标对象（指数/自选股/板块）每个交易日最多一行。

    为什么按天落库而不是只存「当前信号」：
    - 信号判定要求「连续维持 2-3 个周期」，周期数需要回看历史行才能算出；
    - 「回看 5 个交易日命中率」的迭代优化需要信号日 + 其后 5 日的兑现结果；
    - 同 (kind, code, trade_date) 唯一 + 重跑 update_or_create，保证幂等。
    """
    TARGET_INDEX = 'index'
    TARGET_STOCK = 'stock'
    TARGET_INDUSTRY = 'industry'
    TARGET_CONCEPT = 'concept'
    TARGET_KIND_CHOICES = [
        (TARGET_INDEX, '指数'),
        (TARGET_STOCK, '自选股'),
        (TARGET_INDUSTRY, '行业板块'),
        (TARGET_CONCEPT, '概念板块'),
    ]
    DIRECTION_UP = 'up'
    DIRECTION_DOWN = 'down'
    DIRECTION_CHOICES = [
        (DIRECTION_UP, '向上微变'),
        (DIRECTION_DOWN, '向下微变'),
    ]
    # watching: 周期 1（刚满足投票，尚不构成「维持」）；active: 周期 ≥2；
    # confirmed/missed: 5 日回看后兑现/未兑现（由 review 步骤推进）。
    STATUS_WATCHING = 'watching'
    STATUS_ACTIVE = 'active'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_MISSED = 'missed'
    STATUS_CHOICES = [
        (STATUS_WATCHING, '观察中'),
        (STATUS_ACTIVE, '维持中'),
        (STATUS_CONFIRMED, '已兑现'),
        (STATUS_MISSED, '未兑现'),
    ]
    SEVERITY_HIGH = 'high'
    SEVERITY_MEDIUM = 'medium'
    SEVERITY_LOW = 'low'
    SEVERITY_CHOICES = [
        (SEVERITY_HIGH, '高'),
        (SEVERITY_MEDIUM, '中'),
        (SEVERITY_LOW, '低'),
    ]

    target_kind = models.CharField('对象类型', max_length=20, choices=TARGET_KIND_CHOICES)
    target_code = models.CharField('对象代码', max_length=30)
    target_name = models.CharField('对象名称', max_length=50, blank=True, default='')
    trade_date = models.DateField('信号交易日')
    direction = models.CharField('方向', max_length=10, choices=DIRECTION_CHOICES)
    cycles = models.IntegerField('连续周期数', default=1)
    score = models.FloatField('同向强度(0-100)', default=0.0)
    severity = models.CharField('严重程度', max_length=10, choices=SEVERITY_CHOICES, default='low')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_WATCHING)
    # 每个维度的评估明细：{key: {name, value, direction, weight, available, note}}；
    # 用 JSON 而非关联表：单日单对象的维度快照是只读整体，无需单独查询/索引
    dimensions = models.JSONField('维度明细', default=dict, blank=True)
    # 信号附注（det-v2）：不参与投票的提示与修饰——趋势状态、融资拥挤（分位、
    # 严重度升级原因）、解禁事件、日内追买过热等。只展示，不影响判定与复盘统计
    annotations = models.JSONField('信号附注', default=dict, blank=True)
    review_hit = models.BooleanField('回看是否兑现', null=True, blank=True)
    hit_date = models.DateField('兑现日期', null=True, blank=True)
    reviewed_at = models.DateTimeField('回看时间', null=True, blank=True)
    # 回看明细（#7/#12）：全部可空——v1 口径（1×σ20、板块 T 收盘基期）判定的
    # 旧行没有这些值，统计与展示按「缺失」处理，不与新口径混算。
    # review_date 是做出判定的扫描日，DetectorRun 的基线/超额/独立样本数按它
    # 从库里重算，同日重跑幂等。
    review_date = models.DateField('回看判定日', null=True, blank=True, db_index=True)
    review_version = models.PositiveSmallIntegerField('回看口径版本', null=True, blank=True)
    review_threshold = models.FloatField('兑现阈值(%)', null=True, blank=True)
    review_baseline = models.FloatField('同标的基线命中率', null=True, blank=True)
    fwd_return = models.FloatField('h日收益(%)', null=True, blank=True)
    bench_return = models.FloatField('基准h日收益(%)', null=True, blank=True)
    # 原始差值（标的 − 基准），不乘方向；方向调整在统计时做
    excess_return = models.FloatField('相对基准超额(%)', null=True, blank=True)
    benchmark_code = models.CharField('基准代码', max_length=20, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '探测器信号'
        verbose_name_plural = '探测器信号'
        ordering = ['-trade_date', '-score']
        constraints = [
            models.UniqueConstraint(
                fields=['target_kind', 'target_code', 'trade_date'], name='uniq_signal_target_day',
            ),
            # 库级约束（PERF-10）：写入路径只有 detector.signals.upsert_signal 和
            # review 推进状态，取值都来自上面的 CHOICES；score 是 [-100, 100] 分值
            # 的绝对值加权平均，落在 [0, 100]；周期从 1 起算。Meta 里拿不到外层类
            # 属性，取值只能写字面量，改 CHOICES 时要同步改这里并加迁移。
            models.CheckConstraint(
                condition=models.Q(target_kind__in=['index', 'stock', 'industry', 'concept']),
                name='detsig_target_kind_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(direction__in=['up', 'down']), name='detsig_direction_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(severity__in=['high', 'medium', 'low']), name='detsig_severity_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=['watching', 'active', 'confirmed', 'missed']),
                name='detsig_status_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=0, score__lte=100), name='detsig_score_range',
            ),
            models.CheckConstraint(condition=models.Q(cycles__gte=1), name='detsig_cycles_positive'),
            # target_code 是多态弱引用，按类型约束格式：指数 sh/sz/bj + 6 位（共 8 位），
            # 自选股 6 位（数字由 Stock 序列化器校验，这里不用 REGEXP：SQLite 的
            # REGEXP 是 Django 注入的函数，命令行 sqlite3 回灌 .dump 时会报错），
            # 板块为名称，只要求非空
            models.CheckConstraint(
                condition=(
                    models.Q(target_kind='index')
                    & (models.Q(target_code__startswith='sh') | models.Q(target_code__startswith='sz')
                       | models.Q(target_code__startswith='bj'))
                    & Exact(Length('target_code'), 8)
                ) | (
                    models.Q(target_kind='stock') & Exact(Length('target_code'), 6)
                ) | (
                    models.Q(target_kind__in=['industry', 'concept']) & ~models.Q(target_code='')
                ),
                name='detsig_target_code_format',
            ),
        ]
        indexes = [
            models.Index(fields=['trade_date', 'severity']),
            models.Index(fields=['status', 'review_hit']),
        ]

    def __str__(self):
        return f"{self.get_target_kind_display()} {self.target_name or self.target_code} {self.trade_date} {self.direction}"


class DetectorRun(models.Model):
    """探测器扫描运行日志：每交易日一行（幂等覆盖），迭代与降级全程留痕。

    matrix 存当日全部平静板块对象 × 维度的微变分值（热力图直接供数），
    是本表唯一较大的字段——由 DETECTOR_RUN_RETENTION_DAYS 定期清理。
    """
    trade_date = models.DateField('扫描交易日', unique=True)
    # ok: 全部数据源正常；partial: 部分对象/维度因上游或快照不足降级；failed: 扫描未完成
    status = models.CharField('运行状态', max_length=20, default='ok')
    targets_scanned = models.IntegerField('扫描对象数', default=0)
    calm_count = models.IntegerField('平静对象数', default=0)
    signals_new = models.IntegerField('新增观察信号数(周期1)', default=0)
    signals_sustained = models.IntegerField('维持信号数(周期≥2)', default=0)
    reviewed_count = models.IntegerField('回看信号数', default=0)
    hit_count = models.IntegerField('回看命中数', default=0)
    hit_rate = models.FloatField('回看命中率', null=True, blank=True)
    # 命中率要和基线比才有意义（#7）：零假设下绝对命中率本身不是有效性指标。
    # 只统计新口径（review_version≥2）的回看；旧 run 行这些字段为空。
    baseline_rate = models.FloatField('同标的基线命中率', null=True, blank=True)
    excess_rate = models.FloatField('超额命中率', null=True, blank=True)
    independent_samples = models.IntegerField('独立样本数(按日聚类)', null=True, blank=True)
    avg_excess_return = models.FloatField('平均相对基准超额(%)', null=True, blank=True)
    degraded = models.JSONField('降级记录', default=list, blank=True)
    # 当日调参流水的副本（兼容旧读法）；长期审计以 DetectorTuneLog 为准，
    # 本表按 DETECTOR_RUN_RETENTION_DAYS 清理
    adjustments = models.JSONField('权重/阈值调整流水', default=list, blank=True)
    matrix = models.JSONField('板块微变矩阵', default=dict, blank=True)
    # 当日被否决的信号（det-v2）：如向上信号因融资拥挤被否决，不落 DetectorSignal，
    # 但对象、分位与原因留在这里供页面展示「已否决」
    vetoed = models.JSONField('否决记录', default=list, blank=True)
    message = models.CharField('备注', max_length=300, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '探测器扫描记录'
        verbose_name_plural = '探测器扫描记录'
        ordering = ['-trade_date']

    def __str__(self):
        return f"{self.trade_date} {self.status}"


class DetectorTuneLog(models.Model):
    """探测器调参流水（#16）：每次自动调参（或关闭时的演练结论）一行，永久保留。

    为什么单独建表：旧流水写在 DetectorRun.adjustments，run 只保留 120 天，
    而 DetectorDimConfig 的改动永久生效——四个月后就无法回答「这个权重是
    哪天、凭什么样本改成这样的」。本表行很小，不参与任何保留期清理。
    - source：auto=自动调参已生效；dry_run=自动调参关闭（默认），只记录
      「若开启会怎么调」；legacy=迁移时从旧 DetectorRun.adjustments 搬来的记录；
    - changes：{参数名: [旧值, 新值]}，权重记为 weight；
    - stats：判定所用的统计量（独立样本数、命中率、基线、后验概率等）。
    """
    SOURCE_AUTO = 'auto'
    SOURCE_DRY_RUN = 'dry_run'
    SOURCE_LEGACY = 'legacy'
    SOURCE_CHOICES = [
        (SOURCE_AUTO, '自动调参'),
        (SOURCE_DRY_RUN, '演练（未生效）'),
        (SOURCE_LEGACY, '旧流水迁移'),
    ]

    trade_date = models.DateField('扫描交易日')
    dim_key = models.CharField('维度标识', max_length=40)
    source = models.CharField('来源', max_length=20, choices=SOURCE_CHOICES, default=SOURCE_AUTO)
    applied = models.BooleanField('是否已生效', default=False)
    changes = models.JSONField('参数变更', default=dict, blank=True)
    stats = models.JSONField('统计依据', default=dict, blank=True)
    reason = models.CharField('原因', max_length=300, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '探测器调参流水'
        verbose_name_plural = '探测器调参流水'
        ordering = ['-trade_date', 'dim_key']
        indexes = [models.Index(fields=['dim_key', 'trade_date'])]

    def __str__(self):
        return f"{self.trade_date} {self.dim_key} {self.source}"
