/** 记忆 API：短期会话 + 长期记忆 */
import api from './client'

// client 的响应拦截器已经把 response.data 解包返回了，
// 这里不能再取一次 .data，否则拿到的是 undefined。

export function listConversations() {
  return api.get('/memory/conversations')
}

export function getConversation(sessionId) {
  return api.get(`/memory/conversations/${sessionId}`)
}

export function deleteConversation(sessionId) {
  return api.delete(`/memory/conversations/${sessionId}`)
}

export function getSessionHistory(sessionId) {
  return api.get('/memory/session', { params: { session_id: sessionId } })
}

export function listSessions() {
  return api.get('/memory/sessions')
}

export function clearSession(sessionId) {
  return api.delete('/memory/session', { params: { session_id: sessionId } })
}

export function listLongTermMemories(category) {
  return api.get('/memory/long-term', { params: { category } })
}

export function createLongTermMemory(data) {
  return api.post('/memory/long-term', data)
}

export function rememberConversation(data) {
  return api.post('/memory/remember', data)
}

export function getLongTermMemory(memoryId) {
  return api.get(`/memory/long-term/${memoryId}`)
}

export function updateLongTermMemory(memoryId, data) {
  return api.patch(`/memory/long-term/${memoryId}`, data)
}

export function deleteLongTermMemory(memoryId) {
  return api.delete(`/memory/long-term/${memoryId}`)
}
