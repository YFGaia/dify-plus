# billing-hook-relocation Specification

## Purpose

TBD - created by archiving change p2-merge-upstream-1-14-2. Update Purpose after archive.

## Requirements

### Requirement: workflow 节点计费派发保持有效

`update_account_money_when_workflow_node_execution_created_extend.delay(...)` 的 Celery 计费派发 SHALL 在 1.14.2 的 graph 层新结构中重挂（1.13.3 坐标：`api/core/app/workflow/layers/persistence.py` 的 `_handle_node_succeeded`；上游 `ef2b5d6107` 已将 provider 配额扣减移入同区域的 `llm_quota.py`），且 SHALL NOT 与上游 `llm_quota` layer 互相调用。

#### Scenario: workflow 运行触发扣费

- **WHEN** 手动触发一次包含 LLM 节点的 workflow 运行并成功结束
- **THEN** `account_money_extend.used_quota` 产生与该节点 token 用量和单价一致的增量

#### Scenario: 与上游 llm_quota 层隔离

- **WHEN** 审查合并后的 `api/core/app/workflow/layers/` 目录
- **THEN** fork 计费派发与上游 `llm_quota.py`（provider credits 扣减）互不调用、各自独立注册

### Requirement: app_token_id 归因写入保持有效

`extras["app_token_id"] = api_token.id` 写入 SHALL 在 1.14.2 重写后的 `api/core/app/apps/workflow/app_generator.py` 与 `advanced_chat/app_generator.py` 中重挂。

#### Scenario: Service API 调用归因

- **WHEN** 使用 API 密钥经 service_api 调用 workflow 或 chatflow 应用
- **THEN** 生成实体的 extras 中携带 `app_token_id`，密钥额度累计正确增加

### Requirement: 密钥关联记录写入保持有效

`ApiTokenMessageJoinsExtend` 关联记录写入 SHALL 在 `advanced_chat/generate_task_pipeline.py`、`workflow/generate_task_pipeline.py` 以及 chat/agent_chat/completion 三个 `app_generator.py` 的 1.14.2 新结构中重挂。

#### Scenario: 五类应用的关联记录

- **WHEN** 分别以 API 密钥调用 advanced_chat、workflow、chat、agent_chat、completion 五类应用各一次
- **THEN** `api_token_message_joins_extend` 均产生对应关联记录

### Requirement: Service API 额度校验保持有效

`api/controllers/service_api/wraps.py` 的 `validate_app_token` 额度前置校验与 `EndUserAccountJoinsExtend`（end_user↔account）映射 SHALL 在上游 user/tenant 注入重构后保持有效，其波及的约 9 个 service_api controller 签名 SHALL 与新版 wraps 对齐。

#### Scenario: 日限额拦截

- **WHEN** API 密钥当日累计用量达到 `day_limit_quota` 后再次调用
- **THEN** 请求被拒绝并返回额度超限错误

#### Scenario: 月限额拦截

- **WHEN** API 密钥当月累计用量达到月限额后再次调用
- **THEN** 请求被拒绝并返回额度超限错误

#### Scenario: end_user 到 account 归因

- **WHEN** WebApp 终端用户经 service_api 产生消息
- **THEN** 用量按 `EndUserAccountJoinsExtend` 映射归因到对应 account 的额度

### Requirement: API 密钥额度字段联查保持完整

`api/controllers/console/apikey.py` 的额度字段联查（`description`、`accumulated_quota`、`day_limit_quota` 等 fork 字段）SHALL 在上游 `select()` 重构基础上重写并保持返回完整。

#### Scenario: 密钥列表包含额度字段

- **WHEN** Console 请求应用 API 密钥列表
- **THEN** 响应包含全部 fork 额度字段且值与数据库一致

### Requirement: 消息扣费事件处理保持有效

`message_was_created` 信号的 fork 扣费 handler SHALL 在 1.14.2 中保持注册且触发语义未变（fork 自带 Events 框架与上游事件机制的对接不回归）。

#### Scenario: 消息创建触发扣费

- **WHEN** Console 调试或 WebApp 对话产生一条计费消息
- **THEN** 对应 account 的 `used_quota` 增加，且仅增加一次

### Requirement: 额度重置定时任务保持注册

`api/extensions/ext_celery.py` 中的 3 个 extend beat 任务（月度用户额度重置、日/月密钥额度重置，队列 `extend_low`/`extend_high`）SHALL 在 celery 升级后仍被注册。

#### Scenario: beat 任务清单断言

- **WHEN** 查询合并后 celery beat schedule / 注册任务列表
- **THEN** 3 个 extend 定时任务全部存在且队列配置不变

### Requirement: 消息上下文处理保持有效

`messages_context_handling` SHALL 在 `api/core/memory/token_buffer_memory.py`（或其 1.14.2 迁移后位置）继续生效。

#### Scenario: 上下文处理行为不变

- **WHEN** 进行多轮对话触发历史消息上下文组装
- **THEN** fork 的上下文处理逻辑生效，行为与合并前一致

### Requirement: fork OAuth 扩展保持有效

`api/libs/oauth.py` 的 `OaOAuth`/钉钉 OAuth 类与 `controllers/console/auth/oauth.py` 的对应接入 SHALL 在上游 oauth 体系演进后保持完整。

#### Scenario: 三方登录可用

- **WHEN** 分别使用钉钉、OAuth2、Casdoor 发起登录
- **THEN** 三种方式均能完成认证并进入系统

### Requirement: 挂点新坐标登记

全部 10 项挂点在 1.14.2 中的最终坐标（文件路径 + 函数/位置）SHALL 更新到 `docs/dify-plus/与上游差异总表.md`，作为 Step B（1.15.0 合并）的输入清单。

#### Scenario: 登记完整性

- **WHEN** 合并定稿后审查 `docs/dify-plus/与上游差异总表.md`
- **THEN** 10 项挂点均有 1.14.2 新坐标记录，被移动过的挂点标注了迁移说明

### Requirement: 计费挂点在 1.15.0 结构上全部重新定位

沿用 P2 更新过的计费挂点清单，全部挂点 SHALL 在 1.15.0 代码结构上重新核对并落位（含 `persistence.py` 节点计费派发、两个 `app_generator.py` 的 `app_token_id` 写入、`generate_task_pipeline.py` 关联记录、`service_api/wraps.py` 校验、`apikey.py` 联查、`ext_celery.py` beat 注册等）；新坐标 MUST 登记回 `docs/dify-plus/与上游差异总表.md`，代码块 MUST 保留「二开部分 Begin/End」标记。

#### Scenario: 挂点清单逐项核对完成

- **WHEN** 合并修复完成后对照挂点清单逐项检查
- **THEN** 每个挂点在 1.15.0 代码中有明确落位（原位保留或已搬迁），差异总表坐标为最新

### Requirement: 6 条计费回归链路全绿

合并完成后 SHALL 通过 P0 建立的 6 条计费回归链路：Console 调试运行扣费、Explore 运行扣费、WebApp 登录+扣费、Service API 密钥日/月限额拦截、workflow LLM 节点扣费、月初额度重置（beat 任务注册在位）。

#### Scenario: workflow 节点扣费

- **WHEN** 手动触发含 LLM 节点的 workflow 运行成功
- **THEN** `account_money_extend.used_quota` 出现对应增量

#### Scenario: 密钥限额拦截

- **WHEN** Service API 密钥的日/月已用额度达到上限后再次调用
- **THEN** 请求被限额校验拦截并返回额度受限错误

#### Scenario: 重置任务在位

- **WHEN** 查看 celery beat 调度列表
- **THEN** 3 个 extend 额度重置任务（月度用户额度、日/月密钥额度）均已注册
