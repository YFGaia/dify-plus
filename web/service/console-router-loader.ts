import type { AnyContractRouter } from '@orpc/contract'
import { contractLoaders } from '@dify/contracts/api/console/orpc.gen'
// extend: fork-only 契约段（login_config 双阶段 / 系统管理代码执行控制）
import { forkConsoleContractExtend } from '@/contract/router-extend'

const generatedConsoleContractLoaders: Partial<Record<string, () => Promise<AnyContractRouter>>> =
  contractLoaders

// extend: fork 契约段注册——生成物 loader 不认识 fork 端点，按顶层 segment 兜底命中
const forkConsoleContractSegments: Partial<Record<string, AnyContractRouter>> = {
  loginConfigBootstrap: { loginConfigBootstrap: forkConsoleContractExtend.loginConfigBootstrap },
  loginConfig: { loginConfig: forkConsoleContractExtend.loginConfig },
  systemManage: { systemManage: forkConsoleContractExtend.systemManage },
}

async function loadGeneratedConsoleContract(segment: string) {
  const loader = generatedConsoleContractLoaders[segment]
  if (!loader) return null

  return loader()
}

async function loadEnterpriseContract(): Promise<AnyContractRouter> {
  const { contract } = await import('@dify/contracts/enterprise/orpc.gen')
  return { enterprise: contract }
}

export async function loadConsoleContractForSegment(segment: string) {
  if (segment === 'enterprise') return loadEnterpriseContract()

  const generatedContract = await loadGeneratedConsoleContract(segment)
  if (generatedContract) return generatedContract

  // extend: fork 自有契约段
  const forkContract = forkConsoleContractSegments[segment]
  if (forkContract) return forkContract

  throw new Error(`Console contract segment "${segment}" is not configured.`)
}
