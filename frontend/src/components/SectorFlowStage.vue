<script setup>
import { computed } from 'vue'
import { pctClass } from '../utils/format.js'
import { flowPath, netToneLabel, rowTitle, signedAmount } from '../utils/sectorFlow.js'

const props = defineProps({
  board: { type: String, default: 'industry' },
  period: { type: String, default: 'day' },
  loading: { type: Boolean, default: false },
  data: { type: Object, default: null },
})

const periodLabel = computed(() =>
  props.period === '5d' ? '5日累计' : props.period === '10d' ? '10日累计' : '当日',
)

const summary = computed(() => props.data?.summary || {})
const inflowRows = computed(() => (props.data?.inflow_top || []).slice(0, 5))
const outflowRows = computed(() => (props.data?.outflow_top || []).slice(0, 5))
const inflowTotal = computed(() =>
  inflowRows.value.reduce((sum, row) => sum + Number(row.net || 0), 0),
)
const outflowTotal = computed(() =>
  outflowRows.value.reduce((sum, row) => sum + Number(row.net || 0), 0),
)
const netTone = computed(() => netToneLabel(summary.value.net_total))

function barWidth(row, side) {
  const rows = side === 'in' ? inflowRows.value : outflowRows.value
  const maximum = Math.max(...rows.map((item) => Math.abs(Number(item.net || 0))), 0)
  if (!maximum) return '0%'
  return `${Math.max(4, (Math.abs(Number(row.net || 0)) / maximum) * 100)}%`
}
</script>

<template>
  <section class="flow-shell">
    <div class="flow-shell-header">
      <div>
        <span class="eyebrow">资金流向</span>
        <h2>{{ board === 'industry' ? '行业' : '概念' }}板块前五强弱</h2>
      </div>
      <span class="method-label">{{ periodLabel }}</span>
    </div>

    <div v-if="loading && !data" class="flow-loading">正在加载板块资金...</div>
    <div v-else class="flow-stage">
      <svg class="flow-lines" viewBox="0 0 1200 360" preserveAspectRatio="none" aria-hidden="true">
        <path
          v-for="(_, index) in outflowRows"
          :key="`out-${index}`"
          class="line-out"
          :d="flowPath(`out`, index)"
        />
        <path
          v-for="(_, index) in inflowRows"
          :key="`in-${index}`"
          class="line-in"
          :d="flowPath(`in`, index)"
        />
      </svg>

      <div class="flow-column flow-column-out">
        <div class="column-heading">
          <div><span>净流出板块</span><small>前五合计</small></div>
          <strong class="down">{{ signedAmount(outflowTotal) }}</strong>
        </div>
        <div class="flow-list">
          <div
            v-for="(row, index) in outflowRows"
            :key="row.name"
            class="flow-row"
            :title="rowTitle(row)"
          >
            <span class="rank rank-out">{{ String(index + 1).padStart(2, '0') }}</span>
            <div class="flow-name">
              <b>{{ row.name }}</b
              ><small>{{ row.leader || '暂无领涨股' }}</small>
            </div>
            <div class="flow-track">
              <i class="flow-fill" :style="{ width: barWidth(row, `out`) }"></i>
            </div>
            <strong class="flow-value down">{{ signedAmount(row.net) }}</strong>
          </div>
        </div>
      </div>
      <div class="flow-core-wrap">
        <div class="flow-core">
          <span class="flow-symbol" aria-hidden="true">⇄</span>
          <small>{{ netTone }}</small>
          <strong :class="pctClass(summary.net_total)">{{
            signedAmount(summary.net_total)
          }}</strong>
          <span>板块净额</span>
        </div>
      </div>

      <div class="flow-column flow-column-in">
        <div class="column-heading">
          <div><span>净流入板块</span><small>前五合计</small></div>
          <strong class="up">{{ signedAmount(inflowTotal) }}</strong>
        </div>
        <div class="flow-list">
          <div
            v-for="(row, index) in inflowRows"
            :key="row.name"
            class="flow-row"
            :title="rowTitle(row)"
          >
            <span class="rank rank-in">{{ String(index + 1).padStart(2, '0') }}</span>
            <div class="flow-name">
              <b>{{ row.name }}</b
              ><small>{{ row.leader || '暂无领涨股' }}</small>
            </div>
            <div class="flow-track">
              <i class="flow-fill" :style="{ width: barWidth(row, `in`) }"></i>
            </div>
            <strong class="flow-value up">{{ signedAmount(row.net) }}</strong>
          </div>
        </div>
      </div>
      <div v-if="!outflowRows.length && !inflowRows.length" class="flow-empty">
        暂无资金流向数据
      </div>
    </div>

    <footer class="flow-footnote">
      <span aria-hidden="true">ⓘ</span>
      <p>
        {{
          data?.methodology || '板块资金为上游当日聚合强弱指标，不代表板块之间的真实资金转移路径。'
        }}
      </p>
    </footer>
  </section>
</template>

<style scoped>
.flow-shell {
  margin-bottom: 10px;
  border: 1px solid #26343f;
  border-radius: 8px;
  background: #0c141c;
}

.method-label {
  border: 1px solid #2e4251;
  border-radius: 4px;
  color: #8fc6d6;
  font-size: 11px;
  padding: 2px 7px;
}

.flow-shell-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid #25323c;
}

.eyebrow {
  display: block;
  margin-bottom: 2px;
  color: #84929e;
  font-size: 10px;
}

.flow-shell h2 {
  margin: 0;
  font-size: 14px;
}

.flow-stage {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 190px minmax(0, 1fr);
  min-height: 342px;
  padding: 12px 10px 14px;
  overflow: hidden;
}

.flow-lines {
  position: absolute;
  z-index: 0;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.flow-lines path {
  fill: none;
  stroke-width: 1.3;
  vector-effect: non-scaling-stroke;
}

.line-out {
  stroke: #347b63;
}

.line-in {
  stroke: #8a4248;
}

.flow-column,
.flow-core-wrap {
  position: relative;
  z-index: 1;
}

.flow-column {
  min-width: 0;
}

.column-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 48px;
  padding: 0 8px;
}

.column-heading span,
.column-heading small {
  display: block;
}

.column-heading span {
  color: #dbe4eb;
  font-size: 12px;
  font-weight: 600;
}

.column-heading small {
  margin-top: 2px;
  color: #87929b;
  font-size: 10px;
}

.column-heading > strong {
  font-size: 13px;
}

.flow-list {
  display: grid;
  gap: 4px;
}

.flow-row {
  display: grid;
  grid-template-columns: 28px minmax(68px, 0.7fr) minmax(80px, 1.25fr) 82px;
  gap: 8px;
  align-items: center;
  min-height: 48px;
  padding: 4px 8px;
}

.rank {
  display: grid;
  width: 27px;
  height: 23px;
  place-items: center;
  border: 1px solid #31424d;
  border-radius: 4px;
  color: #84929d;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 10px;
}

.rank-out {
  border-color: #285b4d;
  color: #6fc6a4;
}

.rank-in {
  border-color: #66363b;
  color: #e78990;
}

.flow-name {
  min-width: 0;
}

.flow-name b,
.flow-name small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.flow-name b {
  color: #dce5ec;
  font-size: 12px;
}

.flow-name small {
  margin-top: 2px;
  color: #86919b;
  font-size: 9px;
}

.flow-track {
  height: 6px;
  overflow: hidden;
  border-radius: 3px;
  background: #1d2831;
}

.flow-fill {
  display: block;
  min-width: 3px;
  height: 100%;
  border-radius: inherit;
}

.flow-column-out .flow-fill {
  margin-left: auto;
  background: #64be9c;
}

.flow-column-in .flow-fill {
  background: #db666d;
}

.flow-value {
  text-align: right;
  font-size: 11px;
  white-space: nowrap;
}

.flow-core-wrap {
  display: grid;
  place-items: center;
}

.flow-core {
  display: grid;
  width: 150px;
  min-height: 112px;
  place-items: center;
  align-content: center;
  border: 1px solid #31526a;
  border-radius: 7px;
  background: #101b27;
  box-shadow: 0 0 0 5px rgba(23, 52, 69, 0.18);
}

.flow-symbol {
  color: #55a8d5;
  font-size: 23px;
  line-height: 1;
}

.flow-core small,
.flow-core > span:last-child {
  color: #84929d;
  font-size: 10px;
}

.flow-core strong {
  margin: 3px 0;
  font-size: 21px;
}

.flow-empty,
.flow-loading {
  display: grid;
  min-height: 330px;
  place-items: center;
  color: #86919b;
  font-size: 12px;
}

.flow-empty {
  position: absolute;
  z-index: 3;
  inset: 0;
}

.flow-footnote {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  border-top: 1px solid #25323c;
  color: #86919a;
  padding: 9px 13px;
  font-size: 10px;
}

.flow-footnote p {
  margin: 0;
}

.up {
  color: #ec6c74;
}

.down {
  color: #68c29f;
}

@media (max-width: 1120px) {
  .flow-stage {
    grid-template-columns: minmax(0, 1fr) 160px minmax(0, 1fr);
  }

  .flow-row {
    grid-template-columns: 26px minmax(62px, 0.8fr) minmax(56px, 1fr) 74px;
    gap: 6px;
    padding-inline: 5px;
  }

  .flow-core {
    width: 132px;
  }
}

@media (max-width: 900px) {
  .flow-stage {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px 18px;
  }

  .flow-lines {
    display: none;
  }

  .flow-core-wrap {
    grid-column: 1 / -1;
    grid-row: 1;
    padding: 8px 0;
  }

  .flow-column-out {
    grid-column: 1;
    grid-row: 2;
  }

  .flow-column-in {
    grid-column: 2;
    grid-row: 2;
  }

  .flow-core {
    width: 180px;
    min-height: 96px;
  }
}

@media (max-width: 680px) {
  .flow-stage {
    grid-template-columns: 1fr;
  }

  .flow-core-wrap,
  .flow-column-out,
  .flow-column-in {
    grid-column: 1;
  }

  .flow-core-wrap {
    grid-row: 1;
  }
  .flow-column-out {
    grid-row: 2;
  }
  .flow-column-in {
    grid-row: 3;
  }

  .flow-row {
    grid-template-columns: 28px minmax(72px, 0.75fr) minmax(75px, 1.2fr) 78px;
  }
}

@media (max-width: 430px) {
  .flow-row {
    grid-template-columns: 25px 72px minmax(50px, 1fr) 72px;
    gap: 5px;
  }

  .flow-name small {
    display: none;
  }
}
</style>
