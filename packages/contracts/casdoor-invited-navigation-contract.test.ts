import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vite-plus/test'
import { contractLoaders } from './generated/api/console/orpc.gen'

const raw = JSON.parse(
  readFileSync(new URL('./openapi/console-openapi.json', import.meta.url), 'utf8'),
)

// Browser redirects stay in the native OpenAPI/Markdown navigation contract.
// They are not synthesized as a JSON RPC merely to expose query DTO types.
describe('native invited Casdoor browser navigation contract', () => {
  it('registers the optional bounded invitation query alongside the original navigation fields', () => {
    const operation = raw.paths['/auth/casdoor/login'].get
    expect(Object.keys(raw.paths['/auth/casdoor/login'])).toEqual(['get'])
    expect(operation.security).toEqual([])
    expect(
      operation.parameters.map((parameter: { name: string }) => parameter.name).sort(),
    ).toEqual(['init', 'invite_token', 'locale', 'return_path', 'timezone'])
    expect(
      operation.parameters.find((parameter: { name: string }) => parameter.name === 'invite_token'),
    ).toEqual({
      in: 'query',
      name: 'invite_token',
      required: false,
      schema: { type: 'string', minLength: 1, maxLength: 512 },
    })
    expect(operation.responses).toHaveProperty('302')
    expect(operation.responses).toHaveProperty('303')
    expect(operation.requestBody).toBeUndefined()
  })

  it('keeps invitation out of callback query and retains the closed runtime model shape', () => {
    const callback = raw.paths['/auth/casdoor/callback'].get
    expect(callback.parameters.map((parameter: { name: string }) => parameter.name).sort()).toEqual(
      ['code', 'error', 'error_description', 'state'],
    )
    const model = raw.components.schemas.CasdoorLoginQuery
    expect(model.additionalProperties).toBe(false)
    expect(Object.keys(model.properties).sort()).toEqual([
      'init',
      'invite_token',
      'locale',
      'return_path',
      'timezone',
    ])
    expect(model.properties.invite_token.anyOf).toContainEqual({
      type: 'string',
      minLength: 1,
      maxLength: 512,
    })
  })

  it('preserves original native SDK routing without adding a navigation RPC', async () => {
    const { auth } = await contractLoaders.auth()
    expect('login' in auth.casdoor).toBe(false)
    expect('callback' in auth.casdoor).toBe(false)
    expect(auth.casdoor.display.get['~orpc'].route.path).toBe('/auth/casdoor/display')
    expect(auth.casdoor.result.get['~orpc'].route.path).toBe('/auth/casdoor/result')
  })

  it('documents the invitation query on the actual native browser login section', () => {
    const markdown = readFileSync(
      new URL('../../api/openapi/markdown/console-openapi.md', import.meta.url),
      'utf8',
    )
    const start = markdown.indexOf('### [GET] /auth/casdoor/login')
    expect(start).toBeGreaterThan(-1)
    const end = markdown.indexOf('\n### ', start + 1)
    const section = markdown.slice(start, end === -1 ? undefined : end)
    expect(section).toContain('invite_token')
    expect(section).toContain('return_path')
    expect(section).not.toContain('id_token')
  })
})
