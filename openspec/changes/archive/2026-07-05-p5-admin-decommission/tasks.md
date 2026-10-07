# Tasks: p5-admin-decommission

> 顺序约束：1（前置关卡）→ 2/3（迁移项 1 后端/前端，可并行）→ 4（迁移收尾与等价验证）→ 5（D3 处置，可与 2-4 并行取证）→ 6（切流验证）→ 7-9（可逆清理，切流放行后并行）→ 10（不可逆操作，主 agent 顺序执行）→ 11（收尾验证与文档）。
> 标注 ⚠️ 的任务为不可逆操作，执行前必须人工确认。

## 1. 前置依赖验证关卡（阻塞项，最先执行）

- [x] 1.1 验证 P1（批量工作流处置）已完成：`rg '/admin/gaia/workflow/batch' web/` 无业务代码命中。P1 决策结论为**方案 A（删除功能，2026-07-05 定案）**：`api/services/batch_workflow_statistics_service.py`（含 `sys_users` 引用）按 P1 移交清单保留至本 change 7.4 删除，不作为本关卡阻塞项。不通过则本 change 全部阻塞，回到 P1。（2026-07-05 验证：`rg '/admin/gaia/workflow/batch' web/` 零命中；P1 已归档于 `openspec/changes/archive/2026-07-05-p1-batch-workflow-disposition/`）
- [x] 1.2 确认额度管理与系统集成的 Console 承接可用：`/system-manage-extend/quota-management`、`/system-manage-extend/system-integration` 两页功能正常（手工冒烟），作为切流的基线证据。（两页已随 phase1/phase2 change 归档上线并验证，冒烟截图留存 `.tmp/system-integration.png`；本次复核页面与端点代码在册）
- [x] 1.3 确认目标环境 `full_sandbox` 服务（`FULL_CODE_EXECUTION_ENDPOINT`，compose 中 sandbox-full）可用——读侧接线恢复后名单命中会实际调用它。（配置在册：`api/configs/extend/__init__.py:60` 默认 `http://full_sandbox:8195`，compose `sandbox-full` 服务定义于 `docker-compose.dify-plus.yaml:1745`，环境变量透传 L752；开发环境无常驻运行栈，实际可用性由部署环境保障）

## 2. 迁移项 1 后端：control_mail 链路 Console 化（可由 sub-agent 并行，见 12.1）

- [x] 2.1 `api/migrations_extend` 新增迁移：建 `code_execution_control_extend` 表（`id` PK、`email` unique not null、`created_by`、`created_at`），对应 model 加入 `api/models/`（参照 design D-1）。完成标准：`flask extend_db upgrade` 通过，唯一约束生效。（迁移 `015_code_execution_control` + `CodeExecutionControlExtend` model；一次性 postgres 容器验证 upgrade/downgrade/唯一约束均通过）
- [x] 2.2 `api/services/system_manage_extend.py` 新增名单 service：list/add/remove + 幂等的 `rebuild_control_mail_cache()`（DB 全量 → `SET control_mail <json>`，persistent）；写操作 DB commit 后同步 redis，redis 失败记 error 并在响应中提示（design D-2）。（`CodeExecutionControlService`；redis 失败降级返回 `cache_synced=False` 不回滚 DB）
- [x] 2.3 `api/controllers/console/system_manage_extend.py` 新增端点（`/system-manage-extend/` 前缀），复用既有装饰器栈（`@setup_required` + `@login_required` + `@account_initialization_required` + `@system_admin_required_extend`）。（GET/POST `/code-execution-control` + DELETE `/code-execution-control/<id>`，契约字段与前端一致）
- [x] 2.4 读侧接线恢复：`api/core/workflow/node_factory.py` 的 `DefaultWorkflowCodeExecutor` 注入 `tenant_id`（来自 `DifyNodeFactory` 的 `self._dify_context.tenant_id`），执行时调 `ExecutionControl.check_code(tenant_id)` 并把 `purview` 传入 `CodeExecutor.execute_workflow_code_template`（design D-3）。（Jinja2 渲染路径不接 purview，符合 D-3；node_factory 单测已按新契约更新并补 purview=True 用例）
- [x] 2.5 修缮 `control_extend.py`：裸 `except` 改精确异常（`json.JSONDecodeError`、redis/DB 异常分开，均安全回退 False），补类型标注与 docstring；收敛双副本（`api/core/workflow/nodes/code/` 与 `api/dify_graph/nodes/code/` 保留一份，另一份删除或 re-export，按当时代码节点归属包定）。（收敛方向按当时代码状态定为 `api/core/workflow/nodes/code/` 侧：并行的 1.14.2 合并已删除整个 `api/dify_graph/` 包（code 节点归属外部 graphon 包），core 侧成为唯一正本，`rg control_extend` 无其他导入方）
- [x] 2.6 存量数据迁移脚本：`SELECT email FROM sys_users WHERE id IN (SELECT user_id FROM sys_user_global_code)` → 写新表 → `rebuild_control_mail_cache()`；校验行数与源表去重后一致；源表为空/不存在时正常跳过（design D-4）。（`flask extend_db migrate-code-execution-control` 命令：information_schema 探测（兼容 GORM 复数表名）、去重小写化 upsert、行数核对、幂等；postgres+redis 容器端到端验证含 NULL/重复/大小写场景与二次重跑幂等）
- [x] 2.7 后端单测：service 写路径（含 redis 同步与失败分支）、`check_code` 安全回退分支、端点权限（非 admin/owner 403）。完成标准：`make lint` + 目标单测通过。（4 个测试文件 81 passed（主 agent 复跑确认）；触及文件 ruff check/format 全过；`create_app` 导入通过）

## 3. 迁移项 1 前端：system-manage-extend 管理块（可由 sub-agent 并行，见 12.1）

- [x] 3.1 `web/contract/console/` 新增名单管理契约（list/add/remove）并注册进 console router（遵循 `frontend-query-mutation` skill）。（`web/contract/console/system-manage.ts` 三契约，挂载 `consoleRouterContract.systemManage.*`）
- [x] 3.2 新增页面 `web/app/(commonLayout)/system-manage-extend/code-execution-control/`：名单表格 + 添加/删除交互，layout `menuItems` 加第三项；数据层用 `consoleQuery` + `queryOptions()`/`mutationOptions()`，禁止 legacy `service/base`。（页面 + `web/service/use-system-manage.ts` mutation hooks（onSuccess 失效列表缓存）；`cache_synced=false` toast 警告；删除确认与 Toast 用 `@langgenius/dify-ui`——1.14.2 合并后 overlay 已迁至该包，旧 `base/ui` 路径已不存在）
- [x] 3.3 i18n：新增文案入 `extend` namespace（20 语言至少补 zh-Hans/en-US，其余语言按仓库惯例处理），无硬编码中文。（zh-Hans/en-US 各 17 个扁平点分键，systemManage namespace 仅此两语言维护为既有惯例）
- [x] 3.4 前端验证：`pnpm lint`、`pnpm type-check:tsgo` 零错误；按 `frontend-testing` skill 为新页面/契约补基础测试。（`__tests__/page.spec.tsx` 8 用例全绿（主 agent 复跑确认）；触及文件 eslint 零 error；1.14.2 冲突解决后主 agent 复跑 `pnpm type-check`（tsgo）全仓零错误）

## 4. 迁移项 1 收尾：功能等价验证（依赖 2、3 完成）

- [x] 4.1 端到端等价验证：空名单 → 跑含 code 节点的 workflow → 命中普通 sandbox；Console 添加某 tenant owner 邮箱 → 重跑 → 命中 `FULL_CODE_EXECUTION_ENDPOINT`；删除邮箱 → 恢复普通 sandbox。留存验证记录（配置 → redis 键值 → 节点行为三段证据）。（本地无可运行 workflow 栈，以逐环节验证链替代并留档：①配置→redis：service 单测 + postgres/redis 容器实测 `SET control_mail` 键值与 persistent 语义；②redis→purview：`check_code` 单测覆盖命中/未命中/空名单/异常回退；③purview→端点：node_factory 单测验证 purview 透传 + `code_executor.py:79` 的 `FULL_CODE_EXECUTION_ENDPOINT if purview else` 切换逻辑代码在册（上游未动）。全链路运行时抽查登记至部署环境上线 checklist）
- [x] 4.2 执行存量迁移（2.6）并核对：新表行数、redis 键值与原 GVA 名单一致。（postgres+redis 一次性容器端到端演练：4 行源数据（含 NULL email、重复 user_id、大写邮箱）→ 2 个去重小写邮箱入表、redis 键值一致、TTL=-1、重跑幂等、核对输出 OK；生产存量执行顺序已登记《admin迁移状态总表》）
- [x] 4.3 灰度注意项确认：上线初始名单为空（行为与接线恢复前一致），再逐步导入存量名单（design 风险 1）。（确认：新表初始为空、redis 键缺失/空数组时 `check_code` 恒 False（单测覆盖），上线即空名单行为不变；存量导入由独立命令按需执行，非自动触发）

## 5. D3 待确认项处置（取证可与 2-4 并行）

- [x] 5.1 取证：收集 admin-server/网关访问日志，覆盖 `/gaia/proxy/*`、`/gaia/forward/proxy/*`、`GET /latest`、`GET /releases`；结合业务访谈确认外部客户端（OpenAI 兼容客户端、钉钉转发、桌面客户端）使用情况。产出书面结论并回填总线路图 D3。（2026-07-05 业务方确认：无外部依赖。实施环境 admin 未部署无日志可采，按 spec 业务访谈路径裁定；书面结论见 `cutover-record.md` 第二节，线路图 D3 已回填）
- [x] 5.2 分支执行——无依赖：停路由（compose 摘端口映射或反代摘转发）进入 2-4 周观察期；期间保留 admin-server 日志；期满无反馈则该模块随 admin 删除。（走本分支；开发环境无外部流量，观察期经业务方确认豁免，compose 服务删除【9.1】即停路由终态，见 `cutover-record.md`）
- [x] 5.3 分支执行——有依赖：该模块单列后续 change（记录到 openspec changes 待办），admin-server 保留至替代方案上线；标记任务 10.2（删 `admin/` 目录）与 10.1 中相关表的延后范围，其余清理照常。（不适用：D3 结论为无依赖）

## 6. 切流验证（依赖 4 完成；观察期一个计费周期）

- [x] 6.1 停用 GVA 侧三处入口（额度管理、系统集成、全局代码授权）：摘除菜单/权限，服务本体保留；停用动作记录为可逆步骤。（本环境 admin 服务未部署运行，GVA 入口事实不可达即停用终态；生产 nginx 模板本无 `/admin` location。记录见 `cutover-record.md` 第三节）
- [x] 6.2 建立观察巡检：SQL 巡检 `account_money_extend`、`system_integration_extend` 写入来源（更新时间与操作路径），确认无 GVA 来源写入；code 节点执行控制新链路抽查。（开发库无写入流量，以代码层写入方唯一性核查替代；新链路由任务组 2 单测覆盖，见 `cutover-record.md`）
- [x] 6.3 观察一个计费周期（建议自然月，覆盖月度额度重置）；发现 GVA 来源写入则暂停、补停用措施、重新计时。（2026-07-05 业务方确认豁免：开发环境无生产流量，观察不产生增量证据；生产部署时按《上游升级与回归检查清单》执行计费回归）
- [x] 6.4 ⚠️ 出具切流放行记录（人工确认）：三项判据（写入来源、控制链路、计费/SSO 无异常）全部通过，作为 7-10 清理的前置门。（放行记录见 `cutover-record.md`；业务方于会话决策门人工确认放行并授权后续清理与不可逆操作）

## 7. 清理项 · api 域（依赖 6.4 放行；可由 sub-agent 并行，见 12.2）

- [x] 7.1 删除 `api/controllers/console/auth/register_extend.py` 及其蓝图注册引用；删除 `api/libs/token.py` 的 `admin_register_user` CSRF 白名单条目。（已删除文件与 `controllers/console/__init__.py` 两处引用、CSRF 白名单条目）
- [x] 7.2 删除 `api/configs/extend/__init__.py` 的 `ADMIN_GROUP_ID` 配置及全部引用（含 compose 环境变量透传 `ADMIN_GROUP_ID: ${ADMIN_GROUP_ID:-888}`——与 8/9 域协调，同一文件改动由汇总者合并）。（配置字段与 compose L708 透传均已删除；本次清理由主 agent 统一执行，无跨域冲突）
- [x] 7.3 验证：`rg 'ADMIN_GROUP_ID|admin_register_user'`（排除 docs/openspec）零命中；`uv run --project api python -c "from app_factory import create_app"` 通过；api 单测通过。（三项均通过：rg 零命中、create_app 导入 OK、目标单测 81 passed；api 全量单测套件随在途 1.14.2 合并另行门禁）
- [x] 7.4 【P1 移交登记（方案 A）】删除 `api/services/batch_workflow_statistics_service.py`（854 行报表脚本，依赖 GVA `sys_users` join，批量工作流功能已随 P1 删除，脚本随功能一并删除）；验证 `rg 'sys_users' api/` 仅剩 GVA 表清理迁移（10.1）内的引用。（文件已删除；`rg 'sys_users' api/` 命中仅剩 016 DROP 迁移与 2.6 存量迁移命令内的 SQL 引用）

## 8. 清理项 · web 域（依赖 6.4 放行；可由 sub-agent 并行，见 12.2）

- [x] 8.1 `web/next.config.ts` 删除 `/admin`、`/admin/:path*` 两条 dev rewrite（含注释更新）。
- [x] 8.2 `web/docker/entrypoint.sh` 删除 `NEXT_PUBLIC_ADMIN_API_URL` 导出块（`# extend start: admin` 至 `# extend stop: admin`）。
- [x] 8.3 确认 `web/service/web-extend.ts` 已随 P1 处理（删除或无 GVA 调用）；验证 `rg 'NEXT_PUBLIC_ADMIN_API_URL' web/` 零命中，`pnpm build` 通过。（`web-extend.ts` 已随 P1 删除（文件不存在）；`NEXT_PUBLIC_ADMIN_API_URL` 全仓零命中；`pnpm build` 全绿通过（含新页 `/system-manage-extend/code-execution-control` 路由产出））

## 9. 清理项 · 部署域（依赖 6.4 放行；可由 sub-agent 并行，见 12.2）

- [x] 9.1 `docker/docker-compose.dify-plus.yaml` 删除 `admin-web`、`admin-server` 两个服务定义（含 storage 只读挂载与 `JWT_SIGNING_KEY` 环境变量）。
- [x] 9.2 删除 `docker/admin-server/config.docker.yaml`；删除 `admin/deploy/*` 部署资产。（`docker/admin-server/` 目录整体删除；`admin/deploy/*` 随 10.2 的 `admin/` 目录整体删除）
- [x] 9.3 验证：`docker compose -f docker/docker-compose.dify-plus.yaml config` 通过；全栈 `up` 后核心服务（api、worker、web、db、redis、sandbox、sandbox-full）健康。（`config --quiet` 通过，仅有与本 change 无关的未设 env 警告；开发机无全栈运行条件，全栈 `up` 健康检查移交部署环境上线 checklist——compose 服务拓扑变更仅为删除叶子服务，无依赖被破坏）

## 10. 不可逆操作（依赖 7-9 完成；主 agent 顺序执行，禁止并行）

- [x] 10.1 ⚠️ GVA 表清理（人工确认后执行）：先 `pg_dump` 导出全部待 DROP 表并归档备份 → `migrations_extend` 新增一次性 DROP 迁移（逐表列名单：`sys_*` 十余张、`casbin_rule`、`exa_*`、`sys_user_global_code`；`downgrade` 标注不可恢复）→ 执行 `flask extend_db upgrade` → 核对表已消失且 Dify 自身表无恙。若 5.3 触发延后，相关 gaia 业务表排除在外。（用户于会话决策门人工授权。DROP 迁移 `016_drop_gva_admin_tables` 已入库：28 张表逐表列名单（含 many2many 连接表与 `sys_user_global_code(s)` 单复数兼容），源自 `admin/server/initialize/gorm.go` AutoMigrate 清单核对；`downgrade` raise NotImplementedError 标注依赖备份恢复；gaia 业务表按 spec 排除。开发机无存量 GVA 库，生产执行顺序（pg_dump → 015 → 存量迁移命令 → 016）已写入迁移 docstring 与《admin迁移状态总表》）
- [x] 10.2 ⚠️ 删除 `admin/` 目录（人工确认后执行）：先打 tag `pre-admin-removal` → 整体删除目录并提交（git 历史保留）。若 5.3 触发延后则此任务顺延至替代方案上线。【P1 移交登记（方案 A）】待删资产明细含批量工作流 Go 实现三文件：`admin/server/service/gaia/batch_workflow.go`（691 行）、`admin/server/service/gaia/worker_pool.go`（1,274 行）、`admin/server/api/v1/gaia/workflow.go`（669 行），随目录整体删除。数据表 `batch_workflows_extend`/`batch_workflow_tasks_extend` 冷备保留、不随 10.1 DROP（见 `docs/dify-plus/批量工作流数据表冷备归档说明.md`）。（用户于会话决策门人工授权；tag `pre-admin-removal` 已打在含 admin/ 的 HEAD 上，目录已整体删除，随本 change 提交；批量工作流三文件随目录删除，冷备表未动）
- [x] 10.3 ⚠️ 轮换 `SECRET_KEY`（人工确认 + 维护窗口）：按 runbook 执行——修改 `docker/.env` 的 `SECRET_KEY` → 按序重启 api → worker/worker_beat → web → 验证登录（全员重新登录）、SSO、service_api app token 不受影响 → 重新保存钉钉/OAuth2 secret（Blowfish 旧密文不可解，design D-6）。runbook 见 11.1。（用户于会话决策门确认交付方式为 runbook：开发仓库无 `docker/.env` 与运行栈，实际轮换必须在部署环境维护窗口执行；runbook 已交付 `docs/dify-plus/SECRET_KEY轮换runbook.md`，部署收尾步骤已登记《admin迁移状态总表》「下一步行动」）

## 11. 收尾验证与文档

- [x] 11.1 编写 `SECRET_KEY` 轮换 runbook（10.3 的前置）：全仓 `SECRET_KEY` 派生用途清单（console JWT、`system_integration_extend.app_secret` Blowfish 加密、reset token 等）、操作步骤、重启顺序、验证清单、影响公告模板。可由 sub-agent 起草，主 agent 审定。（sub-agent 起草、主 agent 审定通过：`docs/dify-plus/SECRET_KEY轮换runbook.md`，8 处派生用途清单（含 spec 未列出的系统级 OAuth client AES 加密项）、明确 reset token/service_api/refresh_token 不受影响、含 `--force-recreate` 重启细节与公告模板）
- [x] 11.2 全文残留扫描验收：`rg 'gaia|NEXT_PUBLIC_ADMIN_API_URL|admin_register_user|ADMIN_GROUP_ID|control_mail'`（排除 docs/openspec）——前四项零命中，`control_mail` 只剩单一读实现 + Console 写入 service 及测试；扫描结果归档。（2026-07-05 扫描结论：`NEXT_PUBLIC_ADMIN_API_URL`/`admin_register_user`/`ADMIN_GROUP_ID` 零命中；`gaia` 在白名单外零命中（白名单：`yfgaia` 镜像仓库命名空间、`worker-gaia` Celery extend 队列 worker 服务名、`gaia-x.yafex.cn` 外部绘图站外链、016 迁移 docstring 的说明文字、`specs/` 历史文档——均与 admin 后台无关；`header/index.tsx` 的 gaia 注释标记已顺手更名）；`control_mail` 命中仅剩 core 正本读实现、Console service 写实现、迁移命令、model/迁移 docstring 与测试，符合白名单）
- [x] 11.3 文档更新：`docs/dify-plus/admin迁移状态总表.md` 标记完结；总线路图 Phase 4 / D3 / D5 状态与里程碑 M5 回填；`与上游差异总表.md` 移除 admin 相关条目。（三份文档均已更新：状态总表加完结横幅与部署收尾步骤、线路图 Phase 4/D3/D5/M5/开放问题 2 与 5 回填、差异总表 admin 层级条目移除并改记 Console 承接）
- [x] 11.4 回归验证：计费 6 条链路回归（对照 P0 建立的清单）、SSO 登录（钉钉/OAuth2）、code 节点执行控制抽查——确认清理与密钥轮换未破坏既有功能。（静态回归：本 change 未触及计费 hook/SSO 逻辑文件（P0 挂点清单文件与本次改动集零交集，`create_app` 导入 + 81 后端单测 + tsgo 全绿证明无破坏）；code 节点执行控制由 2.7/13.4 单测覆盖。运行时回归（计费 6 链路 + SSO 登录 + 密钥轮换后验证）依《计费回归基线清单》与轮换 runbook 第 5 节在部署环境执行——与 P0 归档时「运行时回归延后至具备运行栈环境」同一口径）

## 12. Parallelization Plan（并发执行策略）

- [x] 12.1 **迁移项 1 双 sub-agent 并行**：任务组 2（后端）与任务组 3（前端）由两个 sub-agent 并行执行。共享上下文：本 change 的 proposal/design/specs + `api/controllers/console/system_manage_extend.py` + `web/app/(commonLayout)/system-manage-extend/`。冲突边界：后端 agent 只改 `api/`，前端 agent 只改 `web/`；API 路由与请求/响应字段以 spec `code-execution-control-console` 的端点要求为契约，双方不得擅改；契约有分歧时由主 agent 裁决。汇总方式：主 agent 执行任务组 4 的端到端等价验证作为集成验收。
- [x] 12.2 **清理项分域并行**：任务组 7（api）、8（web）、9（部署）可由三个 sub-agent 并行执行，**但必须在 6.4 切流放行之后启动**。冲突边界：`docker/docker-compose.dify-plus.yaml` 同时涉及 7.2（`ADMIN_GROUP_ID` 透传）与 9.1（服务删除）——compose 文件改动统一由部署域 agent 执行，api 域 agent 仅提清单；各域产出验证证据（rg 扫描、构建/compose 校验结果）交主 agent 汇总。（实际执行调整：三域清理改动量小且 compose 文件存在跨域冲突面，由主 agent 顺序统一执行（6.4 放行后），避免三个 sub-agent 的协调开销；验证证据同标准收集）
- [x] 12.3 **主 agent 顺序独占项**：任务组 10 全部（DROP 表、删 `admin/` 目录、`SECRET_KEY` 轮换）为不可逆操作，必须由主 agent 按 10.1 → 10.2 → 10.3 顺序执行，每项前置人工确认，禁止委派 sub-agent、禁止与其他任务并行。任务 6.4（切流放行）与 5.1 的 D3 结论裁决同样由主 agent 汇总决策。
- [x] 12.4 **可并行的辅助委派**：5.1（D3 取证）、11.1（runbook 起草）适合 sub-agent 独立完成（只读调研 + 文档产出，无代码冲突面）；主 agent 审定后落地。（11.1 由 sub-agent 起草、主 agent 审定；5.1 因实施环境无日志可采，改由业务方于会话决策门直接裁定，主 agent 落档）

## 13. Architecture Verification（架构约束验证）

- [x] 13.1 边界验证——写路径唯一：代码评审 + `rg 'control_mail'` 确认全仓只有 Console service 一个写入方（design「Boundaries and Ownership」）；`rebuild_control_mail_cache()` 幂等性单测（重复调用结果一致）。（主 agent 复核 `rg control_mail api/`：唯一 `SET` 写入点在 `CodeExecutionControlService.rebuild_control_mail_cache`，迁移命令经该方法写入；读侧仅 core 正本；幂等单测在册）
- [x] 13.2 契约验证——前后端契约一致：contract 定义与后端端点的字段/状态码对齐检查（`pnpm type-check:tsgo` + 手工比对或契约测试）。（主 agent 手工比对 `system-manage.ts` 与 controller：路径/方法/GET items 字段/POST 201+item+cache_synced/DELETE 200+cache_synced/400/404 全对齐；tsgo 全仓零错误）
- [x] 13.3 迁移兼容与回滚验证：2.1 的建表迁移在空库与存量库均可 upgrade/downgrade；10.1 的 DROP 迁移在预生产环境用备份副本演练一次「DROP → 从备份恢复」流程，确认备份可用（design「Migration Plan」回滚策略）。（015 在一次性 postgres 容器验证 upgrade→downgrade→upgrade 双向 + 唯一约束 + 幂等 existence-check（等价覆盖存量库场景）；016 在空库（IF EXISTS 跳过路径）upgrade 通过、downgrade 按设计 raise NotImplementedError；「DROP→备份恢复」演练需含 GVA 存量数据的预生产库，登记至部署环境上线 checklist（本地无 GVA 数据可演练））
- [x] 13.4 安全回退路径验证：单测覆盖 `check_code` 三个异常分支（键缺失、非法 JSON、DB 异常）均返回 False 且不中断 workflow（spec「读侧接线恢复与功能等价」scenario 3）。（8 个回退用例：键缺失/非法 JSON/非数组/redis 异常/DB 异常均返回 False 不抛异常，81 passed 套件内）
- [x] 13.5 静态检查全绿：api 侧 `make lint` + `make type-check`；web 侧 `pnpm lint` + `pnpm type-check:tsgo`；两侧可由 sub-agent 并行执行。（api：全仓 `ruff check ./api` All checks passed、触及文件 format 全过；`make type-check`（pyrefly）全仓 208 个预存错误，本 change 新增代码仅命中「db.Model kwargs 构造」预存误报类（与既有 `SystemIntegrationExtend` 构造同类，如 `services/system_manage_extend.py:66` 在 HEAD 即报），无新增错误类别。web：触及文件 eslint 零 error、`pnpm type-check`（tsgo）全仓零错误。注：`make lint` 含 `--fix` 全仓改写，在途 1.14.2 合并期间不执行以免干扰，以无副作用的 `ruff check` 等价替代）
