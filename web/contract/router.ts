import type { InferContractRouterInputs } from '@orpc/contract'
import { bindPartnerStackContract, invoicesContract } from './console/billing'
// extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的 — 路径改为 login_config，需先请求 login_config_bootstrap 写入 cookie
import { loginConfigBootstrapContract, loginConfigContract } from './console/system'
import { trialAppDatasetsContract, trialAppInfoContract, trialAppParametersContract, trialAppWorkflowsContract } from './console/try-app'
import { collectionPluginsContract, collectionsContract, searchAdvancedContract } from './marketplace'

export const marketplaceRouterContract = {
  collections: collectionsContract,
  collectionPlugins: collectionPluginsContract,
  searchAdvanced: searchAdvancedContract,
}

export type MarketPlaceInputs = InferContractRouterInputs<typeof marketplaceRouterContract>

// extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的 — 路径改为 login_config，需先请求 login_config_bootstrap 写入 cookie
export const consoleRouterContract = {
  loginConfigBootstrap: loginConfigBootstrapContract,
  loginConfig: loginConfigContract,
  trialApps: {
    info: trialAppInfoContract,
    datasets: trialAppDatasetsContract,
    parameters: trialAppParametersContract,
    workflows: trialAppWorkflowsContract,
  },
  billing: {
    invoices: invoicesContract,
    bindPartnerStack: bindPartnerStackContract,
  },
}

export type ConsoleInputs = InferContractRouterInputs<typeof consoleRouterContract>
