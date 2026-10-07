import type {
  ForwardToken,
  OAuth2Config,
  QuotaListResponse,
  TestResult,
} from '@/models/system-manage-extend'
// Extend: 系统管理 API 服务封装
import { del, get, post } from '@/service/base'

// ==================== 钉钉 ====================
export const dingtalkTestCallback = (code: string) =>
  post<TestResult>('/system-manage-extend/integration/dingtalk/test-callback', { body: { code } })

// ==================== OAuth2 ====================
export const getOAuth2Config = () => get<OAuth2Config>('/system-manage-extend/integration/oauth2')

export const setOAuth2Config = (data: Partial<OAuth2Config>) =>
  post<{ result: string }>('/system-manage-extend/integration/oauth2', { body: data })

export const testOAuth2Connection = (data: Partial<OAuth2Config>) =>
  post<TestResult>('/system-manage-extend/integration/oauth2/test', { body: data })

// ==================== 转发 Token ====================
export const getForwardTokens = () =>
  get<{ tokens: ForwardToken[] }>('/system-manage-extend/forward-tokens')

export const createForwardToken = (name: string) =>
  post<ForwardToken>('/system-manage-extend/forward-tokens', { body: { name } })

export const deleteForwardToken = (seq: number) =>
  del(`/system-manage-extend/forward-tokens/${seq}`)

// ==================== 用户额度管理 ====================

export const getQuotaList = (params: { page: number; page_size: number; keyword?: string }) =>
  get<QuotaListResponse>('/system-manage-extend/quota-management', { params })

export const setUserQuota = (data: { account_id: string; quota: number }) =>
  post<{ result: string }>('/system-manage-extend/quota-management/set', { body: data })
