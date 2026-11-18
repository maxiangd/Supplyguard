import client from './client'

export function fetchAuditLogs(limit = 100) {
  return client.get('/audit', { params: { limit } })
}

export function clearAuditLogs() {
  return client.delete('/audit')
}

export function exportAuditLogs(limit = 1000) {
  return client.get('/audit/export', { params: { limit }, responseType: 'blob' })
}