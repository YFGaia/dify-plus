# batch-workflow-removal

【条件性：仅当决策门结论为方案 A 时生效】批量工作流功能删除的验收要求。

## ADDED Requirements

### Requirement: 前端批量运行能力移除

Dify 前端 SHALL 不再提供批量运行能力：`web/app/components/share/text-generation/index.tsx` 中批量 tab 及批量任务列表/进度/操作相关代码、`web/app/components/share/text-generation/run-batch/batch-progress/`、`web/service/web-extend.ts`、`web/utils/batch-progress-manager.ts` MUST 被删除。

#### Scenario: 无 GVA 调用残留

- **WHEN** 删除完成后在 web/ 下全文检索 `/admin/gaia`
- **THEN** 无任何运行时代码命中（文档与注释除外）

#### Scenario: 运行页交互降级

- **WHEN** 用户打开 text-generation 类 WebApp 运行页
- **THEN** 仅保留上游原生交互（create 单次运行 + 上游原生 batch 客户端并发运行），无 fork 的后台批量任务列表/进度组件/Excel 后台上传入口，单次运行与上游原生 batch 运行功能不受影响（fork 批量 tab 指后台批量任务界面，上游原生 batch tab 按 tasks 2.2 要求保留）

#### Scenario: 构建与静态检查通过

- **WHEN** 删除完成后执行 `pnpm lint`、`pnpm type-check:tsgo` 与 `pnpm build`
- **THEN** 全部通过，无对已删除模块的悬空 import；批量相关 i18n key（`extend.json` 的 `batchWorkflow.*` 等）一并清理

### Requirement: 数据表冷备归档说明

`batch_workflows_extend` 与 `batch_workflow_tasks_extend` 两张表 SHALL 保留不删除，并在 `docs/dify-plus/` 下留存归档说明，内容 MUST 包含：表名与结构来源（GORM AutoMigrate）、历史用途、保留原因、若未来恢复功能的路径指引（指向本 change 的方案 B 设计）。

#### Scenario: 归档说明可检索

- **WHEN** 后续维护者在 `docs/dify-plus/` 检索批量工作流
- **THEN** 能找到归档说明并理解两张表为何存在、能否删除、如何恢复功能

#### Scenario: 数据不受删除影响

- **WHEN** 方案 A 全部任务完成
- **THEN** 两张表及其数据完整保留，无 DROP/TRUNCATE 发生

### Requirement: Go 侧实现移交 P5 清理

方案 A 下 Go 侧批量工作流实现（`admin/server/service/gaia/batch_workflow.go`、`worker_pool.go`、`admin/server/api/v1/gaia/workflow.go`）SHALL 不在本 change 中删除，MUST 在 P5（`p5-admin-decommission`）任务清单中登记为待删资产。

#### Scenario: 移交登记完成

- **WHEN** 方案 A 前端删除完成
- **THEN** P5 change 的任务/资产清单中含上述三个 Go 文件（或其所属模块）的删除项，且 `api/services/batch_workflow_statistics_service.py`（依赖 `sys_users` 的报表脚本）同步登记为「随批量功能一并删除」
