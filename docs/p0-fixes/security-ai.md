# P0 修复说明：AI 服务商安全（security-ai）

分支：`fix/p0-security-ai`（未 push）

## SEC-01（原 AI-01）AI 服务商 base_url 改写外泄 API Key / SSRF —— 已修复

### 改法
1. **改 base_url 必须重新录入 Key**（`stocks/ai_serializers.py`）
   - `validate()`：已有实例、请求里带 `base_url` 且与库中值（去尾 `/` 后）不同、又没给 `api_key` → 400 `{'api_key': '修改接口地址时必须重新录入 API Key'}`。
   - 只改 name/model/is_enabled，或带上未改动的原 base_url（前端编辑表单总会带上）→ 仍保留原 Key。
2. **base_url 校验**（`stocks/ai/llm.py::validate_base_url`，序列化器 `validate_base_url` 与 `LlmClient.chat` 共用）
   - 只允许 `https`；`AI_ALLOW_HTTP_BASE_URL=1` 时放行 `http`（仅本地开发）。
   - 拒绝 URL 内含用户名/密码、query、fragment；缺主机名、端口非法拒绝。
   - IP 字面量直接检查；域名用 `socket.getaddrinfo` 解析，**任一**解析结果非公网（私网、回环、链路本地含 169.254.169.254、保留、未指定 0.0.0.0、组播、100.64/10 共享地址、IPv4 映射 IPv6 如 `::ffff:127.0.0.1`）即拒绝；**解析失败也拒绝**（fail closed）。
   - `AI_BASE_URL_ALLOWED_HOSTS` 非空时只允许白名单主机；白名单主机视为运维显式信任，跳过内网检查（用于本地 Ollama 等）。
   - 保存时去尾部 `/` 规范化。
3. **llm.py 请求侧**
   - `requests.post(..., allow_redirects=False)`；3xx 抛 `LlmError(kind='redirect')`，不回显 Location。
   - 每次调用前再次 `validate_base_url`（覆盖库里历史遗留的不合规地址和 DNS 变化），不合规 `LlmError(kind='config')`，不会发出请求。
   - `LlmError` 新增 `kind`（config/network/timeout/redirect/auth/rate_limit/upstream/bad_response）与 `status_code` 属性；原有 message 文本保持不变（其他端点行为不变）。
4. **test 端点**（`stocks/ai/views.py::test_connection`）
   - 失败只返回 `{'status':'failed','error':<分类>,'upstream_status':<状态码>,'detail':<通用文案>,'message':同上}`，HTTP 502；不再回显上游响应体。成功只返回 `{'status':'ok'}`，**不再回显 LLM 回复内容**（原来回显前 50 字）。
   - 服务端日志只记 provider id、kind、status，不记上游 body。
   - 新增 `throttle.check_provider_test_throttle()`：每分钟 5 次、每日 30 次（所有服务商合计）；超限 429。调用前先写 `AiCallLog(purpose='provider_test', success=False)` 预占，成功后更新 success/tokens。
   - `provider_test` 流水**不计入**正式 AI 功能的每日总额度（`check_throttle` 的 daily_count 排除该用途）。

### 涉及文件
- `backend/stocks/ai_serializers.py`
- `backend/stocks/ai/llm.py`
- `backend/stocks/ai/views.py`
- `backend/stocks/ai/throttle.py`
- `backend/stocks/tests_ai.py`
- `backend/StockTrace/settings.py`（仅新增 2 个设置项，紧跟 `STOCKTRACE_AI_DAILY_LIMIT` 之后）：
  - `AI_ALLOW_HTTP_BASE_URL`（环境变量同名，`1/true/yes` 为真，默认 False）
  - `AI_BASE_URL_ALLOWED_HOSTS`（环境变量同名，逗号分隔，默认空 = 任意公网主机）

未改 models.py、未新增迁移。`AiCallLog.purpose` 写入了不在 `PURPOSE_CHOICES` 里的 `'provider_test'`：CharField 的 choices 不在 ORM 写入时校验，功能无影响；admin 里该用途会显示原值。若要在 choices 补上该项需迁移（只有 data-ingest 可加），可留作后续。

### 新增/修改测试（`stocks/tests_ai.py`）
- `AiProviderApiTests`（setUp 统一 mock `stocks.ai.llm.socket.getaddrinfo`，断网可跑）
  - `test_update_without_key_keeps_old`（保留：只改 model 仍保留 Key）
  - `test_update_with_same_base_url_without_key_ok`
  - `test_update_base_url_without_key_rejected`（400，库中地址与 Key 不变）
  - `test_update_base_url_with_new_key_ok`
  - `test_insecure_base_urls_rejected`（http/ftp/回环/私网/169.254/0.0.0.0/IPv4 映射/DNS 解析到私网与 ::1/无法解析/带凭据/带 query/非 URL，16 个子用例）
  - `test_base_url_normalized_trailing_slash`、`test_local_dev_allowed_via_settings`、`test_allowed_hosts_whitelist_enforced`
- `LlmClientSecurityTests`：重定向不跟随且 `allow_redirects=False`；历史遗留内网/http 地址在发请求前被拦；错误分类；100.64/10、组播被拒。
- `ProviderTestEndpointTests`：上游 body 不回显；成功不回显回复且记流水；重定向只报分类不泄露 Location；内网 base_url 不发请求；频控 5 次后 429 且不占正式额度。

### 测试结果
- `DJANGO_DEBUG=true stv/bin/python nonet_run.py stocks`：**Ran 231，failures=1，errors=2**。
  - failure：`tests_features.test_sina_fallback_when_em_fails`（已知，写死日期）。
  - errors：`tests_detector` 的 `test_all_sources_down_produces_no_fabricated_signals`、`test_calm_sector_with_two_aligned_dims_fires_signal`（`KeyError: 'status'`）。**在本分支 HEAD（未改动，git stash 后）同样报错**，与本次改动无关，疑似与运行日期/非交易日相关，请 alerts-detector 方向或合并时确认。
  - 新增 AI 测试全部通过（基线 214 → 231，新增 17 个测试方法）。
- 注：不设 `DJANGO_DEBUG=true` 时 settings 因缺 `DJANGO_SECRET_KEY` 拒绝启动（他人改动带来的行为），测试需带该变量或设 SECRET_KEY。
- `stv/bin/ruff check .`：All checks passed。

### 运维须知
- 部署后**已存的服务商**若 base_url 不是 https 公网地址，所有 AI 调用和 test 会报"接口地址不安全"，需在设置页改地址并重新录入 Key。
- 本地开发连 Ollama 等：`AI_ALLOW_HTTP_BASE_URL=1` + `AI_BASE_URL_ALLOWED_HOSTS=127.0.0.1`（注意白名单非空后只允许名单内主机，需要同时用公网服务商就一起写上，如 `127.0.0.1,api.deepseek.com`）。
- 生产建议设置 `AI_BASE_URL_ALLOWED_HOSTS=api.deepseek.com,api.moonshot.cn,dashscope.aliyuncs.com,open.bigmodel.cn`（按实际使用填写），比"任意公网"更严。
- 服务器需能做 DNS 解析，否则保存服务商会被拒（fail closed）。

### 未完成 / 残留风险 / 需 VPS 实测
- **DNS rebinding TOCTOU**：校验时解析与 requests 实际连接时解析是两次，恶意域名可在两次之间改解析结果。未做 IP 钉扎（https 下要保 SNI/证书校验，改动较大）。配置 `AI_BASE_URL_ALLOWED_HOSTS` 白名单可彻底规避。
- 个股分析/选股翻译/点评端点的 502 仍把 `LlmError` 文本（含上游 body 前 300 字）回显（属 SEC-07/AI-08，不在本条范围；地址已限定为公网 https，SSRF 读内网的路径已断）。如需一并处理，可在这些视图改用 `exc.kind` 分类文案。
- 前端 `Settings.vue::errText` 只识别 `detail`/`api_key`，base_url 校验错误（`{'base_url': [...]}`）会显示成 "Request failed with status code 400"；改 base_url 未填 Key 的提示能正常显示。建议前端方向在 errText 里补 `data.base_url`，并在编辑表单改地址时提示需重新填写 Key。不归本方向文件，未改。
- Provider 写操作的单独保护（IsAdminUser/开关，AI-01 建议 4）未做，属 SEC-02 范畴。
- 需 VPS 实测：真实服务商（如 api.deepseek.com）保存与 test 正常；该服务商不会对 `/chat/completions` 返回 30x（若有则需填最终地址）。
