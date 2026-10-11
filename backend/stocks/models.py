from django.db import models


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
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = 'AI 服务商'
        verbose_name_plural = 'AI 服务商'
        ordering = ['-is_enabled', 'id']

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
    success = models.BooleanField('成功', default=True)
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

    # 单位约定：成交量「股」、成交额「元」（各数据源在适配层换算，见 services.py）
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


class DetectorDimConfig(models.Model):
    """探测器微变维度的可调配置（权重 / 阈值 / 启停）。

    为什么独立成表：探测器的迭代优化要按回看命中率微调各维度的权重与
    灵敏度阈值，调整结果必须跨重启持久化且可审计（调整流水在
    DetectorRun.adjustments，本表 updated_at 记录最后生效时间）。
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
    review_hit = models.BooleanField('回看是否兑现', null=True, blank=True)
    hit_date = models.DateField('兑现日期', null=True, blank=True)
    reviewed_at = models.DateTimeField('回看时间', null=True, blank=True)
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
    degraded = models.JSONField('降级记录', default=list, blank=True)
    adjustments = models.JSONField('权重/阈值调整流水', default=list, blank=True)
    matrix = models.JSONField('板块微变矩阵', default=dict, blank=True)
    message = models.CharField('备注', max_length=300, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '探测器扫描记录'
        verbose_name_plural = '探测器扫描记录'
        ordering = ['-trade_date']

    def __str__(self):
        return f"{self.trade_date} {self.status}"
