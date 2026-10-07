import type { GetLoginConfigResponse } from '@dify/contracts/api/console/login-config/types.gen'
import type { GetSystemFeaturesResponse } from '@dify/contracts/api/console/system-features/types.gen'
import { zGetLoginConfigResponse } from '@dify/contracts/api/console/login-config/zod.gen'
import { parseSystemFeaturesSnapshot } from './snapshot'

export type LoginConfigExtend = Pick<
  GetLoginConfigResponse,
  | 'is_custom_auth2'
  | 'is_custom_auth2_logout'
  | 'ding_talk_client_id'
  | 'ding_talk_corp_id'
  | 'ding_talk'
  | 'rmb_to_usd_rate'
>

// A view composed by Console consumers, never the public snapshot's DTO/cache.
export type SystemFeaturesExtend = GetSystemFeaturesResponse & LoginConfigExtend

export const defaultSystemFeaturesExtendFields: LoginConfigExtend = {
  is_custom_auth2: false,
  is_custom_auth2_logout: '',
  ding_talk_client_id: '',
  ding_talk_corp_id: '',
  ding_talk: false,
  rmb_to_usd_rate: 7.26,
}

export const parseLoginConfig = (response: unknown): GetLoginConfigResponse => {
  parseSystemFeaturesSnapshot(response)
  return zGetLoginConfigResponse.parse(response)
}

export function asSystemFeaturesExtend(
  features: GetSystemFeaturesResponse,
  loginConfig?: GetLoginConfigResponse,
): SystemFeaturesExtend {
  const config = loginConfig ? parseLoginConfig(loginConfig) : defaultSystemFeaturesExtendFields
  return {
    ...parseSystemFeaturesSnapshot(features),
    is_custom_auth2: config.is_custom_auth2,
    is_custom_auth2_logout: config.is_custom_auth2_logout,
    ding_talk_client_id: config.ding_talk_client_id,
    ding_talk_corp_id: config.ding_talk_corp_id,
    ding_talk: config.ding_talk,
    rmb_to_usd_rate: config.rmb_to_usd_rate,
  }
}
