export function dingtalkErrorMessageKey(error: unknown) {
  const code = typeof error === 'string' ? error : error instanceof Error ? error.message : ''
  switch (code) {
    case 'enterprise_email_required':
      return 'systemManage.dingtalk.enterpriseEmailRequired'
    case 'binding_required':
    case 'identity_conflict':
      return 'systemManage.dingtalk.accountBindingRequired'
    case 'invalid_transaction':
    case 'invalid_authorization_code':
    case 'registration_expired':
      return 'systemManage.dingtalk.registration.EXPIRED'
    case 'administrator_session_required':
    case 'system_management_forbidden':
    case 'registration_owner_mismatch':
    case 'enterprise_member_required':
    case 'account_unavailable':
    case 'workspace_membership_required':
      return 'systemManage.common.noPermission'
    case 'configuration_changed':
      return 'systemManage.dingtalk.emailLookup.loadFailed'
    case 'credentials_required':
      return 'systemManage.dingtalk.credentialsMissing'
    case 'login_disabled':
      return 'systemManage.common.disabled'
    case 'login_verification_required':
      return 'systemManage.dingtalk.loginRequired'
    default:
      return 'systemManage.dingtalk.loginFailed'
  }
}
