import type {
  GetAuthCasdoorDisplayData,
  GetAuthCasdoorResultData,
  HeadCasdoorDisplayApiData,
} from './generated/api/console/auth/types.gen'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vite-plus/test'
import {
  zCasdoorDisplayResponse,
  zCasdoorRestrictedResultResponse,
  zGetAuthCasdoorResultQuery,
} from './generated/api/console/auth/zod.gen'
import { contractLoaders } from './generated/api/console/orpc.gen'

const syntheticUuid = '11111111-1111-4111-8111-111111111111'
const validRestrictedResult = {
  code: 'authorization_pending',
  correlation_id: syntheticUuid,
  retry_allowed: false,
} as const

type Operation = {
  '~orpc': {
    inputSchema?: { safeParse: (input: unknown) => { success: boolean } }
    outputSchema?: unknown
    route: { inputStructure?: string; method?: string; operationId?: string; path?: string }
  }
}

function collectOperations(value: unknown, result: Operation[] = []): Operation[] {
  if (!value || typeof value !== 'object') return result
  if ('~orpc' in value && typeof value['~orpc'] === 'object' && value['~orpc'] !== null) {
    result.push(value as Operation)
    return result
  }
  for (const child of Object.values(value)) collectOperations(child, result)
  return result
}

describe('independent native generated Casdoor auth contract', () => {
  it('loads both consumer GETs with the native route, schema references, and callable input shapes', async () => {
    const { auth } = await contractLoaders.auth()
    const result = auth.casdoor.result.get as Operation
    const display = auth.casdoor.display.get as Operation

    expect(result['~orpc'].route).toMatchObject({
      inputStructure: 'detailed',
      method: 'GET',
      operationId: 'getAuthCasdoorResult',
      path: '/auth/casdoor/result',
    })
    expect(display['~orpc'].route).toMatchObject({
      inputStructure: 'detailed',
      method: 'GET',
      operationId: 'getAuthCasdoorDisplay',
      path: '/auth/casdoor/display',
    })
    expect(
      result['~orpc'].inputSchema?.safeParse({ query: { handoff: 'a'.repeat(43) } }).success,
    ).toBe(true)
    expect(result['~orpc'].inputSchema?.safeParse({ query: {} }).success).toBe(false)
    expect(result['~orpc'].outputSchema).toBe(zCasdoorRestrictedResultResponse)
    expect(display['~orpc'].inputSchema).toBeUndefined()
    expect(display['~orpc'].outputSchema).toBe(zCasdoorDisplayResponse)

    const resultConsumerCall: GetAuthCasdoorResultData = {
      query: { handoff: 'a'.repeat(43) },
      url: '/auth/casdoor/result',
    }
    const displayConsumerCall: GetAuthCasdoorDisplayData = { url: '/auth/casdoor/display' }
    expect(resultConsumerCall.query).toEqual({ handoff: 'a'.repeat(43) })
    expect(displayConsumerCall.url).toBe('/auth/casdoor/display')
  })

  it('keeps HEAD as the sole display auxiliary SDK operation and omits every OPTIONS entry', async () => {
    const { auth } = await contractLoaders.auth()
    const operations = collectOperations(auth)
    const casdoor = operations.filter((operation) =>
      operation['~orpc'].route.path?.startsWith('/auth/casdoor/'),
    )
    const head = auth.casdoor.display.head as Operation

    expect(casdoor).toHaveLength(3)
    expect(head['~orpc'].route).toMatchObject({
      inputStructure: 'detailed',
      method: 'HEAD',
      operationId: 'head_casdoor_display_api',
      path: '/auth/casdoor/display',
    })
    expect(head['~orpc'].inputSchema).toBeUndefined()
    expect(head['~orpc'].outputSchema).toBeUndefined()
    expect(Object.keys(auth.casdoor.display).sort()).toEqual(['get', 'head'])
    expect(casdoor.some(({ '~orpc': { route } }) => route.method === 'OPTIONS')).toBe(false)

    const headData: HeadCasdoorDisplayApiData = { url: '/auth/casdoor/display' }
    expect(headData.url).toBe('/auth/casdoor/display')
  })

  it('requires a URL-safe 43-character handoff in the actual generated query schema', () => {
    const valid = 'Ab_9-'.repeat(8) + 'xyz'
    expect(valid).toHaveLength(43)
    expect(zGetAuthCasdoorResultQuery.parse({ handoff: valid })).toEqual({ handoff: valid })
    for (const invalid of ['', 'a'.repeat(42), 'a'.repeat(44), `${'a'.repeat(42)}+`]) {
      expect(zGetAuthCasdoorResultQuery.safeParse({ handoff: invalid }).success).toBe(false)
    }
    expect(zGetAuthCasdoorResultQuery.safeParse({}).success).toBe(false)
    expect(zGetAuthCasdoorResultQuery.safeParse({ handoff: 123 }).success).toBe(false)
  })

  it('accepts only the restricted result codes, UUID-shaped correlation, and literal false retry flag', () => {
    for (const code of [
      'authorization_pending',
      'role_snapshot_unknown',
      'workspace_unavailable',
    ]) {
      expect(zCasdoorRestrictedResultResponse.parse({ ...validRestrictedResult, code }).code).toBe(
        code,
      )
    }
    expect(
      zCasdoorRestrictedResultResponse.safeParse({
        ...validRestrictedResult,
        code: 'provider_unavailable',
      }).success,
    ).toBe(false)
    expect(
      zCasdoorRestrictedResultResponse.safeParse({
        ...validRestrictedResult,
        correlation_id: 'not-a-uuid',
      }).success,
    ).toBe(false)
    expect(
      zCasdoorRestrictedResultResponse.safeParse({ ...validRestrictedResult, retry_allowed: true })
        .success,
    ).toBe(false)
    expect(
      zCasdoorRestrictedResultResponse.safeParse({ code: validRestrictedResult.code }).success,
    ).toBe(false)
  })

  it('records actual Zod stripping and default behavior without claiming strictness or readiness', () => {
    const parsed = zCasdoorRestrictedResultResponse.parse({
      ...validRestrictedResult,
      extra: 'discarded',
    })
    expect(parsed).toEqual(validRestrictedResult)
    expect(parsed).not.toHaveProperty('extra')
    expect(zCasdoorDisplayResponse.parse({})).toEqual({
      button_text: 'Casdoor',
      enabled: false,
      start_path: '/console/api/auth/casdoor/login',
    })
    expect(zCasdoorDisplayResponse.safeParse({ start_path: '/other' }).success).toBe(false)
    expect(zCasdoorDisplayResponse.safeParse({ button_text: '' }).success).toBe(false)
    expect(zCasdoorDisplayResponse.safeParse({ button_text: 'x'.repeat(121) }).success).toBe(false)
  })

  it('preserves the ten pre-existing auth operation routes and their generated schemas', async () => {
    const { auth } = await contractLoaders.auth()
    const operations = collectOperations(auth)
    const oldOperations = operations.filter(
      ({ '~orpc': { route } }) => !route.path?.startsWith('/auth/casdoor/'),
    )

    expect(operations).toHaveLength(13)
    expect(oldOperations).toHaveLength(10)
    expect(new Set(oldOperations.map(({ '~orpc': { route } }) => route.operationId)).size).toBe(10)
    for (const {
      '~orpc': { route, inputSchema, outputSchema },
    } of oldOperations) {
      expect(route.method).toBeTruthy()
      expect(route.path).toBeTruthy()
      expect(route.inputStructure).toBe('detailed')
      expect(inputSchema || outputSchema).toBeTruthy()
    }
  })

  it('keeps all nineteen management operations and their actual route and schema metadata loadable', async () => {
    const { systemManageExtend } = await contractLoaders.systemManageExtend()
    const operations = collectOperations(systemManageExtend)

    expect(operations).toHaveLength(19)
    expect(new Set(operations.map(({ '~orpc': { route } }) => route.operationId)).size).toBe(19)
    for (const {
      '~orpc': { route, inputSchema, outputSchema },
    } of operations) {
      expect(route.method).toBeTruthy()
      expect(route.path).toBeTruthy()
      expect(route.inputStructure).toBe('detailed')
      expect(inputSchema || outputSchema).toBeTruthy()
    }
  })

  it('documents the four navigation/result paths, HEAD 405, bootstrap owner, and no Casdoor OPTIONS in raw and Markdown', () => {
    const raw = JSON.parse(
      readFileSync(new URL('./openapi/console-openapi.json', import.meta.url), 'utf8'),
    ) as {
      paths: Record<
        string,
        Record<string, { operationId?: string; responses?: Record<string, unknown> }>
      >
    }
    const markdown = readFileSync(
      new URL('../../api/openapi/markdown/console-openapi.md', import.meta.url),
      'utf8',
    )
    const paths = raw.paths
    const route = (path: string) => {
      const result = paths[path]
      if (!result) throw new Error(`Missing raw OpenAPI path ${path}`)
      return result
    }
    const operation = (path: string, method: string) => {
      const result = route(path)[method]
      if (!result) throw new Error(`Missing raw OpenAPI operation ${method.toUpperCase()} ${path}`)
      return result
    }

    expect(Object.keys(route('/auth/casdoor/login')).sort()).toEqual(['get'])
    expect(Object.keys(route('/auth/casdoor/callback')).sort()).toEqual(['get'])
    expect(Object.keys(route('/auth/casdoor/result')).sort()).toEqual(['get'])
    expect(Object.keys(route('/auth/casdoor/display')).sort()).toEqual(['get', 'head'])
    expect(operation('/auth/casdoor/display', 'head').responses).toHaveProperty('405')
    expect(operation('/auth/casdoor/display', 'get').operationId).toBe('get_casdoor_display_api')
    expect(operation('/auth/casdoor/result', 'get').operationId).toBe(
      'get_casdoor_restricted_result_api',
    )
    expect(operation('/login_config_bootstrap', 'get').operationId).toBe(
      'get_login_config_bootstrap_api',
    )
    expect(markdown).toContain('### [HEAD] /auth/casdoor/display')
    for (const path of ['callback', 'display', 'login', 'result']) {
      expect(markdown).toContain(`### [GET] /auth/casdoor/${path}`)
    }
    expect(markdown).not.toMatch(/### \[OPTIONS\] \/auth\/casdoor\//)
  })
})
