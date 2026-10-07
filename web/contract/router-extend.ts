// Fork-only Console routes without a generated response contract.
// extend: 系统管理 — 代码执行控制（sandbox-full 授权名单）
import {
  codeExecutionControlAddContract,
  codeExecutionControlListContract,
  codeExecutionControlRemoveContract,
} from './console/system-manage'

export const forkConsoleContractExtend = {
  systemManage: {
    codeExecutionControlList: codeExecutionControlListContract,
    codeExecutionControlAdd: codeExecutionControlAddContract,
    codeExecutionControlRemove: codeExecutionControlRemoveContract,
  },
}
