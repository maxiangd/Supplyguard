/** 记忆 API：短期会话 + 长期记忆 */
import api from './client'

export function listConversations() {
  return api.get('/memory/conversations').then(r => r.data)
}

export function getConversation(sessionId) {
  return api.get(`/memory/conversations/${sessionId}`).then(r => r.data)
}

export function deleteConversation(sessionId) {
  return api.delete(`/memory/conversations/${sessionId}`).then(r => r.data)
}

export function getSessionHistory(sessionId) {
  return api.get('/memory/session', { params: { session_id: sessionId } }).then(r => r.data)
}

export function listSessions() {
  return api.get('/memory/sessions').then(r => r.data)
}

export function clearSession(sessionId) {
  return api.delete('/memory/session', { params: { session_id: sessionId } }).then(r => r.data)
}

export function listRiskMemories({ entityId, level, limit } = {}) {
  return api
    .get('/memory/risks', { params: { entity_id: entityId, level, limit } })
    .then(r => r.data)
}

export function listLongTermMemories(category) {
  return api.get('/memory/long-term', { params: { category } }).then(r => r.data)
}

export function createLongTermMemory(data) {
  return api.post('/memory/long-term', data).then(r => r.data)
}

export function getLongTermMemory(memoryId) {
  return api.get(`/memory/long-term/${memoryId}`).then(r => r.data)
}

export function updateLongTermMemory(memoryId, data) {
  return api.patch(`/memory/long-term/${memoryId}`, data).then(r => r.data)
}

export function deleteLongTermMemory(memoryId) {
  return api.delete(`/memory/long-term/${memoryId}`).then(r => r.data)
}