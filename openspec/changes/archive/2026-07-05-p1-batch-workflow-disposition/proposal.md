# Proposal: p1-batch-workflow-disposition

## Why

批量工作流（batch workflow）是 Dify 前端对 Go 管理后台（admin/GVA）的**唯一运行时依赖**，也是废弃 admin（P5）的最大阻塞项。现状存在三重问题：

1. **架构耦合**：`web/service/web-extend.ts` 有 8 个 fetch 直调 `/admin/gaia/workflow/batch/*`，用 `csrf_token` cookie 冒充 Bearer token；Go 侧 worker 又用与 api 共享的 `SECRET_KEY` 伪造 Dify 兼容 JWT 回调 Console API——双向鉴权 hack，安全性差且阻碍 `SECRET_KEY` 轮换。
2. **实际可达性存疑**：生产 nginx 模板（`docker/nginx/conf.d/default.conf.template`）**没有 `/admin` location**，标准 compose 部署下该功能实际不可达；仅 dev 模式（`web/next.config.ts` rewrite 到 :8888）或自定义网关环境可用——疑似低使用率，可能可以直接删除而非迁移。
3. **维护成本**：Go 侧实现约 2,634 行（`batch_workflow.go` 691 + `worker_pool.go` 1,274 + `api/workflow.go` 669），任务表由 GORM AutoMigrate 建表（游离于 fork 的 Alembic 迁移链之外），`user_id` 还耦合 GVA 的 `sys_users` uint 主键。

本 change 是**带决策门（Gate）的处置方案**：先用 SQL 验证近 90 天使用量并向业务确认，再在「方案 A：删除功能」与「方案 B：迁移到 Flask + Celery」之间二选一执行。对应总线路图的决策点 D2 与 Phase 4 任务 4.1。

## 决策结论（决策门已通过）

- **结论：方案 A（删除功能）**
- **确认人**：仓库维护者（业务方）；**日期**：2026-07-05
- **证据摘要**：
  1. 使用量 SQL（任务 1.1）无法在实施环境执行——本地无任何 dify 生产库/实例可达（仅有无关项目的 Postgres 容器），按 spec「无法访问生产库」场景升级给业务方，由维护者直接确认处置结论。
  2. 静态证据复核成立：生产 nginx 模板 `docker/nginx/conf.d/default.conf.template` 无 `/admin` location，标准 compose 部署下前端 `/admin/gaia/*` 请求 404，功能不可达；仅 dev rewrite（`web/next.config.ts` L40-41 → `:8888`）或自定义网关环境可用。
  3. 业务方确认：近 90 天无人使用/生产不可达，无需再补 SQL，按方案 A 执行。
- **附带结论**：任务 1.2（使用明细）与 1.5（`sys_users → accounts` 映射键）因结论为 A 不适用；数据表按方案 A 保留冷备，不 DROP/TRUNCATE。

## What Changes

- **决策门（必做，最先）**：对 `batch_workflows_extend` 执行使用量 SQL（近 90 天计数 + 按月分布），结合业务访谈得出书面结论，回填总线路图 D2。
- **方案 A（无人用，推荐默认）**：
  - **BREAKING** 删除前端批量运行能力：移除 `text-generation/index.tsx` 的批量 tab 相关代码、`run-batch/batch-progress/`、`web/service/web-extend.ts`、`web/utils/batch-progress-manager.ts`。
  - Go 侧 ~2,634 行实现不单独删除，随 P5 admin 废弃一并清理；数据表保留做冷备并写归档说明。
- **方案 B（在用）**：
  - **BREAKING** 后端重写：Excel 解析改 openpyxl/pandas；worker pool 改 Celery 队列；执行改为内部调用 workflow 服务（`AppGenerateService`），彻底消除伪造 JWT。
  - 数据主权移交：`batch_workflows_extend` / `batch_workflow_tasks_extend` 建表移入 `api/migrations_extend`；`user_id` 从 `sys_users` uint 迁为 account uuid（含存量数据迁移）。
  - 前端 8 个调用改为 Console API（contract-first），进度轮询接口语义保持兼容以最小化前端改动。
  - `api/services/batch_workflow_statistics_service.py`（854 行）脱离 `sys_users` join。
- **共同收尾**（两方案一致，可延至 P5）：`web/next.config.ts` 删除 `/admin` rewrite；`web/docker/entrypoint.sh` 删除死变量 `NEXT_PUBLIC_ADMIN_API_URL`。

## Capabilities

### New Capabilities

- `batch-workflow-disposition-gate`: 使用量验证决策门与两方案共同收尾要求——SQL 验证、业务确认、决策记录回填，以及处置完成后前端构建配置中 admin 残留的清理。
- `batch-workflow-removal`: 【条件性：方案 A】批量工作流功能删除的验收要求——前端无 GVA 调用残留、构建与测试通过、数据表冷备归档说明。
- `batch-workflow-console-service`: 【条件性：方案 B】批量工作流迁移到 Flask + Celery 后的能力要求——Console API 契约、Celery 任务执行、数据迁移、鉴权与权限、进度轮询兼容。

### Modified Capabilities

（无——`openspec/specs/` 当前为空，本 change 不修改既有 capability。）

## Architecture Impact

- **复用现有 fork 模式**（方案 B）：Console controller 挂 `api/controllers/console/system_manage_extend.py` 同款装饰器模式（`@system_admin_required_extend` 或应用域登录装饰器，按权限口径定）；Celery 复用 `extend_high`/`extend_low` 队列或新建专用队列；迁移脚本走 `api/migrations_extend` 独立 Alembic 链；前端遵循 web/AGENTS.md 强制的 contract + TanStack Query 规范（`web/contract/console/`）。
- **消除跨服务鉴权基础设施**：两方案均消除「csrf_token 当 Bearer」与「共享 SECRET_KEY 伪造 JWT」两个 hack，为 P5 的 `SECRET_KEY` 轮换扫清障碍。
- **数据模型主权**：方案 B 将 GORM AutoMigrate 表纳入 Alembic 管理，消除双建表主权；方案 A 冻结表结构仅做冷备。
- **排期依赖**：本 change 是 P5（admin 废弃）的前置；与 P0/P2（合并上游）无代码冲突可并行；但方案 B 的前端改动建议排在 P3（1.15.0 合并，`text-generation/index.tsx` 大重构）之后落地，避免二次冲突——详见 design.md 排期权衡。

## Impact

- **前端**：`web/service/web-extend.ts`（删除或改写）、`web/utils/batch-progress-manager.ts`、`web/app/components/share/text-generation/index.tsx`（fork +719 行的主要来源）、`run-batch/batch-progress/index.tsx`、`web/next.config.ts`、`web/docker/entrypoint.sh`；方案 B 另新增 `web/contract/console/` 契约文件。
- **后端（仅方案 B）**：新增 batch workflow service/Celery tasks/Console controller；`api/migrations_extend` 新增迁移；`api/services/batch_workflow_statistics_service.py` 改造。
- **Go 侧**：`admin/server/service/gaia/batch_workflow.go`、`worker_pool.go`、`admin/server/api/v1/gaia/workflow.go` ——本 change 不动，随 P5 删除。
- **数据库**：`batch_workflows_extend`、`batch_workflow_tasks_extend`（方案 A 冷备；方案 B 迁移主权 + `user_id` 数据迁移）。
- **外部系统**：无外部客户端依赖（批量工作流仅 Dify 前端调用）。
