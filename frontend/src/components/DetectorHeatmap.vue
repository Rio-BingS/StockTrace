<script setup>
// 板块微变热力图：行=板块（后端已按最大 |分值| 降序并截取前 40），
// 列=启用的微变维度，格值=归一化分值（-100~100）。
// 配色沿用全站红涨绿跌：正=红、负=绿；None 格子不渲染（露出深色底=数据不可用）。
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { HeatmapChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { escapeHtml } from '../utils/escapeHtml.js'

use([HeatmapChart, GridComponent, TooltipComponent, VisualMapComponent, CanvasRenderer])

const props = defineProps({
  dims: { type: Array, default: () => [] },   // [{key, name}]
  rows: { type: Array, default: () => [] },   // [{code, name, values: {key: value|null}}]
})

const chartData = computed(() => {
  const cells = []
  props.rows.forEach((row, y) => {
    props.dims.forEach((dim, x) => {
      const v = row.values?.[dim.key]
      if (v != null) cells.push([x, y, Number(v)])
    })
  })
  return cells
})

const height = computed(() => Math.max(300, props.rows.length * 26 + 100))

const option = computed(() => {
  if (!chartData.value.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: {
      position: 'top',
      backgroundColor: '#101827',
      borderColor: '#33415f',
      textStyle: { color: '#ddd', fontSize: 11 },
      formatter: (p) => {
        const row = props.rows[p.value[1]]
        const dim = props.dims[p.value[0]]
        const v = p.value[2]
        // 板块名来自第三方数据源，拼 HTML 前转义（SEC-09）
        return `${escapeHtml(row?.name)}<br/>${escapeHtml(dim?.name)}：<b style="color:${v >= 0 ? '#ff8796' : '#4cd68a'}">${v > 0 ? '+' : ''}${v.toFixed(1)}</b>`
      },
    },
    grid: { left: 8, right: 12, top: 6, bottom: 40, containLabel: true },
    xAxis: {
      type: 'category',
      data: props.dims.map(d => d.name),
      splitArea: { show: true },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#9eabc1', fontSize: 11 },
    },
    yAxis: {
      type: 'category',
      data: props.rows.map(r => r.name),
      inverse: true, // 分值最高的板块排最上
      splitArea: { show: true },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#a6b1c5', fontSize: 11 },
    },
    visualMap: {
      min: -100,
      max: 100,
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 2,
      itemHeight: 90,
      textStyle: { color: '#68758d', fontSize: 10 },
      // A 股配色：红=向上微变、绿=向下微变、中段深色=接近 0
      inRange: { color: ['#00a846', '#0f3d2e', '#16213e', '#3d1e2b', '#e94560'] },
    },
    series: [{
      type: 'heatmap',
      data: chartData.value,
      itemStyle: { borderColor: '#111d34', borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.5)' } },
    }],
  }
})
</script>

<template>
  <v-chart v-if="chartData.length" :option="option" autoresize :style="{ height: height + 'px', width: '100%' }" />
  <div v-else class="empty">矩阵内暂有板块但无有效分值</div>
</template>

<style scoped>
.empty { text-align: center; padding: 36px 20px; color: #64718a; }
</style>
