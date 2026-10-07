# billing-regression-baseline Specification

## Purpose

TBD - created by archiving change p0-restore-billing-hooks. Update Purpose after archive.

## Requirements

### Requirement: 计费回归清单 SHALL 覆盖 6 条链路且可重复执行

仓库 SHALL 包含一份计费回归基线清单文档（置于 `docs/dify-plus/` 下），覆盖以下 6 条链路，每条链路 MUST 给出前置条件、操作步骤与可断言的预期结果（SQL 断言或接口响应断言），使清单可由任何工程师重复执行：

1. Console 调试运行扣费（消息侧 Blinker 链路：`message_was_created` → `update_account_money_when_messaeg_created_extend`）；
2. Explore 运行扣费；
3. WebApp 登录 + 扣费（含 `end_user_account_joins_extend` 归因）；
4. Service API 密钥日/月限额拦截（`api_token_money_extend` 累计与 `validate_app_token` 拦截）；
5. workflow LLM 节点扣费（Celery `extend_high` 链路）；
6. 月初额度重置（3 个 beat 任务；允许以手动触发任务 + 数据断言替代自然触发，两种执行方式均需写明）。

#### Scenario: 清单文档存在且结构完整

- **WHEN** 检查 `docs/dify-plus/` 下的回归清单文档
- **THEN** 6 条链路各有独立小节，均包含前置条件、步骤、预期断言

#### Scenario: 本阶段回归全绿

- **WHEN** 在挂点恢复完成后的代码上逐条执行清单
- **THEN** 6 条链路全部通过，结果（通过/日期/执行人）记录在清单或其执行记录中

### Requirement: 基线 SHALL 以 tag 留档

全部验收通过后，SHALL 在最终 commit 上创建 git tag `fork-pre-merge-1.15.0`，作为合并 upstream 1.15.0 之前的可信基线锚点。

#### Scenario: tag 存在且指向验收通过的 commit

- **WHEN** 运行 `git tag -l fork-pre-merge-1.15.0` 与 `git rev-parse fork-pre-merge-1.15.0`
- **THEN** tag 存在，且其指向的 commit 包含冲突修复、4 处挂点恢复与幽灵文件清理的全部内容
