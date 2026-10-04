import type {
  CasdoorConfiguration,
  CasdoorConfigurationResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import {
  zCasdoorConfiguration,
  zCasdoorConfigurationResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'

export type DraftErrors = Record<
  string,
  'invalidField' | 'invalidUrl' | 'invalidMapping' | 'invalidCertificate'
>
export const targetRoles = ['admin', 'editor', 'normal'] as const

export function parseServerConfiguration(data: unknown) {
  const parsed = zCasdoorConfigurationResponse.safeParse(data)
  if (
    !parsed.success ||
    typeof data !== 'object' ||
    data === null ||
    !('etag' in data) ||
    typeof data.etag !== 'number' ||
    !('enabled' in data) ||
    typeof data.enabled !== 'boolean'
  )
    return null
  const response = parsed.data
  if (
    (response.draft?.revision_id ?? null) !== (response.draft_revision_id ?? null) ||
    (response.active?.revision_id ?? null) !== (response.active_revision_id ?? null)
  )
    return null
  return response
}

export function initialConfiguration(response: CasdoorConfigurationResponse): CasdoorConfiguration {
  const source = response.draft?.configuration ?? response.active?.configuration
  return {
    schema_version: 1,
    browser_frontend_url: '',
    backend_api_url: '',
    expected_issuer: '',
    organization: '',
    application: '',
    client_id: '',
    button_text: 'Casdoor',
    default_workspace_id: '',
    workspace_mappings: [],
    certificates: [],
    scope: 'openid email profile',
    default_normal_fallback: true,
    name_sync: 'fill_empty',
    avatar_sync: false,
    avatar_mode: 'fill_empty',
    ...structuredClone(source ?? {}),
    // These optional runtime capabilities have no accepted implementation yet.
    rp_logout: false,
    self_unlink: false,
  }
}

export function validateConfiguration(configuration: CasdoorConfiguration): DraftErrors {
  const errors: DraftErrors = {}
  const parsed = zCasdoorConfiguration.safeParse(configuration)
  if (!parsed.success) {
    for (const issue of parsed.error.issues) errors[issue.path.join('.')] = 'invalidField'
  }
  for (const field of ['browser_frontend_url', 'backend_api_url', 'expected_issuer'] as const) {
    try {
      const value = configuration[field]
      const url = new URL(value)
      if (
        !['http:', 'https:'].includes(url.protocol) ||
        !url.hostname ||
        url.username ||
        url.password ||
        url.hash ||
        /[\s\\\u007F]/.test(value) ||
        (field === 'expected_issuer' && url.search)
      )
        errors[field] = 'invalidUrl'
    } catch {
      errors[field] = 'invalidUrl'
    }
  }
  const workspaces = new Set<string>()
  for (const [index, mapping] of (configuration.workspace_mappings ?? []).entries()) {
    const path = `workspace_mappings.${index}`
    if (workspaces.has(mapping.workspace_id)) errors[`${path}.workspace_id`] = 'invalidMapping'
    workspaces.add(mapping.workspace_id)
    const refs = new Set<string>()
    for (const role of targetRoles) {
      const ref = mapping[role]
      if (!ref) continue
      const key = JSON.stringify([ref.organization, ref.name])
      if (
        !ref.organization.trim() ||
        !ref.name.trim() ||
        ref.organization !== configuration.organization ||
        refs.has(key)
      )
        errors[`${path}.${role}`] = 'invalidMapping'
      refs.add(key)
    }
  }
  if (new Set([...workspaces, configuration.default_workspace_id]).size > 100)
    errors.workspace_mappings = 'invalidMapping'
  const kids = new Set<string>()
  for (const [index, certificate] of (configuration.certificates ?? []).entries()) {
    const path = `certificates.${index}`
    if (
      !certificate.pem.trim().startsWith('-----BEGIN CERTIFICATE-----') ||
      certificate.pem.includes('PRIVATE KEY')
    )
      errors[`${path}.pem`] = 'invalidCertificate'
    if (certificate.kid && kids.has(certificate.kid)) errors[`${path}.kid`] = 'invalidCertificate'
    if (certificate.kid) kids.add(certificate.kid)
    const from = Date.parse(certificate.not_before)
    const until = Date.parse(certificate.accept_until)
    if (!certificate.not_before.endsWith('Z') || !Number.isFinite(from))
      errors[`${path}.not_before`] = 'invalidCertificate'
    if (!certificate.accept_until.endsWith('Z') || !Number.isFinite(until) || from >= until)
      errors[`${path}.accept_until`] = 'invalidCertificate'
  }
  return errors
}

export function plannedCallback(apiBase: string | undefined): string | undefined {
  if (!apiBase) return undefined
  try {
    const url = new URL(apiBase)
    if (
      !['https:', 'http:'].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      return undefined
    return `${apiBase.replace(/\/$/, '')}/auth/casdoor/callback`
  } catch {
    return undefined
  }
}
