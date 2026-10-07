import type {
  CasdoorConfiguration,
  CasdoorConfigurationInput,
  CasdoorConfigurationResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import {
  zCasdoorConfigurationInput,
  zCasdoorConfigurationResponse,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'

export type DraftErrors = Record<string, 'invalidField' | 'invalidUrl' | 'invalidMapping'>
export const targetRoles = ['admin', 'editor', 'normal'] as const

// Role organization is derived from the sign-in application, never a separate
// editable value. Keep the exact role name and preserve unoccupied slots.
export function inheritRoleOrganization(configuration: CasdoorConfiguration): CasdoorConfiguration {
  return {
    ...configuration,
    workspace_mappings: configuration.workspace_mappings?.map((mapping) => ({
      ...mapping,
      ...Object.fromEntries(
        targetRoles.map((role) => [
          role,
          mapping[role]
            ? { ...mapping[role], organization: configuration.organization }
            : mapping[role],
        ]),
      ),
    })),
  }
}

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
  for (const revision of [response.draft, response.active]) {
    if (!revision) continue
    const summaries = revision.validation ?? []
    if (
      new Set(summaries.map((summary) => summary.kind)).size !== summaries.length ||
      summaries.some((summary) => summary.revision_id !== revision.revision_id)
    )
      return null
    if (
      revision.diagnostic &&
      (revision.diagnostic.revision_id !== revision.revision_id ||
        revision.diagnostic.namespace_id !== revision.namespace_id)
    )
      return null
  }
  return response
}

export function initialConfiguration(response: CasdoorConfigurationResponse): CasdoorConfiguration {
  const source = response.draft?.configuration ?? response.active?.configuration
  const {
    certificates: _certificates,
    signing_key_mode: _mode,
    ...editable
  } = structuredClone(source ?? { certificates: [], signing_key_mode: null })
  return inheritRoleOrganization({
    browser_frontend_url: '',
    backend_api_url: '',
    expected_issuer: '',
    organization: '',
    application: '',
    client_id: '',
    button_text: 'Casdoor',
    default_workspace_id: '',
    workspace_mappings: [],
    scope: 'openid email profile',
    default_normal_fallback: true,
    name_sync: 'fill_empty',
    avatar_sync: false,
    avatar_mode: 'fill_empty',
    ...editable,
    schema_version: 2,
    signing_key_mode: 'automatic',
    // These optional runtime capabilities have no accepted implementation yet.
    rp_logout: false,
    self_unlink: false,
  })
}

// Only omit values owned by backend defaults. Explicit advanced settings survive
// mode switches and remain attached to the saved revision.
export function configurationInput(configuration: CasdoorConfiguration): CasdoorConfigurationInput {
  const endpoint = configuration.browser_frontend_url.replace(/\/$/, '')
  const {
    backend_api_url,
    expected_issuer,
    certificates: _certificates,
    signing_key_mode: _mode,
    default_workspace_id: _workspace,
    ...rest
  } = inheritRoleOrganization(configuration)
  return {
    ...rest,
    ...(backend_api_url && backend_api_url !== endpoint ? { backend_api_url } : {}),
    ...(expected_issuer && expected_issuer !== endpoint ? { expected_issuer } : {}),
    schema_version: 2,
  }
}

export function validateConfiguration(configuration: CasdoorConfiguration): DraftErrors {
  const errors: DraftErrors = {}
  const input = configurationInput(configuration)
  const parsed = zCasdoorConfigurationInput.safeParse(input)
  if (!parsed.success) {
    for (const issue of parsed.error.issues) errors[issue.path.join('.')] = 'invalidField'
  }
  for (const field of ['browser_frontend_url', 'backend_api_url', 'expected_issuer'] as const) {
    try {
      const value = configuration[field] || configuration.browser_frontend_url.replace(/\/$/, '')
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
  for (const [index, mapping] of (input.workspace_mappings ?? []).entries()) {
    const path = `workspace_mappings.${index}`
    if (workspaces.has(mapping.workspace_id)) errors[`${path}.workspace_id`] = 'invalidMapping'
    workspaces.add(mapping.workspace_id)
    const refs = new Set<string>()
    for (const role of targetRoles) {
      const ref = mapping[role]
      if (!ref) continue
      const key = JSON.stringify([ref.organization, ref.name])
      if (!ref.organization.trim() || !ref.name.trim() || refs.has(key))
        errors[`${path}.${role}`] = 'invalidMapping'
      refs.add(key)
    }
  }
  if (new Set([...workspaces, configuration.default_workspace_id]).size > 100)
    errors.workspace_mappings = 'invalidMapping'
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
