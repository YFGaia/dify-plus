// Extend: 系统管理功能类型定义

export type DingTalkConfig = {
  status: boolean
  corp_id: string
  agent_id: string
  app_key: string
  app_secret: string
  config: {
    email_api?: {
      url: string
      key: string
      [key: string]: any
    }
    forward_config?: {
      tokens: ForwardToken[]
    }
    [key: string]: any
  }
}

export type OAuth2Config = {
  status: boolean
  app_id: string
  app_secret: string
  config: {
    server_url: string
    authorize_url: string
    token_url: string
    userinfo_url: string
    scope: string
    button_text: string
    logout_url: string
    redirect_uri: string
    [key: string]: any
  }
}

export type ForwardToken = {
  seq: number
  name: string
  token: string
  created_at: string
}

export type TestResult = {
  result: string
  message?: string
  status_code?: number
  user_info?: Record<string, any>
}

// ==================== 用户额度管理 ====================

export type QuotaListItem = {
  account_id: string
  ranking: number
  name: string
  email: string
  avatar: string | null
  used_quota: number
  total_quota: number
  balance: number
}

export type QuotaListResponse = {
  list: QuotaListItem[]
  total: number
  page: number
  page_size: number
}
