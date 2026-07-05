import type { InferContractRouterInputs } from '@orpc/contract'
import { contract as communityContract } from '@dify/contracts/api/console/orpc.gen'
import { contract as enterpriseContract } from '@dify/contracts/enterprise/orpc.gen'
import { rbacAccessConfigContract } from './console/access-control'
import { agentDriveContracts } from './console/agent-drive'
import {
  appDeleteContract,
  appListContract,
  appStarContract,
  appStarredListContract,
  appUnstarContract,
  workflowOnlineUsersContract,
} from './console/apps'
import { bindPartnerStackContract, invoicesContract } from './console/billing'
import {
  exploreAppDetailContract,
  exploreAppsContract,
  exploreBannersContract,
  exploreInstalledAppAccessModeContract,
  exploreInstalledAppAccessModeUpdateContract,
  exploreInstalledAppMetaContract,
  exploreInstalledAppParametersContract,
  exploreInstalledAppPinContract,
  exploreInstalledAppsContract,
  exploreInstalledAppUninstallContract,
  learnDifyAppsContract,
} from './console/explore'
import { fileUploadContract } from './console/files'
import { changePreferredProviderTypeContract, modelProvidersModelsContract } from './console/model-providers'
import { notificationContract, notificationDismissContract } from './console/notification'
import { pluginCheckInstalledContract, pluginLatestVersionsContract } from './console/plugins'
import {
  checkSnippetDependenciesContract,
  confirmSnippetImportContract,
  createCustomizedSnippetContract,
  deleteCustomizedSnippetContract,
  exportCustomizedSnippetContract,
  getCustomizedSnippetContract,
  getSnippetDefaultBlockConfigsContract,
  getSnippetDraftConfigContract,
  getSnippetDraftNodeLastRunContract,
  getSnippetDraftWorkflowContract,
  getSnippetPublishedWorkflowContract,
  getSnippetWorkflowRunDetailContract,
  importCustomizedSnippetContract,
  incrementSnippetUseCountContract,
  listCustomizedSnippetsContract,
  listSnippetWorkflowRunNodeExecutionsContract,
  listSnippetWorkflowRunsContract,
  publishSnippetWorkflowContract,
  runSnippetDraftIterationNodeContract,
  runSnippetDraftLoopNodeContract,
  runSnippetDraftNodeContract,
  runSnippetDraftWorkflowContract,
  stopSnippetWorkflowTaskContract,
  syncSnippetDraftWorkflowContract,
  updateCustomizedSnippetContract,
} from './console/snippets'
// extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的 — 路径改为 login_config，需先请求 login_config_bootstrap 写入 cookie
import { loginConfigBootstrapContract, loginConfigContract } from './console/system'
// extend: 系统管理 — 代码执行控制（sandbox-full 授权名单）
import {
  codeExecutionControlAddContract,
  codeExecutionControlListContract,
  codeExecutionControlRemoveContract,
} from './console/system-manage'
import {
  tagBindingCreateContract,
  tagBindingRemoveContract,
  tagCreateContract,
  tagDeleteContract,
  tagListContract,
  tagUpdateContract,
} from './console/tags'
import {
  triggerOAuthConfigContract,
  triggerOAuthConfigureContract,
  triggerOAuthDeleteContract,
  triggerOAuthInitiateContract,
  triggerProviderInfoContract,
  triggersContract,
  triggerSubscriptionBuildContract,
  triggerSubscriptionBuilderCreateContract,
  triggerSubscriptionBuilderLogsContract,
  triggerSubscriptionBuilderUpdateContract,
  triggerSubscriptionBuilderVerifyUpdateContract,
  triggerSubscriptionDeleteContract,
  triggerSubscriptionsContract,
  triggerSubscriptionUpdateContract,
  triggerSubscriptionVerifyContract,
} from './console/trigger'
import { trialAppDatasetsContract, trialAppInfoContract, trialAppParametersContract, trialAppWorkflowsContract } from './console/try-app'
import {
  workflowDraftEnvironmentVariablesContract,
  workflowDraftUpdateConversationVariablesContract,
  workflowDraftUpdateEnvironmentVariablesContract,
  workflowDraftUpdateFeaturesContract,
} from './console/workflow'
import { workflowCommentContracts } from './console/workflow-comment'
import { workspacesGetContract, workspaceSwitchContract } from './console/workspaces'
import { collectionPluginsContract, collectionsContract, downloadPluginContract, searchAdvancedContract, templateDetailContract } from './marketplace'

export const marketplaceRouterContract = {
  collections: collectionsContract,
  collectionPlugins: collectionPluginsContract,
  searchAdvanced: searchAdvancedContract,
  templateDetail: templateDetailContract,
  downloadPlugin: downloadPluginContract,
}

export type MarketPlaceInputs = InferContractRouterInputs<typeof marketplaceRouterContract>

export const consoleRouterContract = {
  enterprise: enterpriseContract,
  ...communityContract,
  // extend: CVE-2025-63387未授权访问 虽然这个api实际上就是个登录用的 — 路径改为 login_config，需先请求 login_config_bootstrap 写入 cookie
  loginConfigBootstrap: loginConfigBootstrapContract,
  loginConfig: loginConfigContract,
  // extend: 系统管理 — 代码执行控制（sandbox-full 授权名单）
  systemManage: {
    codeExecutionControlList: codeExecutionControlListContract,
    codeExecutionControlAdd: codeExecutionControlAddContract,
    codeExecutionControlRemove: codeExecutionControlRemoveContract,
  },
  apps: {
    ...communityContract.apps,
    list: appListContract,
    deleteApp: appDeleteContract,
    starredList: appStarredListContract,
    star: appStarContract,
    unstar: appUnstarContract,
    workflowOnlineUsers: workflowOnlineUsersContract,
    byAppId: {
      ...communityContract.apps.byAppId,
      agent: {
        ...communityContract.apps.byAppId.agent,
        ...agentDriveContracts.byAppId.agent,
        drive: {
          ...communityContract.apps.byAppId.agent.drive,
          ...agentDriveContracts.byAppId.agent.drive,
        },
      },
    },
  },
  agent: {
    ...communityContract.agent,
    byAgentId: {
      ...communityContract.agent.byAgentId,
      drive: {
        ...communityContract.agent.byAgentId.drive,
        ...agentDriveContracts.byAgentId.drive,
      },
    },
  },
  explore: {
    ...communityContract.explore,
    apps: exploreAppsContract,
    learnDifyApps: learnDifyAppsContract,
    appDetail: exploreAppDetailContract,
    installedApps: exploreInstalledAppsContract,
    uninstallInstalledApp: exploreInstalledAppUninstallContract,
    updateInstalledApp: exploreInstalledAppPinContract,
    appAccessMode: exploreInstalledAppAccessModeContract,
    updateAppAccessMode: exploreInstalledAppAccessModeUpdateContract,
    installedAppParameters: exploreInstalledAppParametersContract,
    installedAppMeta: exploreInstalledAppMetaContract,
    banners: exploreBannersContract,
  },
  trialApps: {
    ...communityContract.trialApps,
    info: trialAppInfoContract,
    datasets: trialAppDatasetsContract,
    parameters: trialAppParametersContract,
    workflows: trialAppWorkflowsContract,
  },
  files: {
    ...communityContract.files,
    upload: {
      ...communityContract.files.upload,
      post: fileUploadContract,
    },
  },
  modelProviders: {
    models: modelProvidersModelsContract,
    changePreferredProviderType: changePreferredProviderTypeContract,
  },
  plugins: {
    checkInstalled: pluginCheckInstalledContract,
    latestVersions: pluginLatestVersionsContract,
  },
  rbacAccessConfig: rbacAccessConfigContract,
  snippets: {
    list: listCustomizedSnippetsContract,
    create: createCustomizedSnippetContract,
    detail: getCustomizedSnippetContract,
    update: updateCustomizedSnippetContract,
    delete: deleteCustomizedSnippetContract,
    export: exportCustomizedSnippetContract,
    import: importCustomizedSnippetContract,
    confirmImport: confirmSnippetImportContract,
    checkDependencies: checkSnippetDependenciesContract,
    incrementUseCount: incrementSnippetUseCountContract,
    draftWorkflow: getSnippetDraftWorkflowContract,
    syncDraftWorkflow: syncSnippetDraftWorkflowContract,
    draftConfig: getSnippetDraftConfigContract,
    publishedWorkflow: getSnippetPublishedWorkflowContract,
    publishWorkflow: publishSnippetWorkflowContract,
    defaultBlockConfigs: getSnippetDefaultBlockConfigsContract,
    workflowRuns: listSnippetWorkflowRunsContract,
    workflowRunDetail: getSnippetWorkflowRunDetailContract,
    workflowRunNodeExecutions: listSnippetWorkflowRunNodeExecutionsContract,
    runDraftNode: runSnippetDraftNodeContract,
    lastDraftNodeRun: getSnippetDraftNodeLastRunContract,
    runDraftIterationNode: runSnippetDraftIterationNodeContract,
    runDraftLoopNode: runSnippetDraftLoopNodeContract,
    runDraftWorkflow: runSnippetDraftWorkflowContract,
    stopWorkflowTask: stopSnippetWorkflowTaskContract,
  },
  billing: {
    ...communityContract.billing,
    invoices: invoicesContract,
    bindPartnerStack: bindPartnerStackContract,
  },
  workflowDraft: {
    environmentVariables: workflowDraftEnvironmentVariablesContract,
    updateEnvironmentVariables: workflowDraftUpdateEnvironmentVariablesContract,
    updateConversationVariables: workflowDraftUpdateConversationVariablesContract,
    updateFeatures: workflowDraftUpdateFeaturesContract,
  },
  workflowComments: workflowCommentContracts,
  notification: notificationContract,
  notificationDismiss: notificationDismissContract,
  tags: {
    ...communityContract.tags,
    list: tagListContract,
    create: tagCreateContract,
    update: tagUpdateContract,
    delete: tagDeleteContract,
    bind: tagBindingCreateContract,
    unbind: tagBindingRemoveContract,
  },
  triggers: {
    list: triggersContract,
    providerInfo: triggerProviderInfoContract,
    subscriptions: triggerSubscriptionsContract,
    subscriptionBuilderCreate: triggerSubscriptionBuilderCreateContract,
    subscriptionBuilderUpdate: triggerSubscriptionBuilderUpdateContract,
    subscriptionBuilderVerifyUpdate: triggerSubscriptionBuilderVerifyUpdateContract,
    subscriptionVerify: triggerSubscriptionVerifyContract,
    subscriptionBuild: triggerSubscriptionBuildContract,
    subscriptionDelete: triggerSubscriptionDeleteContract,
    subscriptionUpdate: triggerSubscriptionUpdateContract,
    subscriptionBuilderLogs: triggerSubscriptionBuilderLogsContract,
    oauthConfig: triggerOAuthConfigContract,
    oauthConfigure: triggerOAuthConfigureContract,
    oauthDelete: triggerOAuthDeleteContract,
    oauthInitiate: triggerOAuthInitiateContract,
  },
  workspaces: {
    ...communityContract.workspaces,
    get: workspacesGetContract,
    switch: {
      post: workspaceSwitchContract,
    },
  },
}
