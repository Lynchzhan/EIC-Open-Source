import axios from 'axios'
import { parseJsonSafe } from './jsonNumbers'
import { message } from '@/app/ui/controls'
import { toUserMessage } from './userMessages'
import { inExperience, experienceAdapter } from '@/app/experience/runtime'

function extractError(error) {
  const detail = error?.response?.data?.detail
  const status = error?.response?.status
  const fallback = status === 401 ? '登录已失效，请重新登录'
    : status === 403 ? '没有执行此操作的权限'
    : status === 404 ? '目标不存在或不可访问'
    : status === 409 ? '操作与当前状态冲突，请刷新后重试'
    : status === 422 ? '提交的数据格式不正确，请检查必填项和字段内容'
    : status === 429 ? '操作过于频繁，请稍后重试'
    : status >= 500 ? '服务暂不可用，请稍后重试' : '请求未完成，请检查输入后重试'
  if (typeof detail === 'string') return toUserMessage(detail, fallback)
  if (Array.isArray(detail)) {
    return '提交的数据格式不正确，请检查必填项和字段内容'
  }
  if (detail && typeof detail === 'object') return toUserMessage(detail.errMsg || detail.err_msg, fallback)
  return toUserMessage(error?.response?.data?.errMsg || error?.response?.data?.err_msg || error?.code || error?.message, fallback)
}

function wrapSuccess(data) {
  if (data && typeof data === 'object' && !Array.isArray(data) && 'code' in data && ('data' in data || 'isSuccess' in data || 'errMsg' in data)) {
    if (data.code !== 200 || data.isSuccess === false) {
      return { ...data, message: toUserMessage(data.message || data.errMsg || data.err_msg) }
    }
    return data
  }
  return { code: 200, isSuccess: true, data }
}

const myAxios = axios.create({
  baseURL: '',
  timeout: 60000,
  withCredentials: true,
  transformResponse: [parseJsonSafe],
})

myAxios.interceptors.request.use((config) => {
  // 显式设置的探测/观测超时保持优先；聊天 SSE 使用独立的流式客户端。
  if (config.timeout === myAxios.defaults.timeout) {
    const limits = { read:60000, write:120000, upload:300000, evaluation:300000 }
    const kind = config.eicRequestClass || (
      /\/api\/File\/(GetFile|GetFilePath)(?:\?|$)/.test(config.url || '') ? 'upload'
        : /Evaluate|Evaluation/.test(config.url || '') ? 'evaluation'
          : ['get','head','options'].includes(config.method || 'get') ? 'read' : 'write')
    config.timeout = limits[kind] || limits.write
  }
  if (inExperience()) {
    config.eicExperience = true
    config.headers.delete?.('Authorization')
    config.adapter = async cfg => {
      try { return await experienceAdapter(cfg) }
      catch (error) { error.config = cfg; throw error }
    }
    return config
  }
  const token = localStorage.getItem('token') || localStorage.getItem('eic-token')
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

myAxios.interceptors.response.use(
  (response) => {
    const newToken = response.headers?.newtoken
    if (newToken && !response.config?.eicExperience) {
      localStorage.setItem('token', newToken)
      localStorage.setItem('eic-token', newToken)
    }
    if (response.config?.responseType === 'blob' || response.data instanceof Blob) {
      return response.data
    }
    if (response.data === false) {
      const errMsg = '操作未生效'
      if (!response.config?.eicErrorHandled) message.error(errMsg)
      return Promise.reject(new Error(errMsg))
    }
    return wrapSuccess(response.data)
  },
  (error) => {
    if (inExperience() || error.config?.eicExperience) return Promise.reject(error)
    const status = error?.response?.status
    const errMsg = extractError(error)
    if (status === 401) {
      localStorage.removeItem('user')
      localStorage.removeItem('UserPermissions')
      localStorage.removeItem('token')
      localStorage.removeItem('eic-token')
      if (!window.location.pathname.includes('/login')) {
        message.error('请先登录')
        window.location.href = `/login?redirect=${encodeURIComponent(window.location.href)}`
      }
    } else if (error.config?.eicErrorHandled || axios.isCancel(error)) {
      return Promise.reject(new Error(errMsg, { cause:error }))
    } else if (status === 403) {
      message.error('接口无权限访问')
    } else if (errMsg) {
      message.error(errMsg)
    }
    return Promise.reject(new Error(errMsg, { cause: error }))
  }
)

export default myAxios

export function isMutationSuccess(response) {
  if (!response || response.code !== 200 || response.isSuccess === false || response.data === false) {
    return false
  }
  return true
}
