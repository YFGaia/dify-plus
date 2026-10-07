# workflow-node-billing Specification

## Purpose

TBD - created by archiving change p0-restore-billing-hooks. Update Purpose after archive.

## Requirements

### Requirement: workflow 节点成功事件 SHALL 派发计费任务

`api/core/app/workflow/layers/persistence.py` 的 `_handle_node_succeeded` SHALL 在节点执行成功时，构造包含 `created_by_role`（按 `UserFrom.ACCOUNT`/`UserFrom.END_USER` 映射为 `CreatorUserRole`）与 `workflow_run_id`（来自 `self._workflow_execution.id_`）的 domain execution dict，并调用 `update_account_money_when_workflow_node_execution_created_extend.delay(...)` 异步派发计费任务，行为与 `origin/main`（约 L290-302）等价。派发 MUST 为异步（`.delay`），扣费计算与失败重试由 Celery 任务自身负责（任务定义 `queue="extend_high"`、`max_retries=3`），任务执行失败 MUST NOT 影响 workflow 主流程结果。

#### Scenario: LLM 节点成功后账号额度被扣减

- **WHEN** 通过 Console 调试或 Service API 运行一个包含 LLM 节点的 workflow 且节点执行成功
- **THEN** `extend_high` 队列收到一条 `update_account_money_when_workflow_node_execution_created_extend` 任务，任务消费后对应账号在 `account_money_extend.used_quota` 上产生增量

#### Scenario: chatflow 的 LLM 节点同样触发扣费

- **WHEN** 运行 advanced-chat（chatflow）应用且其中 LLM 节点执行成功
- **THEN** 同样经 `persistence.py` 挂点派发计费任务并产生 `used_quota` 增量

#### Scenario: 扣费任务失败不影响 workflow 结果

- **WHEN** 计费 Celery 任务在 worker 侧执行失败
- **THEN** 任务按自身 `max_retries=3` 策略重试，workflow 节点执行结果与运行状态不受影响
