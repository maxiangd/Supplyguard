<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createLongTermMemory,
  deleteLongTermMemory,
  getSessionHistory,
  listLongTermMemories,
  listRiskMemories,
  listSessions,
  updateLongTermMemory,
} from '../api/memory'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

// 完整问答记录由风险问答页的「历史对话」承载，这里只管理三类记忆
const activeTab = ref('short')

const shortSessions = ref([])
const shortLoading = ref(false)
const shortSessionId = ref('')
const shortTurns = ref([])

const longTermItems = ref([])
const longTermCategory = ref('')
const longTermLoading = ref(false)

const riskItems = ref([])
const riskEntity = ref('')
const riskLevel = ref('')
const riskLoading = ref(false)

const dialogVisible = ref(false)
const editingId = ref('')
const form = reactive({ category: 'note', title: '', content: '' })

const categoryOptions = [
  { value: 'preference', label: '偏好' },
  { value: 'insight', label: '洞察' },
  { value: 'note', label: '笔记' },
  { value: 'risk', label: '历史风险' },
]
const categoryLabels = {
  preference: '偏好',
  risk: '历史风险',
  insight: '洞察',
  note: '笔记',
}
const categoryTypes = {
  preference: 'success',
  risk: 'danger',
  insight: 'warning',
  note: 'info',
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', { hour12: false })
}

function parseEvidence(row) {
  if (!row?.evidence) return {}
  try {
    return JSON.parse(row.evidence)
  } catch {
    return {}
  }
}

function entityText(row) {
  const evidence = parseEvidence(row)
  const names = evidence.entity_names ?? []
  if (names.length) return names.join('、')
  return (evidence.entity_ids ?? []).join('、')
}

function levelType(level) {
  if (level === 'HIGH' || level === 'CRITICAL') return 'danger'
  return level === 'MEDIUM' ? 'warning' : 'info'
}

async function loadShortSessions() {
  shortLoading.value = true
  try {
    const data = await listSessions()
    shortSessions.value = data.sessions
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '短期记忆加载失败')
    shortSessions.value = []
  } finally {
    shortLoading.value = false
  }
}

async function openShortSession(row) {
  shortSessionId.value = row.session_id
  try {
    const data = await getSessionHistory(row.session_id)
    shortTurns.value = data.turns
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '短期会话加载失败')
    shortTurns.value = []
  }
}

function ttlText(ttl) {
  if (ttl === null || ttl === undefined || ttl < 0) return '—'
  return `剩余 ${Math.max(1, Math.round(ttl / 60))} 分钟`
}

async function loadLongTerm() {
  longTermLoading.value = true
  try {
    const data = await listLongTermMemories(longTermCategory.value || undefined)
    longTermItems.value = data.items
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '长期记忆加载失败')
    longTermItems.value = []
  } finally {
    longTermLoading.value = false
  }
}

async function loadRisks() {
  riskLoading.value = true
  try {
    const data = await listRiskMemories({
      entityId: riskEntity.value.trim() || undefined,
      level: riskLevel.value || undefined,
      limit: 100,
    })
    riskItems.value = data.items
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '历史风险加载失败')
    riskItems.value = []
  } finally {
    riskLoading.value = false
  }
}

function openCreate() {
  editingId.value = ''
  form.category = 'note'
  form.title = ''
  form.content = ''
  dialogVisible.value = true
}

function openEdit(row) {
  editingId.value = row.memory_id
  form.category = row.category
  form.title = row.title
  form.content = row.content
  dialogVisible.value = true
}

async function saveMemory() {
  const title = form.title.trim()
  const content = form.content.trim()
  if (!title || !content) {
    ElMessage.warning('标题和内容不能为空')
    return
  }

  try {
    if (editingId.value) {
      await updateLongTermMemory(editingId.value, {
        category: form.category,
        title,
        content,
      })
    } else {
      await createLongTermMemory({ category: form.category, title, content })
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    loadLongTerm()
    if (activeTab.value === 'risk') loadRisks()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '保存失败')
  }
}

async function removeMemory(row) {
  try {
    await ElMessageBox.confirm(`确定删除「${row.title}」？`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }

  try {
    await deleteLongTermMemory(row.memory_id)
    ElMessage.success('已删除')
    loadLongTerm()
    loadRisks()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '删除失败')
  }
}

onMounted(async () => {
  await loadShortSessions()
  loadLongTerm()
  loadRisks()
})
</script>

<template>
  <el-card shadow="never">
    <div class="head">
      <div>
        <div class="title">记忆与上下文</div>
        <div class="sub">
          当前账号：{{ auth.user?.username ?? '-' }}（{{ auth.role ?? '-' }}）·
          完整问答记录在「风险问答」页的历史对话中查看；这里管理短期记忆、长期记忆与历史风险
        </div>
      </div>
    </div>
  </el-card>

  <el-card shadow="never" class="row">
    <el-tabs v-model="activeTab">
      <el-tab-pane label="短期记忆" name="short">
        <div class="toolbar">
          <span class="hint" style="margin-left: 0">
            Redis 中尚未过期的会话（仅当前账号），过期后由永久档案保留
          </span>
          <el-button :loading="shortLoading" @click="loadShortSessions">刷新</el-button>
        </div>

        <el-table
          v-loading="shortLoading"
          :data="shortSessions"
          size="small"
          class="session-table"
          :row-class-name="
            ({ row }) =>
              row.session_id === shortSessionId ? 'active-session' : ''
          "
          @row-click="openShortSession"
        >
          <el-table-column label="会话" width="110">
            <template #default="{ row }">
              {{ row.session_id.slice(0, 8) }}
            </template>
          </el-table-column>
          <el-table-column prop="turns" label="轮次" width="70" />
          <el-table-column
            prop="last_question"
            label="最近问题"
            min-width="240"
            show-overflow-tooltip
          />
          <el-table-column label="风险" width="90">
            <template #default="{ row }">
              <el-tag
                v-if="row.risk_level"
                size="small"
                :type="levelType(row.risk_level)"
              >
                {{ row.risk_level }}
              </el-tag>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column label="有效期" width="120">
            <template #default="{ row }">{{ ttlText(row.ttl) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                size="small"
                @click.stop="openShortSession(row)"
              >
                查看
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty
          v-if="!shortLoading && !shortSessions.length"
          :image-size="60"
          description="Redis 中没有未过期的会话"
        />

        <div v-if="shortTurns.length" class="short-detail">
          <div class="short-title">
            会话 {{ shortSessionId.slice(0, 8) }}（{{ shortTurns.length }} 轮）
          </div>
          <div v-for="(turn, index) in shortTurns" :key="index" class="turn">
            <div class="turn-question">Q：{{ turn.question }}</div>
            <div class="turn-answer">A：{{ turn.answer }}</div>
            <div v-if="turn.risk_level" class="turn-meta">
              风险等级：{{ turn.risk_level }}
            </div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="长期记忆" name="long">
        <div class="toolbar">
          <el-select
            v-model="longTermCategory"
            placeholder="全部分类"
            clearable
            style="width: 160px"
            @change="loadLongTerm"
          >
            <el-option
              v-for="item in categoryOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <el-button type="primary" @click="openCreate">新建记忆</el-button>
          <span class="hint">共 {{ longTermItems.length }} 条</span>
        </div>

        <el-table v-loading="longTermLoading" :data="longTermItems" size="small">
          <el-table-column label="分类" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="categoryTypes[row.category] ?? 'info'">
                {{ categoryLabels[row.category] ?? row.category }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="title" label="标题" min-width="180" />
          <el-table-column prop="content" label="内容" min-width="280" show-overflow-tooltip />
          <el-table-column prop="role" label="角色" width="100" />
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row)">
                编辑
              </el-button>
              <el-button link type="danger" size="small" @click="removeMemory(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="历史风险" name="risk">
        <div class="toolbar">
          <el-input
            v-model="riskEntity"
            placeholder="按实体过滤，如 supplier-enjie"
            style="width: 260px"
            clearable
          />
          <el-select v-model="riskLevel" placeholder="全部等级" clearable style="width: 140px">
            <el-option label="HIGH" value="HIGH" />
            <el-option label="MEDIUM" value="MEDIUM" />
            <el-option label="LOW" value="LOW" />
          </el-select>
          <el-button type="primary" :loading="riskLoading" @click="loadRisks">
            查询
          </el-button>
          <span class="hint">共 {{ riskItems.length }} 条</span>
        </div>

        <el-table v-loading="riskLoading" :data="riskItems" size="small">
          <el-table-column label="等级" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="levelType(parseEvidence(row).risk_level)">
                {{ parseEvidence(row).risk_level ?? '—' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="title" label="风险结论" min-width="220" show-overflow-tooltip />
          <el-table-column label="涉及实体" min-width="180">
            <template #default="{ row }">{{ entityText(row) || '—' }}</template>
          </el-table-column>
          <el-table-column prop="content" label="回答摘要" min-width="280" show-overflow-tooltip />
          <el-table-column label="记录时间" width="170">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button link type="danger" size="small" @click="removeMemory(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </el-card>

  <el-dialog v-model="dialogVisible" :title="editingId ? '编辑记忆' : '新建记忆'" width="520px">
    <el-form label-width="70px">
      <el-form-item label="分类">
        <el-select v-model="form.category" style="width: 100%">
          <el-option
            v-for="item in categoryOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="标题">
        <el-input v-model="form.title" maxlength="255" show-word-limit />
      </el-form-item>
      <el-form-item label="内容">
        <el-input
          v-model="form.content"
          type="textarea"
          :rows="6"
          maxlength="5000"
          show-word-limit
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="saveMemory">保存</el-button>
    </template>
  </el-dialog>
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

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.session-table {
  margin-bottom: 12px;
}

.session-table :deep(.active-session) {
  background: #ecf5ff;
}

.short-detail {
  margin-top: 16px;
}

.short-title {
  margin-bottom: 6px;
  font-size: 14px;
  font-weight: 600;
}

.hint {
  margin-left: auto;
  color: #909399;
  font-size: 12px;
}

.turn {
  padding: 12px 0;
  border-bottom: 1px solid #f0f0f0;
}

.turn:last-child {
  border-bottom: none;
}

.turn-meta {
  color: #a8abb2;
  font-size: 12px;
}

.turn-question {
  margin-top: 6px;
  color: #1677ff;
  font-size: 13px;
  line-height: 1.6;
}

.turn-answer {
  margin-top: 4px;
  color: #606266;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
}
</style>
