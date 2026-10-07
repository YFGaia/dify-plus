# batch-workflow-console-service

【条件性：仅当决策门结论为方案 B 时生效】批量工作流迁移到 Flask + Celery 后的能力要求。

## ADDED Requirements

### Requirement: Console API 契约与语义兼容

api 侧 SHALL 提供与原 `/admin/gaia/workflow/batch/*` 一一对应的 8 个 Console API 端点：批量上传（processing）、批次列表（list）、进度查询（progress）、停止（stop）、恢复（resume）、全量重试（retry）、失败重试（retry-failed）、结果下载（download）。进度查询与列表响应的字段语义（status 枚举、`total_rows`/`processed_rows`/`error_count`、分页字段）MUST 与原实现兼容。

#### Scenario: 上传 Excel 创建批次

- **WHEN** 已登录用户对其可访问的 installed app 上传合法 Excel（含 `installed_id`、`app_id`、可选 `key_name_mapping`）
- **THEN** 系统解析 Excel（openpyxl）、创建 `batch_workflows_extend` 批次与逐行 `batch_workflow_tasks_extend` 任务，返回批次 id

#### Scenario: 进度轮询兼容

- **WHEN** 前端以原有轮询逻辑（`batch-progress-manager.ts` 语义）请求进度端点
- **THEN** 响应包含与原 Go 实现语义一致的状态与行数统计，前端进度组件无需改字段映射即可渲染

#### Scenario: 非法文件被拒绝

- **WHEN** 上传的文件不是合法 Excel 或行数超出配置上限
- **THEN** 返回 4xx 错误与可读错误消息，不创建批次记录

### Requirement: 鉴权与批次归属校验

8 个端点 SHALL 使用标准 Console 登录态鉴权（session/JWT，经现有 console 鉴权装饰器），MUST NOT 接受 csrf_token 作为凭证。除创建外的所有批次操作（进度/停止/恢复/重试/下载）MUST 校验批次归属：仅批次创建者本人可操作。

#### Scenario: 未登录访问被拒

- **WHEN** 请求未携带有效 console 登录态
- **THEN** 返回 401，且不泄露批次存在性

#### Scenario: 越权访问他人批次

- **WHEN** 登录用户请求非本人创建的批次的进度或下载
- **THEN** 返回 403 或 404，不返回批次数据

### Requirement: Celery 执行链路替代 Go worker pool

批次任务执行 SHALL 由 Celery 任务承接（复用 `extend_low` 或新建专用低优先队列），进程内调用 workflow 执行服务（`AppGenerateService`）完成单行任务，MUST NOT 通过 HTTP + 伪造 JWT 调用 Console API。执行 MUST 以批次创建者 account 作为计费归因主体。

#### Scenario: 批次正常执行完成

- **WHEN** 批次创建后 Celery worker 消费任务
- **THEN** 逐行调用 workflow/completion 应用，任务与批次状态原子更新，全部完成后批次状态为 completed，结果可下载

#### Scenario: 单任务失败与重试

- **WHEN** 某行任务执行失败（如模型错误）
- **THEN** 按重试策略（`max_retries` + 幂等键）重试，最终失败则任务标记 failed、批次 `error_count` 递增，不阻塞其他任务

#### Scenario: 停止与恢复

- **WHEN** 用户对运行中批次调用 stop（随后 resume）
- **THEN** 未开始任务不再被执行（恢复后继续执行剩余任务），已完成任务结果保留

#### Scenario: 计费归因一致

- **WHEN** 批量执行 N 行与用户手动单次执行 N 次相同输入
- **THEN** `account_money_extend` 的扣减金额与归因账号一致（均归因批次创建者）

#### Scenario: 队列隔离

- **WHEN** 大批量任务（如上千行）在执行
- **THEN** 任务仅占用低优先/专用队列，`extend_high` 上的计费任务时效不受影响

### Requirement: 数据表主权移交与 user_id 迁移

`batch_workflows_extend` 与 `batch_workflow_tasks_extend` 的 schema 定义 SHALL 移交 `api/migrations_extend`（独立 Alembic 链）管理：迁移脚本 MUST 幂等兼容 GORM 已建表的存量库与全新库；批次归属 MUST 从 `sys_users` uint 主键迁移为 accounts uuid（新增 `account_id` 列 + 存量回填 + 代码切换读写新列）。

#### Scenario: 存量库升级

- **WHEN** 在已由 GORM 建表的库上执行 `flask extend_db upgrade`
- **THEN** 迁移成功（存在性检查跳过建表），`account_id` 列创建并按映射规则回填，无法映射的行置 NULL 并输出统计日志

#### Scenario: 全新库建表

- **WHEN** 在无这两张表的全新库上执行 `flask extend_db upgrade`
- **THEN** 两张表按新 schema（含 `account_id`）创建成功

#### Scenario: 回滚安全

- **WHEN** 执行该迁移的 downgrade
- **THEN** `account_id` 列被移除，原 `user_id` 列及数据不受影响

### Requirement: 统计脚本脱离 sys_users

`api/services/batch_workflow_statistics_service.py` 的报表 SQL SHALL 改为经 `account_id` 关联 `accounts` 表取用户信息，MUST NOT 再引用 GVA 的 `sys_users` 表。

#### Scenario: 报表输出等价

- **WHEN** 改造后运行统计脚本
- **THEN** 输出的用户维度统计基于 accounts 数据，脚本内无 `sys_users` 引用

### Requirement: 前端切换到 contract-first Console API

前端 8 个批量调用 SHALL 迁移为 contract-first 模式（`web/contract/console/` 定义 + `consoleQuery`/TanStack Query 调用），删除 `web/service/web-extend.ts` 的 fetch 直调与 csrf token 取用逻辑。该切换 SHALL 在 P3（1.15.0 合并）完成后实施。

#### Scenario: 功能全链路回归

- **WHEN** 切换完成后用户执行 上传→轮询进度→停止→恢复→失败重试→下载 全链路
- **THEN** 各操作经 Console API 成功完成，行为与迁移前一致

#### Scenario: 旧数据层清除

- **WHEN** 切换完成后检索 web/ 代码
- **THEN** 无 `/admin/gaia` 调用、无 `web-extend.ts` 文件、无 csrf_token 冒充 Bearer 的逻辑，`pnpm lint`/`type-check:tsgo`/`build` 通过
