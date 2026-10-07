# 合并记录：upstream 1.16.0

> Phase 2 产出（合并规划 §3 Phase 2）。执行时间：2026-07-20。
> 合并命令：`git merge --no-ff refs/tags/1.16.0`，实际冲突 **75 个文件**（content 68 + modify/delete 7），
> 与 Phase 1 merge-tree 试算完全一致。
> 状态：**已全部解决**，无 `<<<<<<<` 残留；差异总表 4.3/4.4 代表 extend 文件全部存活
> （唯一路径变化：`account-money-extend` 自 1.15.0 起位于 `web/app/components/main-nav/components/`，
> 差异总表 4.4 的旧 header 路径待 Phase 7 更新）。

## 1. 冲突分组与决策记录

图例：〔a〕fork 专属/策略性文件；〔b〕挂点宿主/挂点波及；〔c〕一般冲突；〔m/d〕modify/delete。

### 1.1 策略性文件（a 类，5 个）——已解决

| 文件 | 决策 | 理由 |
| --- | --- | --- |
| `README.md` | ours | fork 版 README，上游内容归 `README_DIFY.md` |
| `pnpm-lock.yaml` | theirs | 锁文件取上游，Phase 5 `pnpm install` 重生成校验 |
| `oxlint-suppressions.json` | theirs | 上游生成物（ESLint→oxlint 迁移），Phase 5 跑 lint 按需再生成 |
| `api/pyproject.toml` | 双向合并 | 取上游依赖升级（graphon 0.6.0、json-repair 0.60.1、zstandard 0.25.0），保留 fork 依赖 `alibabacloud_dingtalk`、`pypinyin` |
| `web/package.json` | 双向合并 | 取上游（oxlint scripts、mediabunny、tsc type-check 等），保留 fork 依赖 serwist×3、dingtalk-jsapi、esbuild-wasm、lodash-es、@types/lodash-es；`@types/js-yaml` 跟随上游删除（js-yaml 5 自带类型） |

### 1.2 挂点宿主/挂点波及（b 类）

| 文件 | 挂点 | 决策 | 理由 |
| --- | --- | --- | --- |
| `api/controllers/console/apikey.py` | 6 | 取上游 `with_session` 新结构，fork 逻辑全部重写为注入 session：`_get_api_key_list` 的 LEFT JOIN `ApiTokenMoneyExtend` 改 `session.execute`；`_create_api_key` 在 `session.add/commit` 后建额度记录（同 session），返回合并额度字段的 `ApiKeyItem`；`put()` 加 `@with_session` 装饰器重写；`_delete_api_key` 软删并入同事务（一次 commit）；`_merge_token_with_quota_extend` 帮助函数保留 | 上游 ±131 行结构性重构（db.session 全清 + ast_grep 守卫），fork 逻辑必须换 session 传参风格 |
| `api/tests/unit_tests/controllers/console/test_apikey.py` | 6 | 取上游 raw_get/raw_post/raw_delete（inspect.unwrap + session 注入）测试骨架，fork 断言适配：list 用 `session.execute(...).all()` 返回 (token, quota) 行、execute 计数 2；create 保留请求上下文包裹、add/commit 各 2 次断言；delete 断言 execute 3 次（_get_resource/删 token/软删额度）单 commit | 跟随宿主重写 |
| `api/tests/unit_tests/controllers/console/datasets/test_datasets.py` | 6 | 取上游结构（已自动合并大部分），保留 fork 对 mock_token 显式置 None 的 6 个额度字段（`ApiKeyItem` 扩展字段 + MagicMock 自动属性过不了 pydantic 校验） | 唯一冲突 hunk 是 fork 增量 |
| `api/core/app/apps/chat/app_generator.py` | 4 | 取上游 session 化新结构（`generate()` 必填 `session:`、`_init_generate_records(..., session=session)`），fork 的 `ApiTokenMessageJoinsExtend` 联表写入原位重挂并改为 `session.add + session.commit`（复用调用方 session），不再调用内部走 `db.session` 的 `add_app_token_record_id()` | 挂点 4 重挂模式：扣费归因写入仍只执行一次（单条 insert），换注入 session 消除双会话写风险 |
| `api/core/app/apps/agent_chat/app_generator.py` | 4 | 同上 | 同上 |
| `api/core/app/apps/completion/app_generator.py` | 4 | 同上；import 合并保留上游新增 `AppModelConfig`/`Conversation`/`load_annotation_reply_config` | 同上 |
| `api/controllers/service_api/app/annotation.py` | 5 波及 | 取上游新结构（`with_session`、`dump_response`、job status 响应模型），fork `api_token` 参数在冲突 get 上重挂；同时给同文件其余 5 个 `validate_app_token` 方法补 `api_token: ApiToken \| None = None`（wraps 始终注入 kwargs，缺参会 TypeError——fork 1.15.0 遗留缺口，本次一并补齐） | 挂点 5 约定：所有被 `validate_app_token` 装饰的方法统一带 api_token 末位参数 |
| `api/controllers/service_api/app/app.py` | 5 波及 | 取上游 import（agent 模型退场、`load_annotation_reply_config`），保留 fork `ApiToken` import；3 个 get 的 api_token 参数自动合并存活 | 仅 import 冲突 |
| `api/controllers/service_api/app/audio.py` | 5 波及 | 取上游 import（`dump_response`、`AppRefService`）+ 保留 fork `ApiToken`；两个 post 的 api_token 参数存活 | 仅 import 冲突 |
| `api/controllers/service_api/app/completion.py` | 5 波及 | 取上游 `with_session` + `session=` 传 generate 的新结构，fork 的 `args["api_token"]`、`AppGenerateServiceExtend.calculate_cumulative_usage`（使用统计）原位重挂；上游新增的 conversation 预校验保留；stop 任务方法补 api_token 参数 | 挂点 5 波及最重文件之一 |
| `api/controllers/service_api/app/conversation.py` | 5 波及 | 取上游 import + 保留 fork `ApiToken`；5 个方法的 api_token 自动合并存活 | 仅 import 冲突 |
| `api/controllers/service_api/app/file.py` | 5 波及 | 同上（import 双向合并） | 仅 import 冲突 |
| `api/controllers/service_api/app/workflow.py` | 5 波及 | 两个 run 方法取上游 `with_session` + `session=` 新结构，`args["api_token"]` 原位重挂；get 详情/日志、stop 方法的 api_token 自动合并存活 | 挂点 5 波及 |

| `api/core/app/workflow/layers/persistence.py` | 1 | import 双向合并（fork `UserFrom` + 上游 retry_history）；`user_from` 构造参数、`_handle_node_succeeded` 的 Celery 计费派发均存活；3 个 runner 调用点 `user_from=` 实参核实存活 | 挂点 1，冲突仅在 import 区 |
| `api/tests/unit_tests/core/app/workflow/test_persistence_layer.py` | 1 | 取上游新增 retry history 两个测试；给 `test_retry_history_is_preserved_after_node_succeeds` 与 `test_handle_node_result_events_update_execution` 补 fork 的 monkeypatch（屏蔽计费 Celery 派发，单测无 broker） | 上游新测试也会触发 `_handle_node_succeeded` 的 fork 派发点 |
| `api/tests/unit_tests/core/app/apps/chat/test_app_generator_and_runner.py` | 4/9 | 取上游 `runner.run(..., session)` 新签名 + 保留 fork `SimpleNamespace(id="c1")`（add_messages_context 需要 conversation.id） | 双向合并一行 |
| `api/controllers/console/auth/oauth.py` | 10 | callback 取上游 `_get_redirect_target(redirect_url)` 同源回跳，fork 的 `&id_token={id_token}`（casdoor）追加其后；casdoor `access_token` fallback / dict token 解析链路全部存活（自动合并区） | 挂点 10 |
| `api/libs/oauth.py`（非冲突补做） | 10 | `OaOAuth.get_authorization_url` 补上游新增的 `redirect_url` 参数并传入 `encode_oauth_state` | 保持与上游 OAuth 基类签名一致（任务要求） |
| `api/controllers/console/app/completion.py` | 挂点波及 | import 双向合并；`@money_limit` 与上游 `@with_session` 装饰器共存（money_limit 仍在最前） | 自动合并已保留装饰器，仅 import 冲突 |
| `api/controllers/console/app/workflow.py` | 挂点波及 | 同上 | 同上 |

### 1.3 一般冲突（c 类）

| 文件 | 决策 | 理由 |
| --- | --- | --- |
| `api/controllers/console/app/app.py` | 取上游 `AppPagination.model_validate(..., context={"session": session})`，保留 fork `recommended_apps` 赋值；另按 §1.4 把 `retention_number` 迁入 `AppDetailWithSite` 并在 `AppApi.get` 用注入 session 回填 `AppExtend.retention_number` | 双向合并 + modify/delete 迁移落点 |
| `api/controllers/console/app/model_config.py` | 取上游 `with_session`/`is_agent_with_session` 新结构，fork 记忆上下文块（AppExtend.retention_number 写入 + redis 失效）重写为注入 session、与模型配置同事务 | 挂点波及（记忆上下文） |
| `api/controllers/console/explore/completion.py` | fork `@money_limit` + `calculate_cumulative_usage` 与上游 session 化/conversation 预校验并存 | 双向合并 |
| `api/controllers/console/tag/tags.py` | import 双向合并；fork 标签解绑后 `RecommendedAppService.sync_recommended_app` 同步调用存活 | 仅 import 冲突 |
| `api/controllers/console/workspace/workspace.py` | 删除上游已退役的 marshal 字典（fork 曾在其中加 admin_extend/tenant_extend）；两个权限位改挂到 Pydantic `TenantInfoResponse`（get_tenant_info 仍写入这两个键，此前会被响应模型丢弃） | marshal→Pydantic 迁移，字段换宿主 |
| `api/controllers/web/completion.py` | 取上游 `with_session` 签名，fork 的登录校验（WebAuthRequiredErrorExtend）、余额判断（AccountNoMoneyErrorExtend）、`account_id` 归因、使用统计原位保留 | web 端计费链路 |
| `api/controllers/web/login.py` | 取上游 `LoginStatusQuery`/`LoginStatusResponse` Pydantic 结构，fork `console_logged_in`（Console cookie 校验）字段加入 `controllers/common/fields.py::LoginStatusResponse` 并在两处返回中回填 | 字段换宿主 |
| `api/core/helper/code_executor/code_executor.py` | 取上游 running_language 校验，fork `purview`→`FULL_CODE_EXECUTION_ENDPOINT` 端点切换保留；`execute_workflow_code_template` 保留 purview 参数 + 上游返回类型注解 | fork 代码执行控制 |
| `api/core/workflow/node_factory.py` | import 双向合并；`ExecutionControl` 注入存活（285 行 check_code） | 仅 import 冲突 |
| `api/models/model.py` | `from_end_user_session_id_with_session` 取上游 session 化骨架，fork 的 end_user→关联用户名展示逻辑重写为用注入 session | 双向合并 |
| `api/tests/unit_tests/services/test_account_service.py` | 取上游 sqlite_session 新测试结构，保留 fork 的 AccountMoneyExtend/TenantExtendService/db patch（setup 内 fork 扩展依赖全局 db.session） | 双向合并 |
| `web/service/client.ts` | 取上游动态契约链路（createConsoleDynamicLink + per-segment OpenAPILink），fork 的 `setLoginConfigToken`/`X-Login-Config-Token` header 注入迁入 `createConsoleOpenAPILink`；`consoleClient` 类型改用 `ConsoleRouterContractWithExtend`（并入 fork 契约段） | 契约层重构（见 §1.4 web/contract） |
| `web/service/apps.ts` | 保留 fork `syncApp`/`syncCancelApp`/`editApikey`；`createApikey` 保留 fork 的 `{ body }` 包装（额度参数作为 JSON 请求体，上游裸传 body 会丢失额度参数） | fork 模板同步 + 密钥额度 |
| `web/service/explore.ts` | 整体取上游（normalize 化重写），重挂 fork 的 `fetchOpenInstalledAppList`、legacy-get 版 `fetchInstalledAppList`（fork 后端响应含二开字段，contract normalizer 会在运行时丢字段），删除 unused normalizeInstalledApp | 数据层重构 |
| `web/models/common.ts` | 跟随上游删除 InvitationResult/InvitationResponse（invite 流程改生成契约类型），保留 fork Billing/ApiForwarded 类型与 ICurrentWorkspace 的 admin_extend/tenant_extend | 双向合并 |
| `web/features/system-features/client.ts` | 保留 fork login_config 双阶段（bootstrap→setLoginConfigToken→loginConfig），外层格式跟上游 | CVE-2025-63387 |
| `web/features/system-features/server.ts` | 保留 fork SSR 侧 branding 形状守卫（system-features 桩响应回退 defaults） | 同上 |
| `web/app/components/main-nav/index.tsx` | 整体取上游新版（detail sidebar 移入 @detailSidebar parallel route 后 MainNav 大幅简化），重挂 fork：AccountMoneyExtend 额度徽章、SystemManageNavExtend 入口、预留挂载位注释；routes.ts 的 home→apps-center-extend 自动合并存活 | 上游 ±357 行重构 |
| `web/app/components/main-nav/__tests__/index.spec.tsx` | 取上游格式 + fork home 断言（/explore/apps-center-extend） | 跟随 |
| `web/app/components/apps/list.tsx` | 取上游新版（learnDify 入口等），AppCard 重挂 fork `onApp={recommendedApps.includes(app.id)}` | 双向合并 |
| `web/app/components/apps/app-card.tsx` | 取上游结构，fork 模板同步整链重挂：onApp prop、canSyncToAppTemplate（改用 isCurrentWorkspaceManagerAtom + useExtendPermissions）、两个操作菜单的 syncOption/onSyncToAppTemplate/onCancelSyncToAppTemplate、两个确认弹窗 | 前端最重文件之一 |
| `web/app/components/explore/app-card/index.tsx` | 保留 fork onApp/onRefresh/syncApp 弹窗，权限判断迁 useExtendPermissions | fork 模板同步 |
| `web/app/components/explore/app-list/index.tsx` | 取上游结构，重挂 fork：TagFilter 标签过滤、搜索匹配描述、recommendedAppIds/onApp/onRefresh | fork 应用中心搜索 |
| `web/app/components/explore/sidebar/index.tsx` | 整体取上游（fold 化重构），重挂 fork：discovery 链接仅 owner 可见（isCurrentWorkspaceOwnerAtom）、应用中心入口链接（extend 词条） | fork 应用中心 |
| `web/app/components/explore/sidebar/__tests__/index.spec.tsx` | 桩 workspace-state 原子替代旧 app-context mock；补应用中心链接断言；折叠态可达名断言改 extend 词条 | 跟随 |
| `web/app/components/header/account-setting/index.tsx` | 取上游结构，fork「普通成员不渲染模型供应商 tab」的 filter 重挂（isCurrentWorkspaceManagerAtom） | fork 权限裁剪 |
| `web/app/components/header/account-setting/members-page/invite-modal/index.tsx` | **整体取上游**：上游新 Form + EmailRecipientsField 原生提供多邮箱 chip 输入，fork 的 react-multi-email-extend 定制被上游能力吸收（组件文件保留但不再被引用，Phase 3 决定去留） | fork 定制被上游吸收 |
| `web/app/components/develop/secret-key/secret-key-button.tsx` | 取上游格式，fork「非管理员隐藏入口」重挂（isCurrentWorkspaceManagerAtom）；两个关联 spec 的 mock 改桩 workspace-state | fork 权限裁剪 |
| `web/app/components/develop/secret-key/secret-key-modal.tsx` | 取上游 Dialog/formatTime 新结构，fork 密钥额度整链保留：额度列表格（LEFT JOIN 字段展示）、额度设置/编辑弹窗、editApikey、创建先弹额度框 | 挂点 6 前端面 |
| `web/app/components/develop/secret-key/style.module.css` | fork 宽弹窗（1200px/90vw）+ editIcon 样式保留 | 同上 |
| `web/app/components/base/chat/chat/index.tsx` | 取上游新结构（renderAgentContent 等），fork 消息上下文整链重挂：contextList 拉取（messageContextList）、答案下方清除/恢复上下文标签（Fragment 包裹） | 挂点 9 前端面 |
| `web/app/components/app/configuration/hooks/use-configuration.ts` | 取上游 useCallback 新格式，fork retentionNumber 传参 + 依赖数组重挂 | 记忆上下文 |
| `web/app/components/app/configuration/hooks/use-configuration-utils.ts` | 取上游 createPublishHandler 新结构（typed-selector t），fork retentionNumber 参数 + 发布 body 注入重挂 | 记忆上下文 |
| `web/app/(shareLayout)/components/authenticated-layout.tsx` | 取上游格式，fork isCheckingAuth（WebApp 复用 Console 登录态）保留 | fork 登录链路 |
| `web/app/(shareLayout)/webapp-signin/page.tsx` | 取上游 loginRedirect 安全跳转体系，fork Console 登录态检查（checkConsoleLoginStatus + isCheckingAuth Loading）重挂 | fork 登录链路 |
| `web/app/(shareLayout)/webapp-signin/normalForm.tsx` | 取上游 typed-selector 格式，fork「品牌定制时隐藏欢迎语」保留 | 双向合并 |
| `web/app/signin/normal-form.tsx` | 取上游结构，fork 钉钉/OAuth2 登录按钮判定与钉钉自动登录逻辑保留；登录后默认落点改挂 `getClientLoginFallback` | fork 登录扩展 |
| `web/app/signin/check-code/page.tsx`、`components/mail-and-password-auth.tsx`、`invite-settings/page.tsx`、`one-more-step.tsx` | 取上游 `replaceLoginRedirect(resolvePostLoginRedirect(...))` 安全跳转；fork「默认跳转应用中心」语义集中迁移到 `web/utils/login-redirect.ts::getClientLoginFallback`（'/'→'/explore/apps-center-extend'），四处调用点不再各自散落 | 落点语义换宿主（集中化） |
| `web/app/components/workflow/block-selector/__tests__/*.spec.tsx`（3 个） | 取上游 | fork 侧仅为 1.15.0 合并期的类型断言排版差异，无功能语义 |

### 1.4 modify/delete 冲突（7 个）

| 文件 | 上游去向 | 处置 |
| --- | --- | --- |
| `api/fields/app_fields.py` | `904fadde20`：marshal 字典退役，App 详情改 Pydantic | 删除旧文件；fork `retention_number` 迁入 `console/app/app.py::AppDetailWithSite` 字段，并在 `AppApi.get` 用注入 session 查 `AppExtend` 回填（原 1.15.0 仅挂在 trial.py 用的死 marshal 字典上，本次迁移同时修复 web 配置页读不到该字段的问题） |
| `web/context/app-context.ts` | `23b936aeb5`：拆为 account-state/workspace-state 等 jotai 原子 | 删除；`admin_extend`/`tenant_extend` 迁移：`app-context-normalizers.ts::normalizeCurrentWorkspace` 补 extend 块（`ICurrentWorkspace` 的两个 extend 字段已在 models/common.ts 存活），新增 fork-only `web/context/app-context-extend.ts`（adminExtendAtom/tenantExtendAtom/useExtendPermissions） |
| `web/context/app-context-provider.tsx` | 同上 | 删除；fork 对 provider 的修改（从 workspace 响应回填权限位）等价迁移到 normalizeCurrentWorkspace |
| `web/contract/router.ts` | `61650d34ce`：契约全走生成物 `router.gen.ts` + 按 segment 懒加载 | 删除 router.ts；fork 契约（login_config 双阶段、systemManage 代码执行控制）保留在 fork-only `web/contract/`（重建 `base.ts`，新增 `router-extend.ts` 聚合），运行时经 `console-router-loader.ts` fork 段注册接入，类型经 `ConsoleRouterContractWithExtend` 并入 `consoleClient`；`X-Login-Config-Token` header 注入迁至 `createConsoleOpenAPILink` |
| `web/__tests__/apps/app-list-browsing-flow.test.tsx` | `5fd06fafe0` 移除低价值单测 | 跟随删除（fork 仅 +1 行 mock） |
| `web/__tests__/apps/create-app-flow.test.tsx` | 同上 | 跟随删除（fork 仅 +1 行 mock） |
| `web/__tests__/explore/explore-app-list-flow.test.tsx` | 同上 | 跟随删除（fork +7 行为 refetch/TagFilter 测试桩，无独立断言价值；应用中心行为由 `app-list-center-extend` 自有测试覆盖） |

另：`system-manage-extend/layout.tsx` 与 `main-nav/components/system-manage-nav-extend.tsx`（fork 文件，无冲突但 import 已断）从 `useAppContext` 迁移到 `isCurrentWorkspaceOwnerAtom`。

## 2. 留给 Phase 3 的 TODO

1. **i18n typed-selector 迁移（前端最大机械任务）**：fork 全部 web 文件的旧式 `t('key', { ns })`
   调用迁移到 `t(($) => $['key'], { ns })`（112+ 处，含本次重挂保留的 extend 组件、
   secret-key-modal、chat/index、sidebar、app-card 等）；`web/i18n/*/extend.json` 命名空间接入
   `web/i18n-config/locale-resources/*.ts` 生成清单与类型声明。optimize 模式下旧 API 运行时失效，
   构建可能全绿——必须当一等公民验证。
2. **挂点逐项复核**（差异总表第 7 节 10 项），重点：
   - 挂点 4：三个 generator 的联表写入已改为共享调用方 session 且 commit——复核与
     `_init_generate_records` 的事务边界、扣费只执行一次语义；
   - 挂点 6：`apikey.py` with_session 重写后跑 `test_apikey.py`/`test_datasets.py` 单测确认；
   - 挂点 9：`add_messages_context`/`control_registers` 全链逐行核对（1.13.3 有丢失前科；本次全链自动合并，未人工验证）；
   - 挂点 1：3 个 runner `user_from=` 实参已确认存活，复核 graphon 0.6.0 import（`HumanInputRequired` 移位）。
3. **web 契约层迁移验证**：fork-only `web/contract/`（base.ts/router-extend.ts）+
   `console-router-loader.ts` fork 段注册是最小可编译迁移，需运行时验证：
   login_config 双阶段（含 X-Login-Config-Token header 实际发出）、system-manage-extend
   代码执行控制三个端点、`consoleQuery.systemManage.*` 查询键形状。
4. **app-context 删除的迁移面回归**：`useExtendPermissions`/adminExtendAtom 数据链
   （current workspace 响应 → normalizeCurrentWorkspace → atom）运行时验证；main-nav 双角色、
   模板同步按钮可见性、系统管理入口。
5. **登录默认落点集中化验证**：`getClientLoginFallback` 改为 `/explore/apps-center-extend` 影响所有
   登录后 fallback（signin 四处 + webapp-signin 无 redirect 场景 + device 流程 fallback），
   需回归确认 webapp 场景不受负面影响。
6. **invite-modal fork 定制被上游吸收**：`react-multi-email-extend` 组件已无引用，确认后删除或保留；
   验证邀请流程（含 emailRegex 校验语义是否被上游 EmailRecipientsField 覆盖）。
7. **`retention_number` 新宿主验证**：`AppDetailWithSite.retention_number` 由 `AppApi.get` 回填
   （1.15.0 时该字段挂在已死的 marshal 字典上，web 端实际拿不到；本次算修复），验证配置页回填与发布链路。
8. **`docker/docker-compose.dify-plus.yaml` 移植**（分析 §4.4）：`agent_backend`/`local_sandbox`
   服务、`AGENT_BACKEND_*`/`DIFY_AGENT_*` 变量、`API_WEBSOCKET_WORKER_AMOUNT`、Redis keepalive、
   `WORKFLOW_GENERATION_TIMEOUT_MS` 透传；镜像 tag 1.15.0→1.16.0。
9. **service_api api_token 参数全量清点**：本次已给 7 个冲突 controller 的全部
   `validate_app_token` 方法补 `api_token` 末位参数（含 fork 1.15.0 遗留缺口）；但非冲突文件
   `file_preview.py`、`human_input_form.py`、`workflow_events.py` 的方法仍缺该参数（wraps 始终注入
   kwargs → 运行时 TypeError 风险，属 fork 既有问题）。建议 Phase 3 统一补齐，或把 wraps 注入改为
   签名感知。
10. **锁文件/生成物再生成**：`pnpm install`（pnpm-lock 校验）、`uv sync`（graphon 0.6.0 等）、
    lint 后按需再生成 `oxlint-suppressions.json`。
11. **单测适配跟进**：`test_apikey.py`/`test_persistence_layer.py`/`test_account_service.py`/
    `test_app_generator_and_runner.py` 的 fork 断言已按新实现改写，需实际运行确认；
    sidebar/secret-key-button/ApiServer 三个前端 spec 的 workspace-state 桩需 vitest 验证。
12. **一次性命令确认**（Phase 6 输入）：1.16.0 新增 `migrate_dataset_permissions_to_rbac`、
    `backfill_workflow_run_archive_bundles` 是否进升级 runbook。

## 3. 合并提交信息

`merge(upstream): merge upstream 1.16.0 into fork, resolve 75 conflicts`
（68 content + 7 modify/delete；本文档与合并同提交落库。）
