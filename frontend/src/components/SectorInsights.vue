<script setup>
import { computed } from 'vue'
import { formatPct } from '../utils/format.js'
import { buildInsights, metricWidth } from '../utils/sectorFlow.js'

const props = defineProps({
  summary: { type: Object, default: () => ({}) },
  divergenceCount: { type: Number, default: 0 },
})

const insights = computed(() => buildInsights(props.summary, props.divergenceCount))
</script>

<template>
  <section class="insight-panel">
    <h2>解读</h2>
    <div class="insight-grid">
      <article v-for="(item, index) in insights" :key="item.title">
        <span class="insight-index" :class="item.tone">{{ index + 1 }}</span>
        <div>
          <strong>{{ item.title }}</strong>
          <p>{{ item.body }}</p>
        </div>
      </article>
    </div>
  </section>

  <section class="strength-panel">
    <div class="strength-item">
      <div class="strength-heading">
        <span>轮动广度</span><strong>{{ formatPct(summary.breadth_pct) }}</strong>
      </div>
      <div class="meter">
        <i class="breadth-meter" :style="{ width: metricWidth(summary.breadth_pct) }"></i>
      </div>
      <p>
        {{
          Number(summary.breadth_pct) >= 50
            ? '净流入板块占多数，市场扩散度较高。'
            : '净流出板块占多数，市场扩散度偏弱。'
        }}
      </p>
    </div>
    <div class="strength-item">
      <div class="strength-heading">
        <span>拥挤程度</span
        ><strong>{{ formatPct(summary.top_three_inflow_concentration_pct) }}</strong>
      </div>
      <div class="meter">
        <i
          class="crowding-meter"
          :style="{ width: metricWidth(summary.top_three_inflow_concentration_pct) }"
        ></i>
      </div>
      <p>
        {{
          Number(summary.top_three_inflow_concentration_pct) >= 65
            ? '流入集中于少数主线，注意拥挤风险。'
            : '主线集中度适中，资金分布相对均衡。'
        }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.insight-panel,
.strength-panel {
  margin-bottom: 10px;
  border: 1px solid #26343f;
  border-radius: 8px;
  background: #0c141c;
}

.insight-panel h2,
.strength-heading {
  display: flex;
  align-items: center;
}

.insight-panel h2 {
  margin: 0;
  font-size: 14px;
}

.insight-panel {
  display: grid;
  grid-template-columns: 74px 1fr;
  align-items: stretch;
}

.insight-panel > h2 {
  padding: 14px;
  border-right: 1px solid #25323c;
}

.insight-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
}

.insight-grid article {
  display: flex;
  gap: 10px;
  min-width: 0;
  padding: 12px 14px;
  border-right: 1px solid #25323c;
}

.insight-grid article:last-child {
  border-right: 0;
}

.insight-index {
  display: grid;
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  place-items: center;
  border-radius: 50%;
  font-size: 10px;
}

.insight-index.green {
  background: #173129;
  color: #6bc09f;
}

.insight-index.amber {
  background: #382a17;
  color: #e1a84b;
}

.insight-index.blue {
  background: #172c3b;
  color: #63acd3;
}

.insight-grid strong {
  display: block;
  color: #d7e0e7;
  font-size: 11px;
}

.insight-grid p,
.strength-item p {
  margin: 3px 0 0;
  color: #85929c;
  font-size: 10px;
  line-height: 1.45;
}

.strength-panel {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
}

.strength-item {
  padding: 12px 16px;
}

.strength-item:first-child {
  border-right: 1px solid #25323c;
}

.strength-heading {
  justify-content: space-between;
  margin-bottom: 7px;
  font-size: 11px;
}

.strength-heading strong {
  color: #dbe5ec;
  font-size: 12px;
}

.meter {
  height: 5px;
  overflow: hidden;
  border-radius: 3px;
  background: #1e2932;
}

.meter i {
  display: block;
  height: 100%;
  border-radius: inherit;
}

.breadth-meter {
  background: #4b9fc9;
}

.crowding-meter {
  background: #dfa13c;
}

@media (max-width: 900px) {
  .insight-panel {
    grid-template-columns: 1fr;
  }

  .insight-panel > h2 {
    border-right: 0;
    border-bottom: 1px solid #25323c;
  }
}

@media (max-width: 680px) {
  .insight-grid,
  .strength-panel {
    grid-template-columns: 1fr;
  }

  .insight-grid article,
  .strength-item:first-child {
    border-right: 0;
    border-bottom: 1px solid #25323c;
  }
}
</style>
