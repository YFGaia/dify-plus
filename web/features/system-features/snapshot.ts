import { zGetSystemFeaturesResponse } from '@dify/contracts/api/console/system-features/zod.gen'

// Require the bootstrap shape before generated defaults can turn a partial
// response into a successful, indefinitely cached snapshot.
const shape = zGetSystemFeaturesResponse.shape
const systemFeaturesSnapshot = zGetSystemFeaturesResponse.extend({
  branding: shape.branding.removeDefault().extend({
    enabled: shape.branding.removeDefault().shape.enabled.removeDefault(),
  }),
  license: shape.license.removeDefault().extend({
    status: shape.license.removeDefault().shape.status.removeDefault(),
  }),
  webapp_auth: shape.webapp_auth.extend({
    enabled: shape.webapp_auth.shape.enabled.removeDefault(),
    allow_public_access: shape.webapp_auth.shape.allow_public_access.removeDefault(),
  }),
  enable_email_code_login: shape.enable_email_code_login.removeDefault(),
  enable_email_password_login: shape.enable_email_password_login.removeDefault(),
  enable_social_oauth_login: shape.enable_social_oauth_login.removeDefault(),
  is_allow_register: shape.is_allow_register.removeDefault(),
  sso_enforced_for_signin: shape.sso_enforced_for_signin.removeDefault(),
})

export const parseSystemFeaturesSnapshot = (response: unknown) =>
  systemFeaturesSnapshot.parse(response)
