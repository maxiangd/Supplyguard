<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  clearSession,
  createLongTermMemory,
  deleteConversation,
  deleteLongTermMemory,
  getConversation,
  listConversations,
  listLongTermMemories,
  listRiskMemories,
  updateLongTermMemory,
} from '../api/memory'

const SESSION_KEY = 'chat_session_id'

const activeTab = ref('session')
const sessionId = ref(sessionStorage.getItem(SESSION_KEY) || 'default')
const sessionTurns = ref([])
const sessionLoading = ref(false)
const sessions = ref([])
const sessionsLoading = ref(false)

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

async function loadSessions() {
  sessionsLoading.value = true
  try {
    const data = await listConversations()
    sessions.value = data.sessions
    // 默认选中最近一条，避免前端 sessionStorage 丢失后页面显示为空
    if (
      data.sessions.length &&
      !data.sessions.some((item) => item.session_id === sessionId.value)
    ) {
      sessionId.value = data.sessions[0].session_id
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '会话列表加载失败')
    sessions.value = []
  } finally {
    sessionsLoading.value = false
  }
}

function openSession(row) {
  sessionId.value = row.session_id
  loadSession()
}

async function loadSession() {
  sessionLoading.value = true
  try {
    const data = await getConversation(sessionId.value)
    sessionTurns.value = data.turns
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '会话加载失败')
    sessionTurns.value = []
  } finally {
    sessionLoading.value = false
  }
}

async function clearCurrentSession() {
  try {
    await deleteConversation(sessionId.value)
    // 永久档案删除后，Redis 短期记忆也一并清掉
    try {
      await clearSession(sessionId.value)
    } catch {
      // Redis 不可用时忽略
    }
    sessionTurns.value = []
    await loadSessions()
    if (sessions.value.length) {
      sessionId.value = sessions.value[0].session_id
      await loadSession()
    }
    ElMessage.success('会话记录已删除')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '删除失败')
  }
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
  await loadSessions()
  await loadSession()
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
          风险问答永久保存在 PostgreSQL；Redis 只保留最近几轮用于多轮追问；长期记忆保存偏好与历史风险
        </div>
      </div>
    </div>
  </el-card>

  <el-card shadow="never" class="row">
    <el-tabs v-model="activeTab">
      <el-tab-pane label="问答会话" name="session">
        <el-table
          v-loading="sessionsLoading"
          :data="sessions"
          size="small"
          class="session-table"
          :row-class-name="
            ({ row }) => (row.session_id === sessionId ? 'active-session' : '')
          "
        >
          <el-table-column label="会话" width="110">
            <template #default="{ row }">
              {{ row.session_id === 'default' ? '默认会话' : row.session_id.slice(0, 8) }}
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
          <el-table-column label="最近活动" width="170">
            <template #default="{ row }">
              {{ formatTime(row.last_timestamp * 1000) }}
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openSession(row)">
                查看
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty
          v-if="!sessionsLoading && !sessions.length"
          :image-size="60"
          description="还没有问答记录，去风险问答页提问后会出现在这里"
        />

        <div class="toolbar">
          <el-input
            v-model="sessionId"
            placeholder="按会话 ID 查看"
            style="width: 320px"
            clearable
          />
          <el-button type="primary" :loading="sessionLoading" @click="loadSession">
            查询
          </el-button>
          <el-button :disabled="!sessionTurns.length" @click="clearCurrentSession">
            清空该会话
          </el-button>
          <span class="hint">共 {{ sessionTurns.length }} 轮</span>
        </div>

        <el-empty
          v-if="!sessionTurns.length"
          :image-size="60"
          description="该会话暂无历史，去问答页提问后会出现在这里"
        />
        <div v-for="(turn, index) in sessionTurns.slice().reverse()" :key="index" class="turn">
          <div class="turn-head">
            <span class="turn-index">第 {{ sessionTurns.length - index }} 轮</span>
            <el-tag v-if="turn.risk_level" size="small" :type="levelType(turn.risk_level)">
              {{ turn.risk_level }}
            </el-tag>
            <el-tag
              v-for="name in turn.entity_names ?? []"
              :key="name"
              size="small"
              effect="plain"
            >
              {{ name }}
            </el-tag>
            <span v-if="turn.approval_id" class="turn-meta">
              审批单 {{ turn.approval_id }}
            </span>
            <span class="turn-meta">{{ formatTime(turn.timestamp * 1000) }}</span>
          </div>
          <div class="turn-question">Q：{{ turn.question }}</div>
          <div class="turn-answer">A：{{ turn.answer }}</div>
          <div v-if="turn.resolved_question && turn.resolved_question !== turn.question" class="turn-meta">
            追问补全：{{ turn.resolved_question }}
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

.turn-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.turn-index {
  color: #303133;
  font-size: 13px;
  font-weight: 600;
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
