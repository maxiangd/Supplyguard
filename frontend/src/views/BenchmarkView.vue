<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchBenchmarkCases, runBenchmark } from '../api/benchmark'

const loading = ref(true)
const running = ref(false)
const cases = ref([])
const run = ref(null)
const activeTab = ref('cases')

const metricDefs = [
  { key: 'f1', label: '实体 F1' },
  { key: 'path_recall', label: '路径召回率' },
  { key: 'path_precision', label: '路径精确率' },
  { key: 'edge_f1', label: '边级 F1' },
  { key: 'node_f1', label: '节点 F1' },
  { key: 'path_mrr', label: '路径 MRR', raw: true },
  { key: 'path_case_hit_rate', label: 'Hit@K（按用例）' },
  { key: 'path_rank_hit_rate', label: 'Recall@K（按路径）' },
  { key: 'path_hit_rate', label: '路径条数达标率' },
  { key: 'role_block_rate', label: '文档越权拦截率' },
  { key: 'followup_success_rate', label: '多轮追问命中率' },
  { key: 'risk_accuracy', label: '风险等级准确率' },
  { key: 'precision', label: '实体 Precision' },
  { key: 'recall', label: '实体 Recall' },
]

const cardKeys = ['f1', 'path_recall', 'path_precision', 'role_block_rate']

function pct(value) {
  return `${Math.round((value ?? 0) * 100)}%`
}

function delta(current, baseline, raw = false) {
  const diff = (current ?? 0) - (baseline ?? 0)
  if (raw) return `${diff >= 0 ? '+' : ''}${diff.toFixed(3)}`
  const points = Math.round(diff * 100)
  return `${points >= 0 ? '+' : ''}${points} pt`
}

const cards = computed(() => {
  if (!run.value) return []
  return metricDefs
    .filter((def) => cardKeys.includes(def.key))
    .map((def) => ({
      key: def.key,
      label: def.label,
      value: pct(run.value.metrics[def.key]),
      baseline: pct(run.value.baseline[def.key]),
    }))
})

const compareRows = computed(() => {
  if (!run.value) return []
  return metricDefs.map((def) => ({
    key: def.key,
    label: def.label,
    current: run.value.metrics[def.key],
    baseline: run.value.baseline[def.key],
    raw: def.raw,
  }))
})

function display(value, raw) {
  return raw ? (value ?? 0).toFixed(3) : pct(value)
}

const resultRows = computed(() => {
  if (!run.value) return []
  const caseMap = new Map(cases.value.map((item) => [item.id, item]))
  return run.value.results.map((result) => ({
    ...result,
    question: caseMap.get(result.id)?.question ?? '',
    pathNote: result.path_match?.golden_total
      ? `${result.path_match.golden_matched}/${result.path_match.golden_total}`
      : '—',
  }))
})

async function load() {
  const data = await fetchBenchmarkCases()
  cases.value = data.cases.map((c) => ({
    id: c.id,
    question: c.question,
    expected: `角色=${c.role} | 期望实体≥${c.expected_entities?.length || 0} | 标准路径${c.expected_paths?.length || 0} 条`,
    tags: [c.scenario, c.role, ...(c.expected_path_rules || [])],
  }))
  run.value = null  // 需要手动运行
  loading.value = false
}

async function execute() {
  running.value = true
  try {
    const data = await runBenchmark()
    run.value = {
      ran_at: data.ran_at,
      metrics: {
        f1: data.metrics.entity_f1,
        path_recall: data.metrics.path_recall,
        path_precision: data.metrics.path_precision,
        edge_f1: data.metrics.edge_f1,
        node_f1: data.metrics.node_f1,
        path_mrr: data.metrics.path_mrr,
        path_case_hit_rate: data.metrics.path_case_hit_rate,
        path_rank_hit_rate: data.metrics.path_rank_hit_rate,
        path_hit_rate: data.metrics.path_hit_rate,
        role_block_rate: data.metrics.document_leak_block_rate,
        followup_success_rate: data.metrics.followup_success_rate,
        risk_accuracy: data.metrics.rule_precision,
        precision: data.metrics.entity_precision,
        recall: data.metrics.entity_recall,
      },
      baseline: data.baseline,
      documentCheckMode: data.metrics.document_leak_check_mode,
      results: data.results,
    }
    activeTab.value = 'results'
    ElMessage.success(`评测完成：${data.total_cases} 条用例，F1=${(data.metrics.entity_f1 * 100).toFixed(0)}%`)
  } finally {
    running.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never">
      <div class="head">
        <div>
          <div class="title">评测集</div>
          <div class="sub">
            上次运行：{{ run?.ran_at ?? '尚未运行' }} · {{ cases.length }} 条用例
            <template v-if="run"> · 文档拦截模式：{{ run.documentCheckMode }}</template>
          </div>
        </div>
        <el-button type="primary" :loading="running" @click="execute">
          运行评测
        </el-button>
      </div>
    </el-card>

    <el-row :gutter="16" class="row">
      <el-col v-for="card in cards" :key="card.key" :span="6">
        <el-card shadow="never">
          <div class="stat-label">{{ card.label }}</div>
          <div class="stat-value">{{ card.value }}</div>
          <div class="stat-base">单 Agent 基线 {{ card.baseline }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="row">
      <template #header>指标对比</template>
      <el-table :data="compareRows" size="small">
        <el-table-column prop="label" label="指标" min-width="160" />
        <el-table-column label="本系统" width="120">
          <template #default="{ row }">{{ display(row.current, row.raw) }}</template>
        </el-table-column>
        <el-table-column label="单 Agent 基线" width="140">
          <template #default="{ row }">{{ display(row.baseline, row.raw) }}</template>
        </el-table-column>
        <el-table-column label="差值" width="100">
          <template #default="{ row }">
            {{ delta(row.current, row.baseline, row.raw) }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="row">
      <el-tabs v-model="activeTab">
        <el-tab-pane :label="`用例集 (${cases.length})`" name="cases">
          <el-table :data="cases" size="small">
            <el-table-column prop="id" label="编号" width="90" />
            <el-table-column prop="question" label="问题" min-width="260" />
            <el-table-column prop="expected" label="期望结果" min-width="220" />
            <el-table-column label="标签" min-width="140">
              <template #default="{ row }">
                <el-tag
                  v-for="tag in row.tags"
                  :key="tag"
                  size="small"
                  effect="plain"
                  class="tag"
                >
                  {{ tag }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="本次结果" name="results">
          <el-table :data="resultRows" size="small">
            <el-table-column prop="id" label="编号" width="90" />
            <el-table-column prop="question" label="问题" min-width="260" />
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  :type="row.passed ? 'success' : 'danger'"
                >
                  {{ row.passed ? '通过' : '未通过' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="pathNote" label="标准路径命中" width="120" />
            <el-table-column prop="note" label="说明" min-width="240" />
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.title {
  font-size: 16px;
  font-weight: 600;
}

.sub {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
}

.row {
  margin-top: 16px;
}

.stat-label {
  color: #909399;
  font-size: 13px;
}

.stat-value {
  margin-top: 6px;
  font-size: 26px;
  font-weight: 600;
}

.stat-base {
  margin-top: 4px;
  color: #a8abb2;
  font-size: 12px;
}

.tag {
  margin-right: 6px;
}
</style>
