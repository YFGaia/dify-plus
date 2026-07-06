# 实现线路图：升级 upstream 1.15.0 + 计费/额度兼容改造 + 废弃 Go 管理后台

> 文档状态：**研究完成，待执行**
> 编写日期：2026-07-05
> 当前基线：分支 `merge/upstream-1.13.3`（已合并上游 1.13.3）
> 目标：合并 upstream `1.15.0`（2026-06-25 发布，最新稳定版）；升级计费/用户额度/API 额度二开能力实现兼容；废弃 `admin/`（gin-vue-admin）管理后台，仅保留用户额度管理于 Console 统一前台入口。
> 本文由 5 个并行调研任务汇总而成：二开差异面盘点、计费/额度实现深挖、admin 能力与残留依赖盘点、Console 前台承接评估、上游 1.13.3→1.15.0 冲击面分析。

---

## 0. 执行摘要

| 结论 | 说明 |
|---|---|
| 合并难度 | **高**（上游 1677 提交、~10,775 文件；web 转 pnpm monorepo、api 拆 uv workspace、header 被删除重构为 main-nav） |
| 最紧急事项 | **当前分支计费链路已断链**：相较 `origin/main` 丢失 4 处关键挂点（workflow 节点计费派发、`app_token_id` 写入、chatflow 密钥关联、Celery beat 额度重置注册），且 HEAD 内残留 6 个文件的未解决冲突标记 |
| 推荐合并策略 | **两步走**：1.13.3 → 1.14.2（消化 service 层重构与 quota v3），再 1.14.2 → 1.15.0（集中处理 main-nav 迁移与 monorepo 化） |
| admin 废弃阻塞项 | 仅 3 个：批量工作流（前端在用）、code 节点执行控制 `control_mail` 写入方、批量统计脚本依赖 `sys_users` 表；其余约 55 个 gaia 端点可随 admin 直接下线或按业务决策处置 |
| 用户额度管理 | 已迁入 Console（`/system-manage-extend/quota-management`），功能可用但未遵循 contract + TanStack Query 规范，需在前端阶段补齐 |

**总体阶段划分**（详见后文）：

```
Phase 0  基线止血（修断链、清冲突残留）        —— 必须最先做，否则后续全部白干
Phase 1  架构决策（4 个业务决策点）             —— 需要人工拍板
Phase 2  合并上游（1.14.2 → 1.15.0 两步）      —— 工作量最大
Phase 3  计费/额度兼容改造与加固               —— 与 Phase 2 交织
Phase 4  admin 废弃（迁移→切流→清理）          —— 可与 Phase 3 并行推进
Phase 5  前台入口统一与规范化                   —— 收尾
```

---

## 1. 现状关键事实（调研结论）

### 1.1 基线健康度（⚠️ 有伤）

1. **HEAD 提交内有 6 个文件残留未解决冲突标记**（`<<<<<<< HEAD ... >>>>>>> 1.13.3`）：
   - `api/core/workflow/nodes/knowledge_index/`（`__init__.py`、`entities.py`、`knowledge_index_node.py`）
   - `api/core/workflow/nodes/knowledge_retrieval/`（`knowledge_retrieval_node.py`、`retrieval.py`）
   - `api/core/workflow/nodes/trigger_plugin/trigger_event_node.py`
   - 当前工作区未提交改动正在修复它们，**必须先提交**。
2. **计费链路断链**（相较 `origin/main` 在 1.13.3 合并中丢失）：
   - `api/core/app/workflow/layers/persistence.py` 的 `_handle_node_succeeded` 中丢失 `update_account_money_when_workflow_node_execution_created_extend.delay(...)` 派发（origin/main L301）→ **workflow/chatflow 的 LLM 节点费用不再扣减**；
   - `api/core/app/apps/workflow/app_generator.py`（origin/main L172）与 `advanced_chat/app_generator.py`（L140）丢失 `extras["app_token_id"] = api_token.id` 写入 → **API 密钥额度累计失效**；
   - `advanced_chat/generate_task_pipeline.py`（origin/main L316-319）丢失密钥关联记录写入；
   - `api/extensions/ext_celery.py` 丢失「二开部分」代码块（origin/main L188-209）→ **三个额度重置定时任务（月度用户额度、日/月密钥额度）不再执行**。
   - 遗留死代码：hook 还挂在已无调用方的 `api/core/workflow/workflow_cycle_manager.py`（fork 为修 import 加回的旧路径「幽灵文件」）。
3. 疑似合并残留需清理确认：`api/core/workflow/workflow_cycle_manager.py`（+482）、`nodes/node_mapping.py`、两个 0 字节空文件（`api/controllers/service_api/dataset/upload_file.py`、`api/core/model_runtime/model_providers/bedrock/llm/llm.py`）等。

### 1.2 二开差异面规模（相对 1.13.3）

- api/ 150 文件（+9,912/-324）、web/ 175 文件（+10,108/-1,310）。
- 侵入式修改上游文件：api 约 45 个业务文件、web 约 40 个；独立 `*extend*` 文件占多数（隔离良好）。
- `api/migrations_extend/` 独立 Alembic 链 14 个版本、约 18 张扩展表；配置经 `api/configs/extend/__init__.py` 挂载。
- 完整清单见调研档案（子代理 A 报告），核心侵入面按功能域：计费额度（最重）、SSO/登录、系统管理、应用中心、批量工作流、消息上下文。

### 1.3 计费/额度体系（升级兼容的核心对象）

- 四条线：用户额度（`account_money_extend`）、API 密钥额度（`api_token_money_extend` 四表）、周期快照重置（3 个 Celery beat 任务，队列 `extend_low/extend_high`）、转发计费（`forwarding_*`、AI 绘图）。
- 与上游 `BillingService`（SaaS 云计费）**完全平行、不复用、不冲突**。
- 关键侵入点约 21 处（详见子代理 B 报告第 2 节），最脆弱的是：
  - `api/controllers/service_api/wraps.py` 的 `validate_app_token`（额度前置校验 + end_user↔account 映射），并因此改了约 9 个 service_api controller、30+ 方法签名；
  - `api/core/app/apps/*/app_generator.py` / `generate_task_pipeline.py` 的归因记录写入（上游 1.14/1.15 对这些文件做了 200-300 行级重写）；
  - `api/core/app/workflow/layers/persistence.py` 的节点计费派发（上游正把 provider 配额扣减也移到 graph 层，同一区域会再撞车）；
  - `api/controllers/console/apikey.py` 的额度字段联查（历次合并最难文件）。

### 1.4 上游 1.13.3 → 1.15.0 冲击面

- 总量：1677 提交、10,775 文件（api 2,784 / web 6,687）。
- **web 侧结构性变化**：
  - 转 pnpm monorepo：根级 `pnpm-workspace.yaml` + `catalog:` 依赖，`web/pnpm-lock.yaml` 删除；新增 `packages/dify-ui`、`packages/contracts` 等；
  - **`web/app/components/header/index.tsx` 被上游删除**，导航重构为 `web/app/components/main-nav/` → fork 的额度徽章（`account-money-extend`）、系统管理入口（`system-manage-nav-extend`）、`nav-extend`、`draw-nav-extend` 四个挂载点**宿主消失，需在 main-nav 体系重新实现**；
  - `@headlessui/react` 被移除（overlay 迁移到 dify-ui）→ 二开 UI 中还 import headlessui 的会编译失败；
  - contract 化提速：`web/contract/` +2,161 行，service 层多个模块删除迁入 contract。
- **api 侧结构性变化**：
  - 拆 uv workspace：`api/core/rag/datasource/vdb/` 66 文件迁到 `api/providers/vdb/*`；新增 `dify-agent` 包、`api/dify_graph/`；
  - Python 版本收窄为 `~=3.12.0`；gevent/gunicorn/celery 大版本升级；
  - service 层大规模「显式传 session」+ SQLAlchemy 2.0 `select()` 重构；console 控制器改为注入 user/tenant；
  - **上游 quota v3**：`BillingService` 新增 `quota_reserve / quota_commit / quota_release` 两阶段配额 + tenant 级 Redis 锁——与 fork 额度语义重叠，需架构决策（见 Phase 1）。
- **数据库**：上游新增 27 个迁移，与 fork 的 `*_extend` 表**无命名冲突**，双 Alembic 链可继续并存。唯一功能重叠：上游给 `recommended_apps` 原生加 `categories` JSON 列，与 fork 的 `recommended_category_extend` 两张分类表重叠，需决策数据源。
- **升级动作**：`flask db upgrade` 后必须执行 `flask backfill-plugin-auto-upgrade`；环境变量 19 增 2 删 1 改；SSRF 代理改默认拒绝（私有域名/IP 需配 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS/IPS` 加白）；修复路径穿越 CVE-2026-41948。

### 1.5 admin（Go 管理后台）现状

- 规模：~58 个 gaia 业务端点、~12k 行 Go + ~11.4k 行 Vue；与 Dify 共库（直读 accounts/tenants/messages 等，直写 `accounts`、`account_money_extend`、`system_integration_extend` 等）。
- **已迁移到 Console**：用户额度管理、系统集成（钉钉 SSO/OAuth2/邮箱测试/转发 Token）——GVA 侧同功能成为冗余双写点，应尽快下线。
- **主站残留依赖仅 3 处**：
  1. 批量工作流：`web/service/web-extend.ts` 8 个 fetch 直调 `/admin/gaia/workflow/batch/*`（调用方：`text-generation/index.tsx`、`batch-progress/index.tsx`、`batch-progress-manager.ts`）。注意：生产 nginx 模板没有 `/admin` location，标准部署下该功能实际不可达，**疑似低使用率**；
  2. code 节点执行控制：`api/core/workflow/nodes/code/control_extend.py`（与 `dify_graph` 下副本）读 redis 键 `control_mail`，该键**只由 admin 的 `SyncExecuteCode` 写入**，admin 停掉后链路断；
  3. `api/services/batch_workflow_statistics_service.py` SQL 直接 join GVA 的 `sys_users` 表。
- 安全耦合：admin 与 api 共享 `SECRET_KEY` 互相伪造/接受 JWT，废弃后建议轮换 `SECRET_KEY`。
- 待人工决策的模块：模型供应商网关/转发代理（外部 OpenAI 兼容客户端在用则需替代）、应用版本管理（公开端点 `/latest`、`/releases` 可能被桌面客户端拉取）。

### 1.6 Console 前台承接能力

- `/system-manage-extend/` 骨架可用可扩展（layout menuItems 加项即挂新页），后端 `@system_admin_required_extend` 模式成熟。
- 欠账：6 个管理端点全部走 legacy `service/base`（未用 contract + TanStack Query，违反 web/AGENTS.md 强制规范）；UI 手写表格/分页/弹窗未复用 base 组件；Toast 新旧混用（旧 `base/toast` 若被上游删除会编译失败）；quota 页有硬编码中文；权限口径不一致（前端 owner-only、后端 admin_or_owner）。
- 普通用户侧额度展示：`header/account-money-extend`（硬编码汇率 6.97，与后端配置 7.26 不一致）——随 main-nav 迁移时一并修。

---

## 2. Phase 0：基线止血（前置阻塞项，1-2 天）

> 目标：让 `merge/upstream-1.13.3` 成为一个"计费功能完整、无冲突残留"的可信基线。**不做完这步，禁止开始合并 1.15.0。**
>
> **状态（2026-07-05）**：0.1-0.5 已完成（change `p0-restore-billing-hooks` 已实施并归档，tag `fork-pre-merge-1.15.0` 已打）。挂点坐标与回归步骤见《计费回归基线清单》（`docs/dify-plus/计费回归基线清单.md`）；其中 6 条链路的**运行时回归**（0.4 验收"本阶段全绿"）因实现机无运行栈延后，需在具备 api+worker+beat+LLM 凭据的环境按清单执行并登记，完成后 Phase 0 才算全绿。

| # | 任务 | 验收标准 |
|---|---|---|
| 0.1 | 提交当前工作区的冲突标记修复（6 个 api 文件）及配套改动 | `git grep -l '<<<<<<<' -- api/ web/` 为空；`uv run --project api python -c "from app_factory import create_app"` 通过 |
| 0.2 | 对照 `origin/main` 恢复 4 处丢失计费挂点：`persistence.py` 节点计费派发、两个 `app_generator.py` 的 `extras["app_token_id"]`、`advanced_chat/generate_task_pipeline.py` 关联记录、`ext_celery.py` beat 任务注册（顺势加 `CeleryScheduleTasksConfig` 开关） | 手动触发 workflow 运行后 `account_money_extend.used_quota` 有增量；celery beat 列表含 3 个 extend 任务 |
| 0.3 | 清理幽灵文件/死代码：`workflow_cycle_manager.py` 中的 hook 死代码、两个 0 字节空文件、确认 `node_mapping.py`/`mcp/auth_provider.py` 等疑似残留的去留 | 无孤儿 import；py_compile 全量通过 |
| 0.4 | 建立计费回归基线：编写（或整理）覆盖 6 条链路的回归清单——Console 调试运行扣费、Explore 运行扣费、WebApp 登录+扣费、Service API 密钥日/月限额拦截、workflow LLM 节点扣费、月初额度重置 | 清单可重复执行，本阶段全绿 |
| 0.5 | 打 tag `fork-pre-merge-1.15.0` 留档 | tag 存在 |

---

## 3. Phase 1：架构决策（需人工拍板，与 Phase 0 并行）

| # | 决策点 | 选项与建议 |
|---|---|---|
| D1 | **fork 额度体系 vs 上游 quota v3** | 上游 1.14+ 引入 `quota_reserve/commit/release` 两阶段配额（面向 SaaS 计费）。建议：**本次升级保持 fork 自建表体系不变**（迁移成本与语义差异大），仅把挂点跟随上游代码搬迁；将「评估挂接上游 quota 接口」列为 2.0 时代的长期项。理由：fork 是按美元金额计费+归因到个人账号，与上游按订阅套餐 credits 语义不同 |
| D2 | **批量工作流处置** | **已定案（2026-07-05，业务方确认）：方案 A（删除功能）**。证据：生产 nginx 模板无 `/admin` location（标准部署不可达），业务方确认近 90 天无人使用；实施环境无生产库可跑 SQL，按决策门「无法访问生产库」场景升级业务方直接确认。执行：删前端批量 tab 与数据层（见 `openspec/changes/p1-batch-workflow-disposition`），Go 侧 ~2,634 行随 P5 删除，数据表冷备保留（归档说明见 `docs/dify-plus/批量工作流数据表冷备归档说明.md`） |
| D3 | **模型供应商网关/转发代理 + 应用版本管理** | **已定案（2026-07-05，业务方确认）：无外部依赖，随 admin 整体下线**。取证方式：实施环境为开发仓库、admin 服务未部署运行（无访问日志可采），按 spec「业务访谈」路径由仓库维护者直接确认三类端点（`/gaia/proxy/*`、`/gaia/forward/proxy/*`、`GET /latest`/`GET /releases`）无 OpenAI 兼容客户端、钉钉转发、桌面客户端在用。停路由观察期与业务确认合并豁免（开发环境无外部流量），处置记录见 `openspec/changes/archive/2026-07-05-p5-admin-decommission/cutover-record.md` |
| D4 | **探索页分类数据源** | 上游 `recommended_apps.categories`（JSON 列）vs fork 的 `recommended_category_extend` 两张表。建议：**迁移到上游原生 categories**，写一次性数据迁移脚本，删除 fork 分类表与 `database_retrieval.py` 侵入——减少一个长期侵入面 |
| D5 | **code 节点执行控制的新写入方** | **已定案并落地（2026-07-05，P5）**：采纳「Console 管理 API 维护 redis」——新表 `code_execution_control_extend`（DB 为 source of truth）+ Console 端点写后全量重建 redis `control_mail` 投影；同时恢复 1.13.3 合并中丢失的读侧接线（`DefaultWorkflowCodeExecutor` → `check_code` → `purview`）。实现见 `openspec/changes/archive/2026-07-05-p5-admin-decommission/` |

---

## 4. Phase 2：合并上游（核心工程，预计 2-4 周）

> 策略：**两步合并**，每步独立验证回归，避免一次性消化 1677 个提交。

### 4.1 Step A：1.13.3 → 1.14.2 **✅ 已完成（2026-07-05，p2-merge-upstream-1-14-2，合并提交 c4843eb9）**

> 实际执行修正：① web monorepo 化（pnpm workspace + packages/* + catalog）实际发生在上游 1.14.0，已随本步合并进入仓库，headlessui 亦已在 1.14.1 清零——Step B 的范围需据此重估；② `dify_graph` 在 1.14.2 已外部化为 PyPI 包 `graphon`，仓库内目录删除；③ service 层「显式传 session」未构成对外签名破坏，extend 适配量低于预期；④ 10 项挂点新坐标已登记入《与上游差异总表》第 7 节；⑤ 附带恢复了 1.13.3 合并时丢失的记忆上下文挂点（add_messages_context/control_registers 链路）。

重点消化：service 层「显式传 session」重构、SQLAlchemy 2.0 select() 迁移、console 控制器 user/tenant 注入、quota v3 引入（fork 不接入但要解冲突）、`dify_graph` 演进。

关键动作：
1. `git merge 1.14.2 --no-commit`，机械冲突（蓝图注册、models/`__init__.py`、i18n namespace）批量解决；
2. 逐个重新定位计费挂点（对照 Phase 0 建立的挂点清单）：
   - `persistence.py` / `generate_task_pipeline.py` / `app_generator.py` 若被上游移动或重写，把 hook 移植到新位置并记录新坐标；
   - `service_api/wraps.py` 的 `validate_app_token` 与 9 个 controller 签名改动重新对齐；
   - `apikey.py` 额度字段联查在上游 select() 重构基础上重写；
3. Python 3.12 收窄 + 依赖升级：更新 `api/Dockerfile`、`requirements.docker.txt`、uv.lock；
4. 回归 Phase 0 的 6 条计费链路 + SSO 登录（钉钉/OAuth2/Casdoor）+ 应用中心。

### 4.2 Step B：1.14.2 → 1.15.0 **✅ 已完成（2026-07-06，p3-merge-upstream-1-15-0，合并提交 72f5def0，tag `fork-merged-1.15.0`）**

> 实际执行修正：① monorepo/uv workspace 骨架实际已随 1.14.2 进入仓库，本步的构建适配集中在锁文件重生成、CI 构建上下文改仓库根（并删除 admin 构建 job）、`esbuild-wasm` 补回（`@serwist/turbopack` 构建期硬依赖）；② headlessui 已在 P2 清零，本步仅验收；③ main-nav 4 个挂载点在新体系重做完成，logo/home 指向应用中心，汇率 6.97 改由后端 `RMB_TO_USD_RATE` 经 login_config 下发；④ 探索页分类按 D4 完成原生化（migrations_extend 017 数据迁移 + 018 独立 drop，scratch 库演练通过），fork 两张分类表退役；⑤ 三段命令链在 scratch 库演练通过，升级 runbook 固化于《上游升级与回归检查清单》第 0 节；⑥ 本地 Docker 构建受宿主机代理环境阻塞，镜像构建与运行时回归留待 CI/部署环境。

重点消化：web monorepo 化、main-nav 重构、headlessui 移除、api providers/vdb 拆包。

关键动作：
1. **构建体系适配**（最大单项）：
   - 根级 `pnpm-workspace.yaml` + catalog 依赖落地；fork 的 `web/package.json` 增补依赖（dingtalk-jsapi、papaparse、jschardet、serwist 等）改为 catalog 兼容方式；锁文件以上游为准重装；
   - `docker/docker-compose.dify-plus.yaml`、web/api Dockerfile、CI 脚本适配 monorepo 与 uv workspace 多包构建；
2. **main-nav 迁移**（UI 侵入点重做，不是解冲突）：
   - 在 `web/app/components/main-nav/` 新体系中重新挂载：额度徽章（account-money-extend）、系统管理入口（system-manage-nav-extend）、nav-extend/draw-nav-extend；
   - 顺势把额度徽章的硬编码汇率 6.97 改为读后端配置；
3. **headlessui 清退**：扫描二开组件（invite-modal、system-manage-extend 等）替换为 dify-ui overlay 原语；
4. vdb import 路径修正（如有 fork 代码引用旧路径）；
5. **升级动作**：`flask db upgrade` → `flask extend_db upgrade` → `flask backfill-plugin-auto-upgrade`；`.env` 按 19 增 2 删 1 改对齐；**SSRF 加白**：企业内网域名/IP 配置 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS` / `SSRF_PROXY_ALLOW_PRIVATE_IPS`（否则 http 节点/工具调用内网会 403）；
6. 探索页分类按 D4 决策执行数据迁移；
7. 全量回归：计费 6 链路、SSO、应用中心、系统管理页、批量运行（若保留）、`pnpm lint + type-check:tsgo + build`、api 单测。

### 4.3 高危冲突文件清单（合并时逐个人工核对）

**api**：`controllers/service_api/wraps.py`、`controllers/console/apikey.py`、`core/app/workflow/layers/persistence.py`、`core/app/apps/*/app_generator.py`、`*/generate_task_pipeline.py`、`models/model.py`（内嵌 3 个 extend 模型类，建议趁机外迁到独立文件）、`libs/oauth.py` + `controllers/console/auth/oauth.py`（上游新增 oauth_access_tokens 体系）、`controllers/console/feature.py`（CVE 防护包装层）、`core/memory/token_buffer_memory.py`、`events/__init__.py`（自带 Events 框架）、`extensions/ext_celery.py`、`services/account_service.py`。

**web**：`components/share/text-generation/index.tsx`（fork +719 行，若 D2 决定砍批量功能可大幅简化）、`contract/router.ts` + `contract/console/system.ts`、`context/app-context*`、`config/index.ts`、`i18n-config/{i18next-config.ts,resources.ts}` 的 extend namespace 注册、`i18n/uk-UA`（上游更新会复活，注意策略统一）。

---

## 5. Phase 3：计费/额度兼容改造与加固（合并后 1 周）

> 目标：降低下次升级的冲突成本，修复已知技术债。

| # | 任务 | 说明 |
|---|---|---|
| 3.1 | **收窄 service_api 侵入面** | `validate_app_token` 不再改 30+ 方法签名传 `api_token`，改用 contextvar（或 flask.g）传递 → 9 个 controller 回归上游原样，未来合并冲突大幅减少 |
| 3.2 | 扣费幂等与原子化 | `message_was_created` handler 的账号扣费改原子 UPDATE（对齐 workflow 任务的写法）；Celery 重试加幂等键防重复扣费 |
| 3.3 | 配置统一 | 前端汇率读接口而非硬编码 6.97；`billing_extend.py` 硬编码初始额度 15 改读 `ACCOUNT_TOTAL_QUOTA` |
| 3.4 | 校验缓存 | `money_limit`/`is_money_limit` 加短 TTL 缓存；`is_money_limit` 的 `except: return True` 改精确异常处理，避免 DB 抖动误拦 |
| 3.5 | 数据治理 | `api_token_message_joins_extend` 增加归档/TTL 策略；三个重置任务改分批处理避免全表加载 |
| 3.6 | 建立「挂点注册表」文档 | 把 21 个侵入点的最新坐标登记进 `docs/dify-plus/与上游差异总表.md`，作为每次升级的 checklist |

---

## 6. Phase 4：admin 废弃（迁移 → 切流 → 清理，可与 Phase 3 并行）

> **状态（2026-07-05）：✅ 已完成**（change `p5-admin-decommission`）。4.1 批量工作流已随 P1 删除（D2 方案 A）；4.2 code 执行控制迁 Console 完成（含读侧接线恢复）；4.3 统计脚本已删除；4.4 按 D3 定案随 admin 下线。切流验证按开发环境等价判据放行（见 change 内 `cutover-record.md`）。清理：compose 无 admin 服务、`admin/` 目录已删（tag `pre-admin-removal`）、GVA 表清理迁移 `016_drop_gva_admin_tables` 已入库待部署环境执行、`SECRET_KEY` 轮换 runbook 已交付（`docs/dify-plus/SECRET_KEY轮换runbook.md`），实际轮换在部署环境维护窗口执行。

### 6.1 迁移（阻塞项处理）

| # | 任务 | 依据 |
|---|---|---|
| 4.1 | 按 D2 决策处置批量工作流：迁 Flask+Celery 或删除功能。若迁移：任务表建表迁入 `migrations_extend`，`user_id` 从 sys_users uint 改 account uuid，前端 `web-extend.ts` 8 个调用改为 Console API | 1.5 节残留依赖 1 |
| 4.2 | 按 D5 决策迁移 code 执行控制：Console 管理 API 写 `control_mail`（或改查库），配套管理 UI 挂到 system-manage-extend | 1.5 节残留依赖 2 |
| 4.3 | `batch_workflow_statistics_service.py` 改造脱离 `sys_users`（或随批量功能一并删除） | 1.5 节残留依赖 3 |
| 4.4 | 按 D3 决策处置模型供应商网关/转发代理/应用版本管理 | 外部客户端确认 |

### 6.2 切流验证

- GVA 侧额度管理/系统集成入口下线（已双写的两个模块直接停用 GVA UI），观察一个计费周期确认 Console 侧写入正常。

### 6.3 清理（一次性）

| 资产 | 位置 |
|---|---|
| `admin-web`（:8081）、`admin-server`（:8888）服务 | `docker/docker-compose.dify-plus.yaml` L1685-1742 |
| `docker/admin-server/config.docker.yaml`、`admin/deploy/*` | 部署资产 |
| `web/next.config.ts` 的 `/admin` dev rewrite、`web/docker/entrypoint.sh` 死变量 `NEXT_PUBLIC_ADMIN_API_URL` | 前端残留 |
| `api/controllers/console/auth/register_extend.py` + `api/libs/token.py` CSRF 白名单条目 + `ADMIN_GROUP_ID` 配置 | admin 专用注册端点 |
| 数据库 GVA 表：`sys_*`、`casbin_rule`、`exa_*`（确认无引用后一次性 DROP 迁移） | dify 库 |
| `admin/` 目录整体删除（保留 git 历史） | 仓库 |
| **轮换 `SECRET_KEY`**（admin 曾用共享密钥伪造 Dify JWT） | compose env，需全员重新登录 |

---

## 7. Phase 5：前台入口统一与规范化（收尾，约 1 周）

| # | 任务 | 说明 |
|---|---|---|
| 5.1 | quota-management / system-integration 数据层迁移到 contract-first：新建 `web/contract/console/system-manage-extend.ts` 注册进 `consoleRouterContract`，call site 改 `consoleQuery` + `queryOptions()/mutationOptions()` | 符合 web/AGENTS.md 强制规范；6 个端点 |
| 5.2 | UI 规范化：手写表格/分页/弹窗替换为 base（dify-ui）组件；旧 `Toast.notify` 全部迁到新 toast；`globalThis.confirm` 换 Confirm 组件 | 消除上游删除旧组件时的编译风险 |
| 5.3 | i18n 补齐：quota 页分页控件、email-api 说明的硬编码中文入 `extend.json` | 20 语言 |
| 5.4 | 权限口径统一：前端入口与后端 `@system_admin_required_extend` 统一为同一判断（建议均收敛 admin_or_owner，前端用 `isCurrentWorkspaceManager` 级别），并评估补 middleware 级守卫 | 修复已知不一致 |
| 5.5 | （可选）承接更多运营能力：如需迁 Dashboard 报表，按本骨架 + contract 模式扩展；跨租户视角需后端新增平台级 API（`admin_extend/tenant_extend` 字段为切入点）——列为后续独立项目 | 不阻塞本线路图 |

---

## 8. 里程碑与验收

| 里程碑 | 验收 |
|---|---|
| M0 基线止血完成 | 冲突标记清零；6 条计费链路回归全绿；tag 留档 |
| M1 决策定案 | D1-D5 均有书面结论（更新到本文档） |
| M2 合并 1.14.2 | **✅ 达成（2026-07-05，p2）**：api/web 构建通过（ruff/py_compile/导入冒烟/单测 12080 passed；web lint/type-check/build 全绿）；干净库双 Alembic 链从零验证通过；计费+SSO+应用中心按代码级链路追踪+单测回归通过（本地无运行环境，运行时回归待部署环境执行《计费回归基线清单》） |
| M3 合并 1.15.0 | **✅ 达成（2026-07-06，p3）**：monorepo 构建通过（pnpm lint/type-check/build 全绿）；main-nav 4 挂载点全部在新体系恢复（92 单测过）；三段命令链在测试库演练成功；api 单测 14582 passed、web 28777 passed；SSRF 白名单配置就位（内网清单待业务方、实测留待部署环境）；运行时回归按 runbook 留待部署环境执行 |
| M4 计费加固完成 | service_api 签名侵入清零；幂等/原子化落地；挂点注册表入档 |
| M5 admin 下线 | **✅ 代码侧达成（2026-07-05，P5）**：compose 无 admin 服务；`web/service/web-extend.ts` 已删除（P1）；GVA 表清理迁移已入库（部署环境执行时 pg_dump 先行）；SECRET_KEY 轮换 runbook 已交付，实际轮换待部署环境维护窗口 |
| M6 前台规范化 | system-manage-extend 全部 contract 化；lint/type-check/测试通过 |

## 9. 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 计费挂点在上游 graph 层重构中再次位移（上游正把 provider 配额扣减移入同区域） | 高 | Phase 0 先建挂点清单与回归用例；每步合并后立即跑计费回归 |
| main-nav 重做后额度/管理入口交互回退 | 中 | 迁移前截图留档对比；owner/普通用户双角色验证 |
| monorepo 化导致 fork Docker/CI 构建长期不稳定 | 中 | Step B 单独分支验证构建产物后再合入 |
| SSRF 默认拒绝导致企业内网工具调用批量失败 | 高（易漏） | 升级 checklist 强制项：上线前配置允许清单并回归 http 节点 |
| 批量工作流实际有人用但被误删 | 中 | D2 决策前必须完成使用量 SQL 验证并向业务确认 |
| admin 下线后遗漏外部客户端（网关/版本管理公开端点） | 中 | D3 决策需访问日志佐证；下线采取先停路由观察再删代码 |
| `events/__init__.py` 自带 Events 框架与上游事件机制漂移 | 低-中 | 列入挂点注册表长期跟踪；评估回归 Blinker 原生信号 |

## 10. 需人工确认的开放问题（执行前请回答）

1. ~~D2：批量工作流近 90 天是否有真实使用？~~ **已确认（2026-07-05）：无人使用，按方案 A 删除**（见 3 节 D2 决策记录）
2. ~~D3：`/gaia/proxy/*`、`/gaia/forward/proxy/*`、app-version 公开端点是否有外部客户端依赖？~~ **已确认（2026-07-05）：无外部依赖，随 admin 整体下线**（见 3 节 D3 决策记录）
3. D1：是否认可"本次保持自建额度体系、不接上游 quota v3"？
4. 合并窗口与停机预算：`flask db upgrade` + backfill 需要停机窗口，能接受多久？
5. SECRET_KEY 轮换（导致全员重新登录）安排在哪个窗口？——runbook 已交付（`docs/dify-plus/SECRET_KEY轮换runbook.md`，P5 任务 11.1），具体窗口由部署环境运维按 runbook 排期执行
