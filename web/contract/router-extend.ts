// extend: fork-only console 契约注册。
// 上游 1.16.0 删除手写契约层（web/contract/router.ts）后，生成物 router.gen.ts 成为唯一契约源。
// fork 自有端点在此聚合为一个契约片段：
//   - 运行时：web/service/console-router-loader.ts 按顶层 segment 懒加载时优先命中本片段；
//   - 类型：web/service/client.ts 用 ConsoleRouterContractWithExtend 扩展 consoleClient 的类型。
import type { consoleRouterContract } from '@dify/contracts/api/console/router.gen'
// extend: CVE-2025-63387未授权访问 — login_config 双阶段（bootstrap 写 cookie + JWT）
import { loginConfigBootstrapContract, loginConfigContract } from './console/system'
// extend: 系统管理 — 代码执行控制（sandbox-full 授权名单）
import {
  codeExecutionControlAddContract,
  codeExecutionControlListContract,
  codeExecutionControlRemoveContract,
} from './console/system-manage'

export const forkConsoleContractExtend = {
  loginConfigBootstrap: loginConfigBootstrapContract,
  loginConfig: loginConfigContract,
  systemManage: {
    codeExecutionControlList: codeExecutionControlListContract,
    codeExecutionControlAdd: codeExecutionControlAddContract,
    codeExecutionControlRemove: codeExecutionControlRemoveContract,
  },
}

export type ConsoleRouterContractWithExtend = typeof consoleRouterContract &
  typeof forkConsoleContractExtend
