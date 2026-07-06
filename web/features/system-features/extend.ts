import type { GetSystemFeaturesResponse } from '@dify/contracts/api/console/system-features/types.gen'

// Extend: start 钉钉和OAuth2登录字段 — 后端 /system-features（fork 版 login_config）实际返回、
// 上游生成的 contract 类型未包含的二开字段。上游把 SystemFeatures 类型迁入
// packages/contracts 生成物后，fork 不改生成物，改用交叉类型在此补齐。
export type SystemFeaturesExtend = GetSystemFeaturesResponse & {
  is_custom_auth2: string // extend: Customizing AUTH2
  is_custom_auth2_button: string // extend: Customizing AUTH2 button text
  is_custom_auth2_logout: string // extend: AUTH2 logout url
  ding_talk_client_id: string // Extend: DingTalk third-party login
  ding_talk_corp_id: string // Extend: DingTalk sidebar login
  ding_talk: boolean // Extend: switch DingTalk sidebar login
  rmb_to_usd_rate: number // extend: 后端配置 RMB_TO_USD_RATE 下发的汇率（额度徽章换算用）
}
// Extend: end

export const defaultSystemFeaturesExtendFields = {
  is_custom_auth2: '',
  is_custom_auth2_button: '',
  is_custom_auth2_logout: '',
  ding_talk_client_id: '',
  ding_talk_corp_id: '',
  ding_talk: false,
  rmb_to_usd_rate: 7.26, // extend: 旧版后端未下发该字段时的回退默认值
}

export function asSystemFeaturesExtend(features: GetSystemFeaturesResponse): SystemFeaturesExtend {
  return { ...defaultSystemFeaturesExtendFields, ...features }
}
