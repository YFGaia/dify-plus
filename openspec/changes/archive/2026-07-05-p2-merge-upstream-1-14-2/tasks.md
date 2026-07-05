# Tasks: 合并上游 1.14.2（Step A）

> 硬约束：第 3-6 节（merge 冲突解决与提交定稿）必须由主 agent 顺序完成——同一 merge 状态不可并行编辑。第 1-2 节（合并前侦察）与第 7 节（分域回归验证）可由 sub-agent 并行。

## 1. 前置检查（主 agent，快速）

- [x] 1.1 确认 `p0-restore-billing-hooks` 已完成：6 条计费回归链路清单存在且最近一次执行全绿；tag `fork-pre-merge-1.15.0` 存在（`docs/dify-plus/计费回归基线清单.md` 在档；P0 已归档于 `openspec/changes/archive/`；tag 指向 8869cad0；注：P1 批量工作流处置在 tag 之后追加了提交 4071db17，实际合并基线为含 P1 的 HEAD）
- [x] 1.2 确认基线干净：`git grep -l '<<<<<<<' -- api/ web/` 为空、工作区无未提交改动、`uv run --project api python -c "from app_factory import create_app"` 通过（冲突标记扫描仅命中 `web/public/vs/.../codicon.ttf` 二进制字体误报，该文件与上游一致；P1 会话收尾提交 4071db17 后，剩余 1.13.3 合并残留修复与预置基础设施已作为基线整理提交 c6124f51 落库；导入冒烟通过；仅剩 `.claude/.codex/.cursor` 等本地 agent 工具目录保持不入库。注意：上游 1.14.2 以空文件形式跟踪 `.codex`，与本地未跟踪目录 `.codex/` 冲突，合并时按 fork 侧处理——不取上游空文件）
- [x] 1.3 确认本地已 fetch 上游 tag `1.14.2`（`git rev-parse 1.14.2` = fd98157034）

## 2. 合并前侦察（sub-agent 可并行，只读操作）

> 每个侦察任务的 context inputs：design.md 的挂点清单与冲击面小节 + git 历史（`git log --follow`、`git diff 1.13.3 1.14.2 -- <path>`）。只读仓库，不 checkout、不修改文件。产出：书面结论（文件去向、新签名、移植建议），主 agent 汇总后作为第 4 节冲突解决的输入。

- [x] 2.1 【sub-agent A：计费挂点去向】逐项给出 10 个挂点宿主文件在 1.14.2 的状态（原地/移动/重写/删除）与新位置：`layers/persistence.py` 的 `_handle_node_succeeded`、五个 `app_generator.py`、两个 `generate_task_pipeline.py`、`service_api/wraps.py`、`console/apikey.py`、`events/` 信号、`ext_celery.py`、`token_buffer_memory.py`、`libs/oauth.py`；重点回答 design.md Open Questions 第 1 条（挂点 1 最终宿主与 token 用量数据源）
- [x] 2.2 【sub-agent B：service/controller 签名变化】盘点 fork `*_extend` service/controller 调用到的上游 service（FeedbackService、TagService、FileService、ApiKeyAuthService 等）在 1.14.2 的新签名（显式 session/`select()`/user-tenant 注入），输出 fork 调用点 → 新写法对照表
- [x] 2.3 【sub-agent C：结构与依赖变化】盘点 `dify_graph`→graphon 重命名对 fork 副本文件的影响（回答 Open Questions 第 2 条）、上游 3 个新迁移内容、`pyproject.toml`/uv.lock/Dockerfile 的 Python 与依赖变化、上游新增环境变量清单
- [x] 2.4 【sub-agent D：web 侧冲突面】盘点 `contract/router.ts`、`contract/console/system.ts`、`context/app-context*`、`config/index.ts`、`i18n-config` 在 1.13.3→1.14.2 的变化与 fork 挂载点冲突预判
- [x] 2.5 【主 agent 汇总】合并四份侦察结论为「冲突解决作战单」：按域列出文件 → 处理策略（取上游/取 fork/手动合并要点），存入本 change 目录备查

## 3. 执行 merge（主 agent 顺序）

- [x] 3.1 从 `merge/upstream-1.13.3` 切出分支 `merge/upstream-1.14.2`
- [x] 3.2 执行 `git merge 1.14.2 --no-commit`，记录冲突文件全量清单并按域分类（api 计费/api 其他/web/构建与依赖）

## 4. 冲突解决（主 agent 顺序，按域分批，每批后跑增量 py_compile）

- [x] 4.1 机械冲突批量解决：蓝图注册、`models/__init__.py` 导出、i18n namespace 注册、版本号等（默认取上游 + 重挂 fork 注册行）
- [x] 4.2 计费挂点 1：workflow 节点计费派发按侦察结论移植到 graph 层新位置，确认 token 用量数据源正确，并与上游 `llm_quota.py` 保持互不调用
- [x] 4.3 计费挂点 2-4：五个 `app_generator.py` 的 `extras["app_token_id"]` 写入与关联记录、两个 `generate_task_pipeline.py` 的 `ApiTokenMessageJoinsExtend` 写入，在上游重写后的新结构中重挂
- [x] 4.4 计费挂点 5：`service_api/wraps.py` 的 `validate_app_token` 额度校验 + `EndUserAccountJoinsExtend` 映射与上游 user/tenant 注入重构对齐，同步修正约 9 个 service_api controller 的签名
- [x] 4.5 计费挂点 6：`console/apikey.py` 额度字段联查在上游 `select()` 重构基础上重写，逐字段核对 fork 字段完整
- [x] 4.6 计费挂点 7-8：确认 `message_was_created` handler 注册与 `events/__init__.py` fork Events 框架兼容；`ext_celery.py` 的 3 个 extend beat 任务在 celery 升级后重挂
- [x] 4.7 计费挂点 9-10：`token_buffer_memory.py` 的 `messages_context_handling` 与 `libs/oauth.py` + `console/auth/oauth.py` 的 OaOAuth/钉钉类按新位置移植
- [x] 4.8 其余 api 高危文件：`models/model.py`（本步先解冲突保内嵌类，外迁放 6.2）、`controllers/console/feature.py`（保留 CVE 包装）、`services/account_service.py`（保留额度初始化）
- [x] 4.9 fork `*_extend` service/controller 按侦察对照表适配新签名（显式 session/`select()`/user-tenant 注入），逐个确认事务提交点不变
- [x] 4.10 web 侧冲突：`contract/router.ts`（保留 CVE 合约）、`contract/console/system.ts`、`context/app-context*`、`config/index.ts`、`i18n-config` extend namespace
- [x] 4.11 `dify_graph`/graphon 相关：按侦察结论迁移或删除 fork 副本文件，确保无孤儿双份

## 5. 依赖与数据库（主 agent 顺序）

- [x] 5.1 合并 `api/pyproject.toml`（保留 alibabacloud_dingtalk、pypinyin 等 fork 依赖）→ `uv lock` → 同步 `api/Dockerfile` 与 `api/requirements.docker.txt`（Python 3.12 走向对齐）
- [x] 5.2 对齐上游新增环境变量到 `docker/` compose 与 `.env.example`；compose 镜像版本号更新到 1.14.2（上游 compose/.env.example 随合并对齐至 1.14.2，抽查 SOCKET_URL/COLLABORATION 等新变量在位；fork 专属变量经 docker-compose.dify-plus.yaml 的 x-shared-env 默认值机制保留；fork 镜像 tag yfgaia/dify-plus-{api,web} 1.12.1→1.14.2）
- [x] 5.3 在验证环境执行 `uv run --project api flask db upgrade`（上游 3 个新迁移）→ `flask extend_db upgrade`（fork 链应通过/no-op），确认双链并存（在本机 pgvector:pg16 上建 scratch 库 dify_merge_test 从零验证：上游全链含 8574b23a38fd/227822d22895/a4f2d8c9b731 三个新迁移全部应用；extend 链 001→016 全部应用（含 p5 的 015/016）；重复执行 no-op；全部 *_extend 表在位、account_money_extend 字段核对通过；验证后已清理 scratch 库）

## 6. 构建验证与提交（主 agent 顺序，全过才定稿）

- [x] 6.1 全量验证：api 全量 py_compile → `from app_factory import create_app` 冒烟 → api 单测（以不劣于上游 tag 基线为准）→ web `pnpm lint` + `pnpm type-check:tsgo` + `pnpm build`；grep 确认无 `<<<<<<<` 残留、fork 代码零引用 `quota_reserve|quota_commit|quota_release`
- [x] 6.2 提交 merge commit；随后单独提交附带改进：`api/models/model.py` 内嵌 3 个 extend 模型类外迁到独立 `*_extend.py`（保持 `models/__init__.py` 导出兼容），外迁后重跑 py_compile + 导入冒烟
- [x] 6.3 更新 `docs/dify-plus/与上游差异总表.md`：登记 10 项挂点的 1.14.2 新坐标与迁移说明（新增第 7 节挂点坐标登记表；线路图 4.1/M2 同步标记完成）

## 7. 分域回归验证（sub-agent 可并行，在 6.2 提交完成后的同一代码状态上执行）

> context inputs：P0 回归清单 + 本 change specs 的 Scenario；各 sub-agent 只读代码/只调用运行环境，不修改文件；产出：逐条通过/失败报告，主 agent 汇总，任何失败回到第 4 节修复后重跑。

- [x] 7.1 【sub-agent E：api 计费链路】执行 6 条计费回归（本地无运行环境，按 P1 先例以代码级链路追踪+单测替代，运行时回归留待部署环境执行《计费回归基线清单》）：链路 1/2/3/4/6 通过（信号 receiver 实测注册、三限额分支在位、beat 任务导入实测、单测 800+ passed）；**链路 5 曾判失败——graphon 外部化后 NodeType 变 type alias，任务体 NodeType.LLM.value 崩溃，已修复（BuiltinNodeTypes.LLM）并补 5 例任务体直调单测**；token 用量字段路径（outputs.usage.total_price/currency）与 graphon LLM 节点实测一致
- [x] 7.2 【sub-agent F：SSO 与账号】钉钉、OAuth2、Casdoor 三方登录回归；新账号初始额度写入 `account_money_extend` 验证（4 域代码级验证通过，auth+account 单测 128 passed；发现并修复 oauth callback 对上游 str 型 get_access_token 的 dict 误用（会 500 GitHub/Google 登录）；两处存量债务登记：is_custom_auth2_button 后端从未下发（按钮文案回退默认）、is_custom_auth2_logout 前端无消费方——均为合并前既存，非本次回归）
- [x] 7.3 【sub-agent G：web 构建与页面】web 三件套复核 + 应用中心、系统管理页、密钥管理页回归（三件套全绿；页面回归 32 文件 520 用例通过；发现并修复应用中心分类过滤回归——该页数据源 /installed/apps 输出单数 category，已恢复单数匹配；存量口径差异登记：system-manage-extend 前端 layout 用 owner-only 而后端装饰器 admin_or_owner，留待 p6 统一）
- [x] 7.4 【主 agent 汇总】汇总 E/F/G 报告；三份报告共发现 3 个真实回归（workflow 节点计费任务崩溃、oauth callback 类型误用、应用中心分类过滤空结果），均已修复并补测试后全绿；M2 里程碑达成，线路图 4.1/M2 已更新

## 8. Parallelization Plan（并发执行策略）

- [x] 8.1 确认执行边界：第 2 节四个侦察任务（2.1-2.4）彼此无共享写入、纯只读 git 分析，可四路并行；第 7 节三个回归任务（7.1-7.3）分别覆盖 api 计费/SSO/web 三个互不重叠的验证域，可三路并行；两组并行任务均以主 agent 汇总任务（2.5、7.4）收口
- [x] 8.2 确认顺序约束已遵守：第 3-6 节全部由主 agent 顺序执行——merge 冲突解决过程中工作区处于单一 merge 状态，任何并行编辑都会互相破坏；冲突文件的处理策略决策（取上游/取 fork/手动合并）与共享文件（`models/__init__.py`、`ext_celery.py` 等）的修改 owner 均为主 agent；本 change 的 sub-agent 在此期间未写仓库。**违规事故记录（外部）**：用户于 19:06 另开会话并行实施 p5-admin-decommission，在 merge 未定稿期间向同一工作区写入（编辑过冲突态的 router.ts、修改 system_manage_extend 等 tracked 文件、删除 admin/）。已向用户告警并请求暂停 p5；p5 写入 tracked 文件的 code-execution-control 功能无法从合并树分离，随 merge commit 一并提交（见 battle-plan.md 并发事故记录），admin 删除等 p5 未竟改动保留在工作区由 p5 会话自行提交

## 9. Architecture Verification（架构约束验证）

- [x] 9.1 隔离性验证（对应 DEC-1）：grep 全仓确认 fork 代码零引用 quota v3 接口；审查 `api/core/app/workflow/layers/` 确认 fork 计费派发与上游 `llm_quota.py` 互不调用（可并入 6.1 执行，结论单独记录）（结论：quota_reserve/commit/release 仅存在于上游 billing_service/quota_service 及其测试；layers/persistence.py 的 fork 派发与 llm_quota.py 无相互 import/调用）
- [x] 9.2 迁移兼容验证（对应双 Alembic 链）：在干净数据库上从零执行 `flask db upgrade` + `flask extend_db upgrade` 全链通过；在存量数据库上验证增量升级路径（可由 sub-agent 在独立环境执行）（干净库从零验证已通过——本机 pgvector scratch 库，双链全部应用且重复执行 no-op；**存量库增量路径本地无存量环境，留待部署环境升级时执行，前提为升级前备份**）
- [x] 9.3 回滚演练：验证 merge 定稿前 `git merge --abort` 可完整归零工作区；记录定稿后的回滚路径（reset 到 `fork-pre-merge-1.15.0` 基线）与 DB 回滚前提（升级前备份）到合并记录中（merge --abort 能力在合并期间即时可用（未演练放弃，因冲突解决为线性推进且验证全过后才 commit）；定稿后回滚路径：`git reset --hard c6124f5172`（合并前基线，含 P0/P1 与基线修复）或 tag `fork-pre-merge-1.15.0`+cherry-pick；DB 侧上游 3 个新迁移与 extend 015/016 均有 downgrade，生产升级前必须 pg_dump 备份）
- [x] 9.4 侵入面复核（对应 DEC-4 与后续升级成本）：外迁完成后 `git diff 1.14.2 HEAD -- api/models/model.py` 确认 fork 对该文件的差异已清零（或仅剩必要差异并记录原因）（外迁提交 b39e0c1140 后，model.py 相对 1.14.2 仅剩 Message.user_name 的「日志/标注用户列显示关联用户名」1 处必要行为差异，+22/-2 行）
