import type { AddressInfo } from 'node:net'
import * as http from 'node:http'
import { describe, expect, it } from 'vitest'
import { mintFreshToken } from './cli.js'

type StubServer = {
  readonly url: string
  readonly stop: () => Promise<void>
}

function sendJson(res: http.ServerResponse, body: object): void {
  const payload = JSON.stringify(body)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(payload)
}

function startDeviceFlowStub(onApprove: (req: http.IncomingMessage) => void): Promise<StubServer> {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      switch (req.url) {
        case '/console/api/login':
          res.setHeader('Set-Cookie', [
            'access_token=access; Path=/',
            'csrf_token=csrf-value; Path=/',
          ])
          sendJson(res, { result: 'success' })
          return
        case '/openapi/v1/oauth/device/code':
          sendJson(res, { device_code: 'device-1', user_code: 'USER-1' })
          return
        case '/openapi/v1/oauth/device/approve':
          onApprove(req)
          sendJson(res, { status: 'approved' })
          return
        case '/openapi/v1/oauth/device/token':
          sendJson(res, { token: 'dfoa_test' })
          return
        default:
          res.writeHead(404).end()
      }
    })
    server.listen(0, '127.0.0.1', () => {
      const address = server.address() as AddressInfo
      resolve({
        url: `http://127.0.0.1:${address.port}`,
        stop: () => new Promise<void>((done) => server.close(() => done())),
      })
    })
    server.on('error', reject)
  })
}

describe('mintFreshToken', () => {
  it('uses the canonical CSRF header when approving a device code', async () => {
    let csrfHeader: string | undefined
    const stub = await startDeviceFlowStub((req) => {
      const header = req.headers['x-csrf-token']
      csrfHeader = Array.isArray(header) ? header[0] : header
    })

    try {
      const token = await mintFreshToken(stub.url, 'admin@example.com', 'password')

      expect(token).toBe('dfoa_test')
      expect(csrfHeader).toBe('csrf-value')
    } finally {
      await stub.stop()
    }
  })
})
