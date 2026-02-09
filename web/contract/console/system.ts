import type { SystemFeatures } from '@/types/feature'
import { type } from '@orpc/contract'
import { base } from '../base'

// extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的
export const loginConfigBootstrapContract = base
  .route({
    path: '/login_config_bootstrap',
    method: 'GET',
  })
  .input(type<unknown>())
  .output(type<{ ok: boolean, token: string }>())

// extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的 — 路径改为 login_config，需先请求 login_config_bootstrap 写入 cookie
export const loginConfigContract = base
  .route({
    path: '/login_config',
    method: 'GET',
  })
  .input(type<unknown>())
  .output(type<SystemFeatures>())
