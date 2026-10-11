import { test } from 'node:test'
import assert from 'node:assert/strict'
import { reactive } from 'vue'
import { useIntradayChart } from '../../composables/useIntradayChart.js'
import { DOWN, UP } from '../../constants/colors.js'

function bars(prices) {
  // 故意打乱顺序，验证按时间排序
  return prices
    .map((p, i) => ({
      datetime: `2026-10-09 ${String(9 + Math.floor((30 + i) / 60)).padStart(2, '0')}:${String((30 + i) % 60).padStart(2, '0')}:00`,
      close: String(p),
      volume: 12_345,
    }))
    .reverse()
}

test('useIntradayChart：现价取排序后最后一根，线色按现价相对昨收（ARC-09 / FEX-01）', () => {
  const props = reactive({
    minuteData: bars([10.0, 10.2, 10.1, 9.8]),
    dailyQuote: {
      prev_close: '10.00',
      open_price: '10.00',
      high_price: '10.2',
      low_price: '9.8',
      close_price: '9.8',
    },
  })
  const { baseInfo, chartOption, tableData, extremaList } = useIntradayChart(props)
  assert.equal(baseInfo.value.label, '较昨收')
  assert.equal(tableData.value.current_price, 9.8)
  const series = chartOption.value.series[0]
  assert.equal(series.lineStyle.color, DOWN)
  assert.deepEqual(series.data, [10, 10.2, 10.1, 9.8])
  assert.deepEqual(extremaList.value, [])

  props.minuteData = bars([10.0, 10.3])
  assert.equal(chartOption.value.series[0].lineStyle.color, UP)
})

test('buildIntradayOption：标注百分比不出现 "+-"，tooltip 成交量带单位并转义', () => {
  const props = reactive({
    minuteData: bars([10, 9.9, 9.877]),
    dailyQuote: { prev_close: '10', high_price: '10', low_price: '9.877' },
  })
  const { chartOption } = useIntradayChart(props)
  const labels = chartOption.value.series[0].markPoint.data.map((d) =>
    typeof d.label.formatter === 'function' ? d.label.formatter() : d.label.formatter,
  )
  assert.ok(labels.every((t) => !t.includes('+-')))
  assert.ok(labels.some((t) => t.includes('-1.23%')))
  const html = chartOption.value.tooltip.formatter([{ dataIndex: 0, axisValue: '<b>09:30</b>' }])
  assert.ok(html.includes('&lt;b&gt;09:30'))
  assert.ok(html.includes('1.23 万股'))
})

test('useIntradayChart：无数据时返回空配置', () => {
  const { chartOption, extremaList } = useIntradayChart(
    reactive({ minuteData: [], dailyQuote: null }),
  )
  assert.deepEqual(chartOption.value, {})
  assert.deepEqual(extremaList.value, [])
})
