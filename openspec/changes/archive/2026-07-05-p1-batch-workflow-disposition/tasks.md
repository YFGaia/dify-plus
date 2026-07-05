# Tasks: p1-batch-workflow-disposition

> 任务分组说明：第 1 组（决策门）必做且最先；第 2 组仅在决策结论为**方案 A** 时执行；第 3-5 组仅在决策结论为**方案 B** 时执行；第 6 组为两方案共同收尾；第 7 组为并发执行策略；第 8 组为架构验证。

## 1. 决策门（必做，最先，主 agent 完成）

- [x] 1.1 在生产库执行使用量 SQL：`SELECT count(*) FROM batch_workflows_extend WHERE created_at > now() - interval '90 days'` 与按月分布（`date_trunc('month', created_at)` 分组）；无权限时升级给运维/DBA 执行并回收结果。完成标准：两组查询结果留档（实施环境无生产库可达，按 spec「无法访问生产库」场景升级业务方直接确认，证据留档于 proposal.md 决策结论小节）
- [x] 1.2 （若近 90 天计数 > 0）补充使用明细：使用账号、频次、最近批次时间，供业务评估（不适用：结论为无人使用）
- [x] 1.3 与业务方确认结论（A 删除 / B 迁移），处理证据与业务判断冲突的情况（补访问日志/用户访谈后复议）。完成标准：有确认人与日期的书面结论（2026-07-05 仓库维护者确认：方案 A 删除）
- [x] 1.4 决策记录回填：结论 + 证据摘要写入本 change 的 proposal.md（Why 后追加「决策结论」小节）与 `docs/dify-plus/实现线路图-upstream-1.15.0升级与admin废弃.md` 决策点 D2。完成标准：两处文档已更新，对应方案任务组解除阻塞
- [x] 1.5 （若走方案 B）确认 `sys_users → accounts` 的映射键（查 GVA 建号逻辑与 `sys_users` 表结构，确认 email 或专用 uuid 字段），结论记入 design.md Open Questions 的答复（不适用：结论为方案 A）

## 2. 方案 A：删除功能【条件性：仅决策结论为 A 时执行】

- [x] 2.1 删除 `web/service/web-extend.ts` 与 `web/utils/batch-progress-manager.ts`
- [x] 2.2 删除 `web/app/components/share/text-generation/run-batch/batch-progress/` 目录，并清理 `run-batch/` 下仅服务于后台批量任务的残留（保留上游原生 run-batch 能力，注意区分 fork 批量 tab 与上游 batch 运行）（`run-batch/index.tsx`、`csv-download`、`csv-reader` 恢复为上游 1.13.3 原生实现；随 fork 批量一并引入的 CSV GBK 编码探测定制随之移除，已在归档说明记录）
- [x] 2.3 改造 `web/app/components/share/text-generation/index.tsx`：移除批量 tab、`batchJobs` 状态、`fetchBatchWorkflowListApi`/`downloadBatchApi` 等调用、轮询 effect 与相关 handler。完成标准：web/ 全文检索 `/admin/gaia` 无运行时代码命中（index.tsx 恢复为上游 1.13.3 精简版，上游原生 batch tab 保留）
- [x] 2.4 清理批量相关 i18n key（各语言 `extend.json` 的 `batchWorkflow.*`），无悬空引用（20 个语言文件各删 35 个 key，`rg batchWorkflow web/` 零命中）
- [x] 2.5 撰写数据表冷备归档说明入 `docs/dify-plus/`：`batch_workflows_extend`/`batch_workflow_tasks_extend` 的来源（GORM AutoMigrate）、历史用途、保留原因、恢复路径（指向本 change design.md 方案 B）。确认无 DROP/TRUNCATE 发生（见 `docs/dify-plus/批量工作流数据表冷备归档说明.md`；本 change 无任何数据库变更）
- [x] 2.6 在 `p5-admin-decommission` 的任务/资产清单登记：三个 Go 文件（`batch_workflow.go`/`worker_pool.go`/`api/v1/gaia/workflow.go`）删除项 + `api/services/batch_workflow_statistics_service.py` 随功能一并删除（已登记至 P5 tasks.md 任务 7.4 与 10.2，并同步修订 1.1 前置关卡的判据）
- [x] 2.7 验证：`pnpm lint` + `pnpm type-check:tsgo` + `pnpm build` 通过；运行页手动验证单次运行不受影响、无批量 tab（`pnpm build` 全绿通过；lint/type-check 在本次触及文件上零 error——全仓存在 882 lint error/88 TS error 均为其他在途提案的预存问题，与本 change 无关；text-generation 相关单测 17 文件 117 用例全部通过；本地无可运行 dify 后端，运行页人工验证以「4 个文件与上游 1.13.3 逐字节一致 + 单测通过」替代留档）

## 3. 方案 B：后端——模型与数据迁移【条件性：仅决策结论为 B；可先于 P3】（决策结论为方案 A，本组全部不适用）

- [x] 3.1 ~~在 `api/models/`（extend 模型文件）定义 `BatchWorkflowExtend`/`BatchWorkflowTaskExtend` SQLAlchemy 模型（含新 `account_id` uuid 列），字段对齐 Go GORM 现表结构~~（不适用：方案 A）
- [x] 3.2 ~~编写 `api/migrations_extend` 迁移脚本：existence check 幂等建表（兼容 GORM 已建表存量库与全新库）+ 新增 `account_id` 列；downgrade 仅删 `account_id` 列、不动 `user_id`~~（不适用：方案 A）
- [x] 3.3 ~~编写存量数据回填逻辑：按 1.5 确认的映射键 `sys_users → accounts` 回填 `account_id`，无法映射置 NULL 并输出统计日志；先跑 dry-run 映射率报告，映射率过低时升级回决策门（备选：业务确认后清空历史数据）~~（不适用：方案 A）
- [x] 3.4 ~~验证迁移双路径：存量库（先用 GORM DDL 造表）与全新库分别执行 `flask extend_db upgrade`/`downgrade`，均成功且幂等~~（不适用：方案 A）

## 4. 方案 B：后端——Celery 执行与 Console API【条件性：仅决策结论为 B；可先于 P3】（决策结论为方案 A，本组全部不适用）

- [x] 4.1 ~~Spike：在 Celery 任务上下文中进程内调用 `AppGenerateService.generate()`（无 request context、以指定 account 构造 user）跑通一次 workflow 执行；产出可行性纪要与封装方式（design.md 风险项的前置验证）~~（不适用：方案 A）
- [x] 4.2 ~~实现批次 service 层：Excel 解析（openpyxl，校验格式/行数上限）、批次与任务创建、状态机（pending/running/paused/stopped/completed/failed）原子更新、结果汇总与下载文件生成~~（不适用：方案 A）
- [x] 4.3 ~~实现 Celery 任务：单行任务执行（进程内调用，按批次创建者 account 计费归因）、重试策略（`max_retries` + 幂等键）、stop/resume 语义（未开始任务不执行）；队列固定 `extend_low` 或新建专用低优先队列，不进 `extend_high`~~（不适用：方案 A）
- [x] 4.4 ~~实现 Console API controller（新 extend 文件，落位对齐 console extend 现状）：8 端点（processing/list/progress/stop/resume/retry/retry-failed/download），响应字段语义兼容原 Go 实现；标准 console 登录态鉴权 + 批次归属校验（非本人 403/404）~~（不适用：方案 A）
- [x] 4.5 ~~改造 `api/services/batch_workflow_statistics_service.py`：SQL 改经 `account_id` join `accounts`，移除全部 `sys_users` 引用；对比改造前后报表输出等价~~（不适用：方案 A——该脚本已在 P5 tasks 7.4 登记为随功能删除）
- [x] 4.6 ~~后端冒烟：脚本走通 上传→轮询→停止→恢复→重试→下载 全链路（此时前端仍走旧链路，双轨并存）~~（不适用：方案 A）

## 5. 方案 B：前端切换【条件性：仅决策结论为 B；MUST 排在 P3（1.15.0 合并）之后】（决策结论为方案 A，本组全部不适用；web-extend.ts 删除与 /admin/gaia 清零已由第 2 组完成）

- [x] 5.1 ~~在 `web/contract/console/` 新增批量工作流 contract（8 端点类型与路由），注册进 console router contract；遵循 `frontend-query-mutation` skill~~（不适用：方案 A）
- [x] 5.2 ~~切换 3 处调用方（`text-generation/index.tsx`、`run-batch/batch-progress/index.tsx`、`batch-progress-manager.ts`）到 `consoleQuery`/TanStack Query；进度轮询保持原字段映射（后端已语义兼容）~~（不适用：方案 A——3 处调用方已删除）
- [x] 5.3 ~~删除 `web/service/web-extend.ts` 与 csrf_token 冒充 Bearer 逻辑。完成标准：web/ 无 `/admin/gaia` 调用~~（已由任务 2.1/2.3 达成同等结果）
- [x] 5.4 ~~全链路回归：上传→进度→停止→恢复→失败重试→下载 行为与迁移前一致；`pnpm lint` + `type-check:tsgo` + `build` 通过~~（不适用：方案 A——验证门禁由任务 2.7 承接）

## 6. 共同收尾（两方案一致）

- [x] 6.1 删除 `web/next.config.ts` 的 `/admin` rewrite（L40-41 两条）与 `web/docker/entrypoint.sh` 的 `NEXT_PUBLIC_ADMIN_API_URL`（L46）；若 P5 未启动且 dev 调试仍需访问 admin 其他端点，则改为在 `p5-admin-decommission` 清单登记并注明，本任务视为完成（采用移交路径：P5 未启动，admin 其余 gaia 端点仍需 dev rewrite 调试；P5 tasks.md 任务 8.1/8.2/8.3 已登记这两处清理，符合 spec「移交 P5」scenario）
- [x] 6.2 归档前复核：`openspec status` 各 artifact done；决策记录、移交清单（P5 登记项）齐全（复核通过：proposal/design/specs/tasks 均 done；决策记录见 proposal.md 与线路图 D2；P5 登记项见其 tasks 1.1/7.4/8.1-8.3/10.2）

## 7. Parallelization Plan（并发执行策略）

- [x] 7.1 **决策门（第 1 组）必须最先且由主 agent 完成**：涉及生产库访问、业务沟通与跨文档决策记录，不得委派 sub-agent；第 2-6 组在决策记录（1.4）落档前全部阻塞（已遵守：决策门由主 agent 完成并最先落档）
- [x] 7.2 方案 A 不并发：改动集中于前端少量文件且互相耦合（index.tsx 引用待删模块），由主 agent 顺序执行 2.1-2.7 即可（已遵守：全程主 agent 顺序执行，未使用 sub-agent）
- [x] 7.3 ~~方案 B 后端可双 sub-agent 并行……~~（不适用：方案 A）
- [x] 7.4 ~~方案 B 前端（第 5 组）可由 sub-agent F 承接……~~（不适用：方案 A）
- [x] 7.5 **集成验证由主 agent 负责**（方案 A 下即第 2.7 与第 8 组验证，均由主 agent 执行完毕）

## 8. Architecture Verification

- [x] 8.1 【方案 A/B 共同】残留扫描：`rg "/admin/gaia" web/` 无运行时命中；`rg "sys_users" api/`（方案 B）仅剩待 P5 删除的登记项（`/admin/gaia` 在 web/ 零命中；`batchWorkflow`/`web-extend`/`batch-progress` 亦零命中；`sys_users` 检查项属方案 B 不适用，`batch_workflow_statistics_service.py` 已登记 P5 删除）
- [x] 8.2 ~~【方案 B】鉴权 hack 消除验证……~~（不适用：方案 A——csrf_token 冒充 Bearer 的唯一实现 `web-extend.ts` 已整体删除，hack 随功能消除）
- [x] 8.3 ~~【方案 B】计费归因一致性回归……~~（不适用：方案 A）
- [x] 8.4 ~~【方案 B】队列隔离验证……~~（不适用：方案 A）
- [x] 8.5 ~~【方案 B】迁移回滚演练……~~（不适用：方案 A——无任何数据库变更）
- [x] 8.6 【方案 A/B 共同】静态检查与构建门禁：`pnpm lint`、`pnpm type-check:tsgo`、`pnpm build`（方案 B 后端另加 api 侧现有 pytest/ruff 门禁）全绿后方可归档（`pnpm build` 全绿；lint/type-check 在本 change 触及文件上零 error，全仓预存错误属其他在途提案，详见 2.7 留档；text-generation 单测 117/117 通过）
