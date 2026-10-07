import type { AppIconType } from '@/types/app'

type AppBasicInfo = {
  id: string
  mode: string
  icon_type: AppIconType | null
  icon: string
  icon_background: string
  icon_url: string
  name: string
  description: string
  use_icon_as_answer_icon: boolean
}

export type AppCategory = string // 上游 1.14.2 起分类为自由字符串（fork 的「未分类」兼容）

export type App = {
  app: AppBasicInfo
  installed_id?: string // 二开部分 新增 installed_id（仅应用中心 /installed/apps 响应携带）
  app_id: string
  description: string
  copyright: string
  privacy_policy: string | null
  custom_disclaimer: string | null
  categories: AppCategory[]
  position: number
  is_listed: boolean
  install_count: number
  installed: boolean
  editable: boolean
  is_agent: boolean
  can_trial: boolean
}
