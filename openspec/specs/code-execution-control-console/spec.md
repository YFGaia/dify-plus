# code-execution-control-console Specification

## Purpose

code 节点执行控制（sandbox-full 授权名单）的 Console 管理能力：配置存储、管理端点、redis 键 `control_mail` 写入、前端管理块、存量数据迁移与读侧接线恢复。替代 admin（GVA）侧的 `sys_user_global_code` 表 + `SyncExecuteCode` 写入链路。

## Requirements

### Requirement: 授权名单持久化存储

系统 SHALL 在 `migrations_extend` Alembic 链中新增 `code_execution_control_extend` 表存储 sandbox-full 授权邮箱名单，表 MUST 含 `id`（主键）、`email`（非空且唯一约束）、`created_by`（操作者 account id）、`created_at` 字段。数据库 MUST 作为名单的 source of truth；redis 键 `control_mail` 仅作为投影缓存。

#### Scenario: 迁移创建表

- **WHEN** 执行 `uv run --project api flask extend_db upgrade`
- **THEN** `code_execution_control_extend` 表被创建，`email` 列存在唯一约束

#### Scenario: 重复邮箱被拒绝

- **WHEN** 管理 API 尝试添加名单中已存在的邮箱
- **THEN** 请求返回 400 且表中不产生重复行

### Requirement: Console 管理端点

系统 SHALL 在 `api/controllers/console/system_manage_extend.py` 域下提供授权名单的查询、添加、删除端点（路由前缀 `/console/api/system-manage-extend/`），端点 MUST 使用与既有端点相同的装饰器栈（`@setup_required` + `@login_required` + `@account_initialization_required` + `@system_admin_required_extend`）。

#### Scenario: 管理员查询名单

- **WHEN** admin 或 owner 角色用户请求名单列表
- **THEN** 返回当前全部授权邮箱及审计字段（created_by、created_at）

#### Scenario: 非管理员被拒绝

- **WHEN** 普通成员角色用户调用任一名单管理端点
- **THEN** 返回 403

### Requirement: redis 投影写入

管理 API 的每次名单变更 SHALL 在数据库事务提交后，从数据库全量读取名单并以 JSON 数组格式覆盖写入 redis 键 `control_mail`（persistent，无 TTL）。系统 MUST 提供幂等的缓存重建方法（service 层），供数据迁移与故障恢复复用。redis 写入失败时 MUST 记录 error 日志且响应中告知同步失败，数据库写入不回滚。

#### Scenario: 添加邮箱后 redis 同步

- **WHEN** 管理员添加邮箱 `a@example.com` 且此前名单为 `["b@example.com"]`
- **THEN** redis 键 `control_mail` 的值变为包含两个邮箱的 JSON 数组

#### Scenario: 清空名单

- **WHEN** 管理员删除名单中最后一个邮箱
- **THEN** redis 键 `control_mail` 被写为 `[]`，后续 code 节点执行全部走普通 sandbox

#### Scenario: 缓存重建

- **WHEN** redis 中 `control_mail` 与数据库不一致时调用缓存重建方法
- **THEN** redis 键被数据库当前名单覆盖，方法可重复调用且结果一致

### Requirement: 读侧接线恢复与功能等价

系统 SHALL 恢复 code 节点执行时的授权判定链路：`DefaultWorkflowCodeExecutor`（`api/core/workflow/node_factory.py`）MUST 持有 tenant_id 并在执行 code 节点时调用 `ExecutionControl.check_code(tenant_id)`，将结果作为 `purview` 传入 `CodeExecutor.execute_workflow_code_template`；`purview=True` 时代码 MUST 提交到 `FULL_CODE_EXECUTION_ENDPOINT`（sandbox-full），否则提交到普通 sandbox。`check_code` 在 redis 键缺失、JSON 解析失败或数据库查询异常时 MUST 返回 False（安全默认），且 MUST 使用精确异常捕获替代裸 `except`。

#### Scenario: 名单命中走 sandbox-full

- **WHEN** 某 tenant 的 owner 邮箱在授权名单中，该 tenant 下运行含 code 节点的 workflow
- **THEN** code 节点的执行请求发往 `FULL_CODE_EXECUTION_ENDPOINT`

#### Scenario: 名单未命中走普通 sandbox

- **WHEN** tenant 的 owner 邮箱不在名单中（或名单为空）
- **THEN** code 节点的执行请求发往 `CODE_EXECUTION_ENDPOINT`，行为与接线恢复前一致

#### Scenario: redis 异常安全回退

- **WHEN** `control_mail` 键不存在或值非法 JSON
- **THEN** `check_code` 返回 False，code 节点正常走普通 sandbox，不抛异常中断 workflow

### Requirement: 读实现收敛为单份

迁移完成后，`ExecutionControl` 的读实现 SHALL 收敛为单一模块（`api/core/workflow/nodes/code/control_extend.py` 与 `api/dify_graph/nodes/code/control_extend.py` 双副本按当时代码节点归属包保留一份，另一份删除或改为 re-export）。

#### Scenario: 无僵尸副本

- **WHEN** 执行 `rg 'control_mail' api/`
- **THEN** 读侧命中只指向单一实现模块，写侧命中只指向 Console 管理 service

### Requirement: 前端管理块

系统 SHALL 在 `web/app/(commonLayout)/system-manage-extend/` 下新增「代码执行控制」页面（layout `menuItems` 增加菜单项），提供名单查看、添加、删除交互。数据层 MUST 遵循 contract-first 规范：在 `web/contract/console/` 定义契约并注册进 console router，call site 使用 TanStack Query（`consoleQuery` + `queryOptions()`/`mutationOptions()`），MUST NOT 使用 legacy `service/base` 直调。文案 MUST 走 i18n `extend` namespace，不得硬编码中文。

#### Scenario: 管理员管理名单

- **WHEN** owner 用户进入系统管理的代码执行控制页并添加一个邮箱
- **THEN** 列表即时刷新显示新邮箱，且后端名单与 redis 同步更新

#### Scenario: 前端规范检查

- **WHEN** 运行 `pnpm lint` 与 `pnpm type-check:tsgo`
- **THEN** 新增代码零错误，且不含对 legacy `service/base` 的新增引用

### Requirement: 存量数据一次性迁移

系统 SHALL 提供一次性迁移，将 GVA `sys_user_global_code.user_id` 经 `sys_users.email` 映射为邮箱写入 `code_execution_control_extend`，并调用缓存重建方法同步 redis。迁移 MUST 在 DROP `sys_user_global_code` 之前执行，且 MUST 校验迁移后行数与源表去重后行数一致。

#### Scenario: 存量名单迁移

- **WHEN** `sys_user_global_code` 含 3 个 user_id（对应 3 个不同邮箱）时执行迁移
- **THEN** 新表含这 3 个邮箱，redis `control_mail` 为含 3 个邮箱的 JSON 数组

#### Scenario: 源表为空

- **WHEN** `sys_user_global_code` 为空或不存在时执行迁移
- **THEN** 迁移正常完成，新表为空，redis 键为 `[]`（或保持不存在），不报错
