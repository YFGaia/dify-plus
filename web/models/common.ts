import type { GetAccountProfileResponse } from '@dify/contracts/api/console/account/types.gen'
import type { Role } from './access-control'
import type { I18nText } from '@/i18n-config/language'
import type { Model } from '@/types/app'

export type CommonResponse = {
  result: 'success' | 'fail'
}

export type SetupStatusResponse = {
  step: 'finished' | 'not_started'
  setup_at?: Date
}

export type InitValidateStatusResponse = {
  status: 'finished' | 'not_started'
}

export type LangGeniusVersionResponse = {
  current_version: string
  latest_version: string
  version: string
  release_date: string
  release_notes: string
  can_auto_update: boolean
  current_env: string
}

export type Member = Pick<GetAccountProfileResponse, 'id' | 'name' | 'email' | 'avatar_url'> & {
  avatar: string
  last_login_at?: string
  last_active_at?: string
  created_at?: string
  status: 'pending' | 'active' | 'banned' | 'closed'
  role: 'owner' | 'admin' | 'editor' | 'normal' | 'dataset_operator'
  roles: Role[]
}

const ProviderName = {
  OPENAI: 'openai',
  AZURE_OPENAI: 'azure_openai',
  ANTHROPIC: 'anthropic',
  Replicate: 'replicate',
  HuggingfaceHub: 'huggingface_hub',
  MiniMax: 'minimax',
  Spark: 'spark',
  Tongyi: 'tongyi',
  ChatGLM: 'chatglm',
} as const
type ProviderName = typeof ProviderName[keyof typeof ProviderName]
type ProviderAzureToken = {
  openai_api_base?: string
  openai_api_key?: string
}
type ProviderAnthropicToken = {
  anthropic_api_key?: string
}
type Provider = {
  [Name in ProviderName]: {
    provider_name: Name
  } & {
    provider_type: 'custom' | 'system'
    is_valid: boolean
    is_enabled: boolean
    last_used: string
    token?: string | ProviderAzureToken | ProviderAnthropicToken
  }
}[ProviderName]

export type IWorkspace = {
  id: string
  name: string
  plan: string
  status: string
  created_at: number
  last_opened_at?: number | null
  current: boolean
}

export type ICurrentWorkspace = Omit<IWorkspace, 'current'> & {
  role: 'owner' | 'admin' | 'editor' | 'dataset_operator' | 'normal'
  providers: Provider[]
  trial_credits: number
  trial_credits_used: number
  next_credit_reset_date: number
  trial_end_reason?: string
  custom_config?: {
    remove_webapp_brand?: boolean
    replace_webapp_logo?: string
  }
  // ----------------------- 二开部分Start 添加用户权限 - --------------------------------
  admin_extend?: boolean
  tenant_extend?: boolean
  // ----------------------- 二开部分Stop 添加用户权限 - --------------------------------
}

export type DataSourceNotionPage = {
  page_icon: null | {
    type: string | null
    url: string | null
    emoji: string | null
  }
  page_id: string
  page_name: string
  parent_id: string
  type: string
  is_bound: boolean
}

export type NotionPage = DataSourceNotionPage & {
  workspace_id: string
}

export type DataSourceNotionPageMap = Record<string, DataSourceNotionPage & { workspace_id: string }>

export type DataSourceNotionWorkspace = {
  workspace_name: string
  workspace_id: string
  workspace_icon: string | null
  total?: number
  pages: DataSourceNotionPage[]
}

export const DataSourceProvider = {
  fireCrawl: 'firecrawl',
  jinaReader: 'jinareader',
  waterCrawl: 'watercrawl',
} as const
export type DataSourceProvider = typeof DataSourceProvider[keyof typeof DataSourceProvider]

export type FileUploadConfigResponse = {
  batch_count_limit: number
  image_file_size_limit?: number | string // default is 10MB
  image_file_batch_limit: number // default is 10, for dataset attachment upload only
  single_chunk_attachment_limit: number // default is 10, for dataset attachment upload only
  attachment_image_file_size_limit: number // default is 2MB, for dataset attachment upload only
  file_size_limit: number // default is 15MB
  audio_file_size_limit?: number // default is 50MB
  video_file_size_limit?: number // default is 100MB
  workflow_file_upload_limit?: number // default is 10
  file_upload_limit: number // default is 5
}

export type InvitationResult = {
  status: 'success'
  email: string
  url: string
} | {
  status: 'already_member'
  email: string
  message?: string
} | {
  status: 'failed'
  email: string
  message: string
}

export type InvitationResponse = CommonResponse & {
  invitation_results: InvitationResult[]
}

// Extend Start: Connecting Fees Frontend
export type BillingProps = {
  path: string
  value: string
  address: string
  header: string[]
  description: string
}

export type ApiForwardedBillingProps = {
  para: string
  price: number
  remark: string
  benchmark: string
  operation: number
  children: ApiForwardedBillingProps[]
}

export type ApiForwardedProps = {
  para: string
  billing: ApiForwardedBillingProps[]
  children: []
  price: number
  benchmark: number
  operation: number
  content_type: number
  forwarding_id: string
  description: string
  models: string
  remark: string
  path: string
  id: string
  status: boolean
}
// Extend Stop: Connecting Fees Frontend

export type CodeBasedExtensionForm = {
  type: string
  label: I18nText
  variable: string
  required: boolean
  options: { label: I18nText, value: string }[]
  default: string
  placeholder: string
  max_length?: number
}

export type CodeBasedExtensionItem = {
  name: string
  label: I18nText
  form_schema: CodeBasedExtensionForm[]
}
export type CodeBasedExtension = {
  module: string
  data: CodeBasedExtensionItem[]
}

export type ExternalDataTool = {
  type?: string
  label?: string
  icon?: string
  icon_background?: string
  variable?: string
  enabled?: boolean
  config?: {
    api_based_extension_id?: string
  } & Partial<Record<string, string | undefined>>
}

export type StructuredOutputRulesRequestBody = {
  instruction: string
  model_config: Model
}

export type StructuredOutputRulesResponse = {
  output: string
  error?: string
}
