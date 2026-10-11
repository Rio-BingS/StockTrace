// AI 服务商配置（AIX-07）：优先级与请求参数风格，取值与后端 AiProvider 一致

// 优先级：数字越小越优先，相同时按录入顺序；后端校验 0–9999
export const DEFAULT_PRIORITY = 100
export const PRIORITY_MIN = 0
export const PRIORITY_MAX = 9999

// 参数风格：auto 按模型名判断；推理模型不传 temperature，OpenAI o 系列/gpt-5 改用 max_completion_tokens
export const DEFAULT_PARAM_STYLE = 'auto'
export const PARAM_STYLE_OPTIONS = [
  { value: 'auto', label: '自动（按模型名判断）' },
  { value: 'standard', label: '标准（temperature + max_tokens）' },
  { value: 'reasoning', label: '推理模型（不传 temperature）' },
  { value: 'openai_reasoning', label: 'OpenAI 推理（max_completion_tokens）' },
]

// 表格里用的短标签
const PARAM_STYLE_SHORT = {
  auto: '自动',
  standard: '标准',
  reasoning: '推理',
  openai_reasoning: 'OpenAI 推理',
}

export function paramStyleLabel(value) {
  return PARAM_STYLE_SHORT[value] || PARAM_STYLE_SHORT[DEFAULT_PARAM_STYLE]
}

/**
 * 表单里的优先级 → 提交值：空/非数字按默认值，小数取整，越界夹到 0–9999。
 * （number 输入框清空时 v-model 得到 ''，直接提交会被后端判为不合法）
 */
export function normalizePriority(value) {
  if (value === '' || value === null || value === undefined) return DEFAULT_PRIORITY
  const n = Math.round(Number(value))
  if (!Number.isFinite(n)) return DEFAULT_PRIORITY
  return Math.min(PRIORITY_MAX, Math.max(PRIORITY_MIN, n))
}

// 新增/编辑表单的初始值；编辑时从列表行带出，缺字段（旧后端）按默认值
export function providerForm(p = null) {
  return {
    id: p?.id ?? null,
    name: p?.name ?? '',
    base_url: p?.base_url ?? '',
    model: p?.model ?? '',
    api_key: '',
    is_enabled: p?.is_enabled ?? true,
    priority: p?.priority ?? DEFAULT_PRIORITY,
    param_style: p?.param_style || DEFAULT_PARAM_STYLE,
  }
}
