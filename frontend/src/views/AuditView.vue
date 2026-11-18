<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { clearAuditLogs, exportAuditLogs, fetchAuditLogs } from '../api/audit'

const loading = ref(false)
const exporting = ref(false)
const rows = ref([])
const total = ref(0)

const roleLabels = {
  admin: '管理员',
  procurement: '采购',
  logistics: '物流',
  quality: '质量',
  finance: '财务',
}
const actionLabels = {
  'approval.created': '提交审批',
  'approval.approve': '通过审批',
  'approval.reject': '驳回审批',
}

function formatTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  loading.value = true
  try {
    rows.value = await fetchAuditLogs(100)
    total.value = rows.value.length
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '加载失败')
  } finally {
    loading.value = false
  }
}

async function exportCsv() {
  exporting.value = true
  try {
    const blob = await exportAuditLogs(1000)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `audit_${Date.now()}.csv`
    link.click()
    URL.revokeObjectURL(url)
    ElMessage.success('导出成功')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '导出失败')
  } finally {
    exporting.value = false
  }
}

async function clearAll() {
  try {
    await ElMessageBox.confirm(
      '确定要清空全部审计日志吗？此操作不可恢复。',
      '确认清空',
      { confirmButtonText: '清空', cancelButtonText: '取消', type: 'danger' },
    )
  } catch {
    return
  }
  try {
    const result = await clearAuditLogs()
    ElMessage.success(`已清空 ${result.deleted} 条日志`)
    await load()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail ?? '清空失败')
  }
}

onMounted(load)
</script>

<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      <el-button :loading="exporting" @click="exportCsv">导出 CSV</el-button>
      <el-button type="danger" plain @click="clearAll">清空日志</el-button>
      <span class="hint">仅记录审批全链路</span>
    </div>

    <el-table v-loading="loading" :data="rows" size="small">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="actor" label="用户" width="120" />
      <el-table-column label="角色" width="90">
        <template #default="{ row }">
          {{ roleLabels[row.actor_role] ?? '未知' }}
        </template>
      </el-table-column>
      <el-table-column label="动作" width="110">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">
            {{ actionLabels[row.action] ?? row.action }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="target_id" label="对象" width="210" />
      <el-table-column label="详情" min-width="240">
        <template #default="{ row }">
          {{ row.detail ? JSON.stringify(row.detail) : '—' }}
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}

.hint {
  color: #a8abb2;
  font-size: 12px;
}
</style>