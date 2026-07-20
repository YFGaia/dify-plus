# Design: p1-batch-workflow-disposition

## Context

批量工作流允许用户在 WebApp 运行页（Explore/installed app 的 text-generation 界面）上传 Excel，由后台逐行调用 workflow/completion 应用并汇总结果下载。当前实现横跨三层：

- **前端**：`web/service/web-extend.ts` 8 个 fetch 直调 `/admin/gaia/workflow/batch/*`（processing / list / {id}/progress / stop / resume / retry / retry-failed / download），取 `csrf_token` cookie 当 Bearer token。调用方 3 处：`web/app/components/share/text-generation/index.tsx`（批量运行 tab，fork +719 行的主要来源）、`run-batch/batch-progress/index.tsx`（进度条组件）、`web/utils/batch-progress-manager.ts`（跨页面轮询管理器）。
- **Go 侧（admin/GVA）**：~2,634 行——`admin/server/service/gaia/batch_workflow.go`（691，Excel 解析 + 任务编排）、`worker_pool.go`（1,274，自研 worker pool + 限流重试）、`admin/server/api/v1/gaia/workflow.go`（669，HTTP API）。任务表 `batch_workflows_extend` / `batch_workflow_tasks_extend` 由 GORM AutoMigrate 建表，`user_id` 是 GVA `sys_users` 的 uint 主键。
- **鉴权 hack**：Go worker 用与 api 共享的 `SECRET_KEY` 伪造 Dify 兼容 JWT，回调 `/console/api/installed-apps/{id}/workflows/run` 与 completion-messages 执行任务。

**关键事实**：生产 nginx 模板（`docker/nginx/conf.d/default.conf.template`）没有 `/admin` location，标准 compose 部署下前端请求 `/admin/gaia/...` 会 404——该功能仅在 dev 模式（`web/next.config.ts` L40-41 rewrite 到 `:8888`）或自定义网关部署下可达，**疑似低使用率甚至零使用**。

另有 `api/services/batch_workflow_statistics_service.py`（854 行独立报表脚本）SQL 直接 `INNER JOIN sys_users`（L146），是 GVA 表的另一处 Python 侧引用。

**约束**：本 change 是 P5（admin 废弃）的前置阻塞项；`text-generation/index.tsx` 将在 P3（合并 1.15.0）中被上游大幅重构。

## Goals / Non-Goals

**Goals:**

- 通过决策门（使用量 SQL + 业务确认）在方案 A（删除）/ 方案 B（迁移 Flask + Celery）之间得出书面结论并执行。
- 无论选哪个方案，最终消除 Dify 前端对 `/admin/gaia/*` 的全部运行时调用，以及「csrf_token 当 Bearer」「共享 SECRET_KEY 伪造 JWT」两个鉴权 hack。
- 方案 B 下：任务表建表主权移交 `api/migrations_extend`，`user_id` 迁为 account uuid，执行链路改为进程内调用，前端改为 Console API（contract-first）。

**Non-Goals:**

- 不删除 Go 侧代码（`admin/server/**`）——随 P5 admin 废弃统一清理。
- 不轮换 `SECRET_KEY`——属于 P5 收尾动作。
- 不重做批量运行的 UI/UX（方案 B 保持现有交互，仅替换数据层）。
- 不处理 `/admin` 的其他 gaia 端点（约 55 个，见 P5）。
- 不新增批量工作流的新功能（如定时批量、模板管理）。

## Architecture Assessment

### Existing Design Reuse

- **Console 扩展 controller 模式**：`api/controllers/console/system_manage_extend.py` 已确立「独立 extend 文件 + 权限装饰器 + `api.add_resource`」模式，方案 B 的 Console API 沿用（新建 `batch_workflow_extend.py` 类文件，不改上游路由文件）。
- **Celery 扩展队列**：fork 已有 `extend_high`（见 `api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py`）/ `extend_low` 队列与 worker 部署，方案 B 的批量任务可复用或按同模式新增 `batch_workflow` 专用队列。
- **独立 Alembic 链**：`api/migrations_extend/` 已有 14 个版本、约 18 张扩展表的管理经验，新迁移脚本按既有命名与流程追加。
- **应用内部执行入口**：上游 `AppGenerateService.generate()` 是 Console 调试运行与 Service API 的共同入口，方案 B 直接进程内调用，替代 HTTP + 伪造 JWT。
- **前端 contract-first 模式**：`web/contract/console/` + `consoleQuery`（TanStack Query）是 web/AGENTS.md 的强制规范，方案 B 的 8 个调用全部按此重写，参考 `frontend-query-mutation` skill。
- **历史范例**：`openspec/changes/archive/2026-04-02-admin-migration-phase2-quota/` 展示了「GVA 功能迁 Console」的完整套路（service + controller + 权限装饰器 + 前端页面），方案 B 是同一套路的放大版（多了 Celery 与数据迁移）。

### Boundaries and Ownership

- **capability 归属**：批量工作流属于「WebApp 运行侧」能力（用户维度，非管理员维度）。方案 B 的 Console API 挂 **app/installed-app 域**而非 `system-manage-extend` 管理域——权限用登录用户即可（`@login_required` + installed app 访问校验），不需要 `@system_admin_required_extend`（原 Go 实现也是普通用户可用）。
- **数据所有权**：方案 B 后 `batch_workflows_extend` / `batch_workflow_tasks_extend` 归 api 侧 Alembic 独占管理；Go 侧 GORM AutoMigrate 在 P5 删除前不再是建表主权方（迁移脚本用 `IF NOT EXISTS` 语义兼容已建表的存量库）。
- **失败/重试/取消职责**：方案 B 中批次编排状态机（pending/running/paused/stopped/completed/failed）由 Flask service 层拥有；单任务执行、重试与限流由 Celery 任务拥有（`max_retries` + 幂等键）；前端只做轮询展示与操作触发。
- **并发控制**：Go worker pool 的并发上限语义由 Celery 队列 worker 并发度（`-c` 参数/队列隔离）承接，不再自研 pool。

### Options and Rationale

三个候选，取舍如下：

| 选项                               | 说明                                     | 取舍                                                                                      |
| ---------------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------------- |
| 方案 A：删除功能                   | 前端删批量 tab 与数据层，Go 侧随 P5 删   | 成本最低（约 1-2 天），永久消除 ~2,634 行 Go 维护负担；代价是功能消失，若误判使用量需回滚 |
| 方案 B：迁 Flask + Celery          | 后端重写 + 数据迁移 + 前端改 Console API | 保功能、除 hack、收数据主权；估 1-2 周，且前端部分与 P3 有冲突风险                        |
| （否决）保留 admin-server 只跑批量 | 长期维持一个 Go 服务只为此功能           | 与废弃 admin 的总目标矛盾，鉴权 hack 与 SECRET_KEY 耦合无法消除——线路图 D2 已明确不建议   |

**选择机制**：不预先拍板，由决策门的 SQL 证据 + 业务确认决定。默认倾向方案 A（生产不可达的事实强烈暗示零使用）。

### Quality Attributes

- **安全**：两方案均消除双向鉴权 hack；方案 B 的 Console API 走标准 console session 鉴权，下载接口校验批次归属（user_id == current_user.id），防越权下载他人结果。
- **可靠性**：方案 B 用 Celery 的 ack/retry 替代自研 worker pool；批次状态更新用原子 UPDATE 防并发覆盖；任务级幂等键防重试重复扣费（批量执行走 workflow 会触发 fork 计费链路）。
- **性能**：批量任务进 `extend_low`（或专用队列），与计费高优任务隔离，避免大批量 Excel 拖垮额度扣减时效。
- **可观测性**：批次/任务状态入库可查；Celery 任务沿用现有日志与 flower/监控体系，优于 Go pool 的内存态。
- **可测试性**：service 层纯函数化（解析、状态机）可单测；Console API 可用现有 pytest 模式覆盖。
- **可部署性**：方案 B 无新服务、新中间件——复用现有 api + Celery worker 拓扑，仅新增迁移脚本与（可选）队列声明。

### Complexity and Exceptions

- 方案 B 新增依赖 `openpyxl`（若 `api/pyproject.toml` 尚无；pandas 已在依赖树则优先用 openpyxl 单读避免引入 pandas 重依赖）——单一用途、成熟库，风险低。
- 不新增服务、存储、协议；`batch_workflow_tasks_extend` 的行级任务量可能大（每 Excel 行一条），沿用现表结构不重设计，归档/TTL 治理挂到 P4（billing-quota-hardening）的数据治理项之外单列为 Open Question。

## Decisions

### D-1 决策门先行，SQL 证据 + 业务确认双门槛

用以下 SQL 验证真实使用量，并向业务方确认后书面记录（回填线路图 D2 与本 change proposal）：

```sql
SELECT count(*) FROM batch_workflows_extend WHERE created_at > now() - interval '90 days';
SELECT date_trunc('month', created_at) AS m, count(*) FROM batch_workflows_extend GROUP BY 1 ORDER BY 1 DESC;
```

**理由**：删除是不可逆的用户可见变更，线路图风险登记明确要求「D2 决策前必须完成使用量 SQL 验证并向业务确认」。仅凭「生产 nginx 无 /admin location」推断零使用不充分——存在自定义网关部署的可能。
**替代方案**：直接按「生产不可达」删除（被否决：证据链不完整）；先停路由观察（被否决：dev rewrite 本来就只影响开发环境，观察无意义）。

### D-2 方案 A 只删前端，Go 侧留给 P5

方案 A 的删除范围严格限定前端四处（批量 tab 代码、`run-batch/batch-progress/`、`web-extend.ts`、`batch-progress-manager.ts`）+ i18n 清理；Go 侧 ~2,634 行与 GVA 路由注册**不动**。
**理由**：Go 侧删除与 admin 其余 55 个端点的清理是同一性质工作，P5 一次性删 `admin/` 目录更干净；提前删反而制造 admin 分支噪音。数据表保留冷备（写归档说明入 `docs/dify-plus/`），避免误删历史数据。
**替代方案**：本 change 同步删 Go 代码（被否决：拆碎 P5 的原子清理）。

### D-3 方案 B 执行链路：进程内调用 `AppGenerateService`，不走 HTTP

Celery 任务内直接调用 `AppGenerateService.generate()`（blocking 模式取结果），以批次记录的 account 构造 user 上下文。
**理由**：消除伪造 JWT 的根因——Go 侧走 HTTP 是因为跨进程/跨语言别无选择，Python 侧同进程生态没有这个约束。进程内调用还天然继承 fork 计费挂点（`persistence.py` 节点扣费），无需额外适配。
**替代方案**：继续 HTTP 调 Console API + 真实 token（被否决：token 生命周期管理复杂、多一跳网络失败面）；调 Service API + app api key（被否决：批量运行语义是「用户本人在 WebApp 运行」，应按 account 计费归因，而非 api_token 归因）。

### D-4 方案 B 数据迁移：主权移交 + user_id uint→uuid

新增 `migrations_extend` 迁移脚本：(1) 用 `IF NOT EXISTS` 语义声明两张表（兼容 GORM 已建表的存量库，并将其纳入 Alembic 版本管理）；(2) `user_id` 列改 uuid：新增 `account_id` uuid 列 → 存量数据经 `sys_users.id → sys_users.email/uuid 关联 accounts` 映射回填 → 无法映射的置 NULL 并记录 → 切换代码读写新列 → 旧列延后删除。
**理由**：GVA `sys_users` 表在 P5 会被 DROP，uint 外键不迁移则历史数据失去归属；渐进式改列（加列-回填-切换）比原地改类型安全，可回滚。
**替代方案**：清空历史数据从零开始（保留为决策门备选——若历史数据无查询需求，业务确认后可大幅简化迁移）。

### D-5 方案 B API 面：Console API 语义兼容原 8 端点

新 controller（如 `api/controllers/console/extend/batch_workflow_extend.py`，具体落位实现时按 console extend 文件现状定）提供与原 `/admin/gaia/workflow/batch/*` 一一对应的 8 个端点，路由挂 console app 域（如 `/console/api/installed-apps/<id>/batch-workflows/*`），响应字段名与进度轮询语义（status 枚举、processed_rows/total_rows 等）保持兼容。
**理由**：`batch-progress-manager.ts` 与进度组件对响应结构有耦合，语义兼容可把前端改动压缩为「换 URL + 换鉴权 + contract 化」，降低与 P3 重构的叠加风险。权限从「csrf hack」升级为标准 console 登录态 + 批次归属校验。
**替代方案**：重新设计 REST 风格 API（被否决：收益仅美观，代价是前端轮询层重写）。

### D-6 排期：方案 B 的前端改动排在 P3 之后

方案 B 拆两段——**后端（模型/迁移/Celery/Console API）可立即做**，与 P0/P2 无代码冲突；**前端改造（text-generation/index.tsx 等）等 P3 合并 1.15.0 落地后再做**。
**理由**：`text-generation/index.tsx` 在 1.15.0 中被上游大幅重构（P3 高危冲突文件清单第一位），若先改再合并会二次冲突、白做一遍。期间旧链路（dev rewrite → admin）保持可用，无功能空窗。方案 A 的删除同理建议在 P3 合并前完成（删除比保留更容易解冲突——上游重构时 fork 侧无批量代码需要保留）。
**替代方案**：前后端一次做完（被否决：与 P3 撞车）；全部推迟到 P3 后（被否决：后端部分没必要等，先行可缩短 P5 等待链）。

### D-7 共同收尾允许延至 P5

`web/next.config.ts` 删 `/admin` rewrite（L40-41）、`web/docker/entrypoint.sh` 删 `NEXT_PUBLIC_ADMIN_API_URL`（L46）在本 change 内作为收尾任务执行；若执行时 admin 的其他功能仍需 dev 调试（P5 未启动），可显式移交 P5 清单。
**理由**：rewrite 与死变量是 admin 整体残留的一部分，本 change 消除唯一调用方后删除最自然，但不应阻塞本 change 验收。

## Risks / Trade-offs

- [批量工作流实际有人用但 SQL 样本不足（如季度性使用）被误删] -> 决策门要求按月分布 SQL + 业务访谈双确认；方案 A 保留数据表与 git 历史，功能可按方案 B 路径恢复。
- [方案 B 的 `user_id` 存量映射失败（sys_users 与 accounts 无可靠关联键）] -> 迁移脚本先跑映射率统计（dry-run 报告），映射率低时升级为决策门备选「清空历史数据」，需业务再确认。
- [进程内调用 `AppGenerateService` 在 Celery worker 中的上下文差异（无 request context、login user 缺失）] -> 参考现有 Celery 任务中调用 app 服务的模式先做 spike 验证（tasks.md 前置任务）；必要时封装最小执行入口。
- [大批量任务挤占 Celery 队列影响计费任务时效] -> 批量任务固定走低优先队列并限制并发；不与 `extend_high` 混队。
- [P3 合并期间批量功能出现空窗或行为漂移（方案 B）] -> 前端改造排 P3 后（D-6），后端新旧两套 API 在切换窗口内并存，前端一次性切换。
- [Alembic 声明已存在的 GORM 表导致升级失败] -> 迁移脚本用存在性检查（inspector）幂等处理，新库/存量库双路径均在任务中验证。
- [批量执行经计费链路后行为变化（原 Go 路径伪造 JWT 也走 Console API 计费，语义应一致）] -> 回归时对比单条 workflow 运行与批量运行的 `account_money_extend` 扣减一致性。

## Migration Plan

**决策门（两方案共同前置）**：生产库执行使用量 SQL → 业务确认 → 结论写入本 change 与线路图 D2 → 按结论走 A 或 B。

**方案 A**：

1. 删除前端四处代码 + i18n 无用 key 清理 → `pnpm lint` + `type-check:tsgo` + `build` 通过。
2. 撰写数据表冷备归档说明（表名、用途、保留原因、恢复路径）入 `docs/dify-plus/`。
3. 收尾：删 `/admin` rewrite 与 `NEXT_PUBLIC_ADMIN_API_URL`（或移交 P5）。
4. 回滚：git revert 即可，无数据变更。

**方案 B**（分两段部署）：

1. **后端段**（P3 前可做）：迁移脚本（表主权 + account_id 列 + 回填）→ service/Celery/Console API 上线 → 新 API 用脚本/Postman 冒烟（上传→轮询→下载全链路）→ 此时前端仍走旧链路，双轨并存。
2. **前端段**（P3 后）：contract 定义 → 8 个调用切换 → 删 `web-extend.ts` 旧实现 → 全链路回归（上传/进度/停止/恢复/重试/下载 + 计费扣减一致性）。
3. 收尾：删 rewrite 与死变量；`batch_workflow_statistics_service.py` 切换到 accounts join。
4. 回滚：前端段回滚 = revert 前端 commit（旧链路仍在）；后端段回滚 = 迁移脚本 downgrade（account_id 列可安全删除，原 user_id 列未动）。

## Open Questions

- 决策门 SQL 的执行环境：哪套生产库有权限跑？由谁执行（DBA/运维）？
- 方案 B 时 `batch_workflow_tasks_extend` 的行级数据治理（归档/TTL）是否需要，挂哪个 change？
- 方案 B 的 `sys_users → accounts` 映射键最终用什么（email？GVA 建号时是否留了 account uuid 字段）？需在迁移 dry-run 前确认表结构。
- 共同收尾（rewrite/死变量删除）在本 change 执行还是移交 P5——取决于执行时 P5 是否已排期。
