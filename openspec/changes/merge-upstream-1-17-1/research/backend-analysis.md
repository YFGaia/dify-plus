# Dify-Plus 1.16.0 → upstream 1.17.1 后端合并分析

## 0. 证据和范围

- fork：`HEAD 1c3368ed1584c4e9b6387a28334552d10c946ab4`；比较基线为 upstream 1.16.0 tag `5c6372d2f76d240265b92fd27c16bc772ffcb107`，目标 upstream 1.17.1 tag `8387590ace4a094de812b7847fc6a4c3a27cd52b`。只读比较使用 `/private/tmp/dify-1171-plan.LqV0hY/objects.git`；没有 checkout、修改业务源码或执行测试。
- 本节依照 `docs/dify-plus/与上游差异总表.md` §7 的十项挂点、`docs/dify-plus/上游升级与回归检查清单.md`、当前 `api/` 源码、目标 tag 的源码和迁移文件。合并模拟有 38 个 API 冲突路径，另有自动合并后可能语义失效的路径；冲突原始记录在 `merge-tree.txt`。
- HEAD 在 `fork-merged-1.16.0` 后新增 per-app WebApp 认证开关（`1ffa101203`）和匿名会话记忆修复（`fc5ceb5283`）。`openspec/changes/p4-billing-quota-hardening` 的任务均未勾选；其 contextvar、扣费幂等、缓存、归档均是后续提案，不能当作当前行为。

## 1. 后端执行 DAG（Graph Engineer 节点）

以下 B0-B9 是后端补充分析中的局部节点标识；最终全局执行图由主任务统一映射为 B/F/D 编号。

| 节点 | 前置 | 可执行工作 | 完成证据 |
|---|---|---|---|
| B0 基线冻结 | 无 | 记录上述三 SHA、38 个 API 冲突、十项挂点源代码与两条 Alembic head；保存预升级数据快照和关键表行数/引用基线。 | inventory + 快照位置；不覆盖共享工作区未跟踪文件。 |
| B1 上游基础整合 | B0 | 合入目标源码，先处理 `api/models/__init__.py`、`models/workflow.py`、`controllers/console/__init__.py`、新 application services/仓储/授权框架；保持上游 1.17.1 的路由、实体、签名。 | 所有 API 冲突解决且无旧包导入、路由重复注册；目标新增 API 仍在。 |
| B2 双迁移演练 | B1 | 审核 17 个上游迁移和 fork 019 扩展头，先备份后在 PostgreSQL/MySQL 克隆库执行主链再扩展链，审计不可逆数据变更。 | `alembic_version`=`c3f1a9b2e6d4`，`alembic_version_extend`=`019_webapp_auth_switch`；表、索引、行数和引用对账。 |
| B3 账号创建/登录 | B1,B2 | 将 fork 额度建档从仅 `RegisterService.setup/register` 的挂点重挂到所有真实创建入口共用的事务边界；保留默认额度、依赖账户唯一约束避免双建档；融合新 normalized_email 逻辑。 | setup、邮箱、OAuth、邀请/已有账号路径的额度行/账号行一一对应，无重复。 |
| B4 OAuth2/钉钉 | B3 | 在新 provider registry/账户 OAuth service 接入 OaOAuth 专用 gateway；适配 code 与经认可的 token fallback、dict token/id_token、state 回跳、错误映射；保留钉钉独立路由。 | GitHub/Google/OAuth2/钉钉和未配置、邀请、回跳安全场景通过。 |
| B5 WebApp 访问 | B1,B2,B3 | 保留 per-app 开关的模型/迁移/Redis 读写，把站点写入适配 `AppSiteService`；把登录状态字段与生成端点的匿名判定对齐新 Passport 服务和企业访问模式。 | 公开/需登录 × 匿名/已登录矩阵通过；应用间隔离；token 有正确 app/end_user。 |
| B6 Service API 与密钥 | B1,B2,B3 | 在新版 `wraps.py` 重挂前置额度与 end_user 归因；所有装饰方法签名和 app_model/api_token 注入契约完整；融合 API key 权限、dataset binding、masking 与 fork 额度字段/软删。 | 额度拦截、API 密钥归因、dataset 绑定/越权及 key CRUD 通过。 |
| B7 运行时计费 | B5,B6 | 逐项复核持久化层派发、五类 generator/token 关联、消息 handler、Celery beat；确认合并未在恢复/重试控制流新增计费派发路径。 | 六条既有计费业务链按当前行为回归；核对 INITIAL/RESUMPTION/retry 的派发次数相对 1.16.0 无合并新增重复。现有 Celery 重投非幂等和并发少扣列 P4 债务，不作为本轮新增实现或“已通过”断言。 |
| B8 记忆上下文 | B5,B7 | 复核 `control_registers` 全链、`AppRunner.add_messages_context`、匿名 AppExtend NULL、上游 Agent v2 workspace 新路径。 | 记忆分割记录、历史截取、匿名公开应用不报错；无 NameError。 |
| B9 后端验收与文档 | B2-B8 | 对真实合并结果作 import/lint/type、定向测试、迁移克隆演练和业务回归，更新十项挂点新坐标、迁移运行手册和 OpenSpec 契约。 | 见 §7；未经运行的门禁标记为待验证，不能将静态分析当验收。 |

B3/B4/B5/B6 可以按文件边界并行实现；B7 依赖它们汇合。`wraps.py`、`account_service.py`、`site.py` 由单负责人合并对应改动，避免 P4 未来改造与本次升级同时写同一宿主。

## 2. 十项挂点逐项源代码对照

| # | 当前 fork 与上游 1.17.1 真实变化 | 保留约束和实施落点 | 验收场景 |
|---|---|---|---|
| 1 工作流节点计费 | fork `api/core/app/workflow/layers/persistence.py::WorkflowPersistenceLayer._handle_node_succeeded` 尾部调用 `update_account_money_when_workflow_node_execution_created_extend.delay`；目标改 `_handle_graph_run_started(event)` 以恢复旧节点 cache/sequence，Agent v2 节点同步保存，retry process_data 保留 binding id。 | 在目标 `_handle_node_succeeded` 成功路径重新派发；保留 `created_by/created_by_role`、LLM 节点过滤和 Celery payload，不能因新增 Agent v2 同步保存、retry/RESUMPTION 控制流再加一次派发。`api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py` 的 Celery 重投非幂等为已有 P4 债务，本轮不改。 | 一次节点成功触发与 1.16.0 相同的一次派发；失败不派发；retry/RESUMPTION 不因合并新增派发。重复投递同一任务仍可能双扣，单列已知缺陷，不能断言通过。 |
| 2 token/account extras | fork `workflow/advanced_chat/app_generator.py::generate` 写 `extras["app_token_id"]` 和 `extras["account_id"]`。目标两个 generator 改 worker join、恢复时 graph reload，extras 块本身未大改。 | 保留写入在请求线程、worker 启动前；不要依赖异步线程中的 Flask/contextvar。P4 若以后去除 kwargs 注入，必须先把 token ID 物化到 extras。 | service API workflow/chatflow 产生正确 token join；WebApp 登录用户归因；stream 阻塞/结束后仍有 ID。 |
| 3 chatflow/workflow token join | fork `advanced_chat/generate_task_pipeline.py::_handle_workflow_started_event`、`workflow/generate_task_pipeline.py::_handle_workflow_started_event` 在 run_id 确定后写 `ApiTokenMessageJoinsExtend`。目标改 TTS 流终止、message trace、workflow response；start handler 仍在。 | 两条分支都按 INITIAL 建关联，保留 `workflow_run_id`/`app_token_id`；RESUMPTION 不新增重复 join。 | 初始调用各一行；resume 无重复；回调结束 join 可供 Celery 查到。 |
| 4 chat/agent_chat/completion token join | fork三个 `app_generator.py::generate` 在 `_init_generate_records` 后用调用者注入 `session.add+commit`。目标 chat/agent_chat 文件未变，completion 仅文档变化。 | 即使自动合并，也逐路径确认 `args["api_token"]` 从 controller 到 generator 仍传递、提交发生在消息计费信号前。 | 三类 API 消息各一 join 且 token 金额随 message 用量增长。 |
| 5 Service API 前置额度/归因 | fork `api/controllers/service_api/wraps.py::validate_app_token` 调 `validate_token_quota_extend`、注入 `kwargs["api_token"]`、`create_or_update_end_user_account_join_extend`。目标 wraps 新增 Cloud Sandbox vector-space 503、dataset token binding 校验；`validate_app_token` 主体仍同形，但 service_api 多个 controller 冲突。 | 保留 fork 账号总额、密钥日/月额度 `-1` 不限、owner-end_user 映射；保留上游云订阅与 dataset binding 逻辑。所有 `@validate_app_token` 方法兼容 `app_model` 和 `api_token: ApiToken \| None = None`，不能只改冲突文件；P4 contextvar 仍另列。 | 密钥总额/日/月边界、无额度记录告警、tenant archive、dataset key bound/unbound/越权；completion/chat/workflow HTTP 调用无 TypeError。 |
| 6 API Key 额度联查 | fork `api/controllers/console/apikey.py` 的 `ApiKeyItem` 六额度字段、LEFT JOIN、创建额度行、编辑、删除软删；目标加入 RBACCheck、tenant_id 过滤、Agent access gate、dataset binding、dataset key 列表脱敏和 `session.delete(key)`。 | 保留上游权限与 dataset scope/masking，fork 额度字段不能让 dataset 密钥 reveal-once 失效；创建密钥与额度同行，删除 token 前软删/清理额度关联；审视新的 FK cascade 对扩展表无 FK 的影响。 | app/agent/dataset key 列表、创建、编辑、删除；不同租户/角色 403；dataset key 列表只显示遮罩；额度数据与 token ID 对应。 |
| 7 message handler | fork `api/events/event_handlers/update_account_money_when_messaeg_created_extend.py::handle` 监听 `message_was_created`；目标 `easy_ui_based_generate_task_pipeline.py::_save_message` 仍发送信号，但停止场景改保留 provider 用量后保存。 | 保留 handler 注册、USD/RMB 换算、payer 优先级、token 金额更新；核对 signal 与消息 commit 顺序，尤其停止/错误/重试。本轮保持匿名 end_user 无映射时的既有归因/扣费行为；其非 Account ID 建档以及读改写并发少扣均作为已知债务另列。 | Console/Explore/WebApp 单次消息按当前行为记账；停止时 provider 用量按当前信号路径处理；公开匿名调用记录现有归因结果，不把现有债务作为本轮验收阻断或宣称已修复。 |
| 8 beat 重置 | fork `api/extensions/ext_celery.py::init_app` 注册三个 extend quota reset；目标新增 conversation cleanup、社区 telemetry 等 beat/import。 | 保留 `ENABLE_EXTEND_QUOTA_RESET_TASKS` 三项、队列、时间与开关；新 beat 配置不得覆盖 fork 字典；P4 分批任务仍未实施。 | 任务名/调度唯一；日/月快照与归零准确，无重复注册。 |
| 9 记忆上下文 | fork `token_buffer_memory.py::get_history_prompt_messages` + `control_registers`、`base_app_runner.py::organize_prompt_messages/add_messages_context`、chat/agent_chat runner；目标核心记忆文件与 base runner 未变，chat runner 仅增加 credit request_metadata。 | 自动合并后还要检查完整参数传递与 assistant `name=message.id`；保留 `add_messages_context` 函数内延迟导入 db，`AppExtend.retention_number IS NULL` 跳过（auth 开关可单独创建 AppExtend 行）。上游 Agent v2 改 workspace/binding，不等于 fork 消息记忆机制自动覆盖。 | 有/无 retention、开关仅有 AppExtend、公开匿名会话、多轮截断；无 NameError 和错误分割。 |
| 10 OAuth | fork `api/libs/oauth.py::OaOAuth`、旧 `console/auth/oauth.py::get_oauth_providers/OAuthCallback` 支持 OAuth2、Casdoor dict token/id_token、state redirect；目标 callback 转为 `application_services().accounts.oauth`，provider registry 位于 `extensions/ext_application_services.py`，`DifyOAuthProviderGateway` 只注册 github/google，`OAuthCallbackQuery.code` 必填。 | 保留 OaOAuth 类不足以生效：须在新 registry 注册专用 gateway；适配 `get_access_token` 的 dict/str、`get_user_info`、配置缺失、code 或既有 fallback、id_token 返回；在 `_safe_console_redirect_target` 同源校验后拼接参数。钉钉单独 `ding_talk_extend.py`/路由需复核。 | GitHub/Google 无回归；OAuth2 code、Casdoor fallback、id_token、invite_token、异源 redirect 回退、配置缺失；钉钉登录。 |

## 3. 自动合并也会失效的业务链

1. **账号额度初始化是确定的断链。** fork 只在 `api/services/account_service.py::RegisterService.setup/register` 插入 `AccountMoneyExtend`。目标邮箱注册经 `account_email_registration_adapters.py::AccountServiceRegistrationGateway.create → AccountService.create_account_and_tenant → create_account`；OAuth 注册经 `repositories/account_oauth_repository.py::AccountServiceOAuthAccountRegistrationGateway.register → AccountService.create_account`，均绕过 `RegisterService.register`。应在共用创建边界用同一 Session 幂等写额度（或明确为所有入口增加相同调用），删除旧重复写；还需覆盖 setup、旧调用方。`accounts.normalized_email` 新增使账号匹配/邀请流程也改变。
2. **WebApp 开关站点写路径失效。** fork `controllers/console/app/site.py::AppSite.post` 在上游旧 `with_session` 中写 AppExtend。目标路由改 `@console_account_admission()` + `AppSiteService.update(request_context, app_id, AppSiteChanges)`；`AppSiteUpdatePayload.to_changes()` 原样 dump 所有字段。直接加 `webapp_auth_enabled_extend` 会传给不接受该字段的 `AppSiteChanges`；应明确剥离并协调站点与 AppExtend 两次写的事务/缓存失效时点，或在新 repository/service 扩展一个一致事务。目标 `/web/passport` 由 `WebPassportService.issue` 处理，fork 开关目前控制生成端点和 `/web/login/status`，也要明确 Passport 与公开访问的一致策略。
3. **公开模式计费是已知债务，本轮保持当前语义。** fork `controllers/web/completion.py::is_money_limit` 按 `end_user.id` 查账号额度；匿名一般无额度行。message handler 在既非 Account 又无 `EndUserAccountJoinsExtend` 映射时以 end_user UUID 建账号额度行。本次合并只验证这种现有结果未发生漂移，不新定付款人、不改前置判断与事后归因。将匿名付款人/限额统一策略另列为用户后续决策与独立变更；公开访问验收采用当前 per-app 开关矩阵。
4. **登录配置与敏感字段分级。** 目标 `console/feature.py::SystemFeatureApi` 仅返回 `get_public_system_features()` 非敏感快照，完整 license 新增 `/system-features/license` 且有 `console_account_admission()`。fork `login_config_bootstrap` JWT/IP 校验与 `login_config` 可保留，但其返回模型需适配新的 public snapshot，不能通过预登录路由重新暴露 license 详情。
5. **OAuth provider 栈变换。** 目标 `AccountOAuthService` + registry/gateway 接管账号 claim 锁、注册、workspace、session 和邀请，旧 callback `_generate_account` 删除。fork token fallback、id_token、OaOAuth 应集成到新 gateway/实体，不复活整段旧 controller 以免绕过锁与邮箱归一规则。
6. **上游新能力不能被 fork 额度覆盖。** `controllers/service_api/wraps.py` 的 Cloud vector-space Sandbox 未知用量返回 503、知识库 API token binding、`apikey.py` 的 RBAC 与 dataset key 遮罩必须保留。fork 自建计费与 upstream Cloud plan 是两套边界。

## 4. 主 Alembic 新增 17 项完整清单

旧主链 head `7a1c2d9e4b60` → 新 head `c3f1a9b2e6d4`，下面按 `down_revision` 顺序排列；不能按文件名日期排序（7 月 15/6 月 29 的命名顺序不同）。fork 独立链仍是 `018_drop_recommended_cats` → `019_webapp_auth_switch`，版本表 `alembic_version_extend`。目标 upstream 没有 `migrations_extend`。

| 顺序 | revision | 实际操作与专门检查 |
|---:|---|---|
| 1 | `3c9f8e2a1d7b` | workflow run archive bundle 两组游标索引；PostgreSQL `CREATE INDEX CONCURRENTLY` 有 autocommit 块，演练锁与失败重试。 |
| 2 | `b8c9d0e1f2a3` | 新建 `account_step_by_step_tour_states`。 |
| 3 | `d2825e7b9c10` | `agent_debug_conversations` 增 `draft_type` 并处理旧 debug 会话作用域。 |
| 4 | `6f5a9c2d8e1b` | `dify_setups` 加 instance/telemetry 时间字段。 |
| 5 | `2f39536b3feb` | 新建 `agent_home_snapshots` 并在 agent config/runtime 会话表加 home snapshot 引用和索引调整。 |
| 6 | `f6e4c5686857` | 新建 `agent_workspaces`、`agent_workspace_bindings`；删除 `agent_runtime_sessions`；向 conversations/drafts 等加 binding 引用。源迁移未见旧 session 数据回填，需事前盘点且保存快照。 |
| 7 | `e4708db55c1d` | home snapshot 关联列允许 NULL，配合新 workspace 流程。 |
| 8 | `a1c7f4e9b3d2` | 新建 workflow version counter、`workflows.version_number` 与应用版本索引。 |
| 9 | `f3a9c2d17b4e` | OAuth provider app 加 `auto_authorize` 布尔列。 |
| 10 | `56124e050600` | conversations 软删清理索引。 |
| 11 | `89919253ca7a` | 清除 agent config JSON 的 soul `files`、workflow binding 的 `drive_key`，删除 `agent_drive_files`；downgrade 仅重建空表，旧内容不可还原。 |
| 12 | `fbdfcf5f5a6e` | 继续清除遗留 agent soul file JSON 数据；属数据变换，须记录前后数量。 |
| 13 | `925e75620b69` | 清理 agent v2 node_job_config 的旧 preset outputs（text/files/json）；downgrade `pass`，单向。 |
| 14 | `a4f8d2c9e1b0` | skills、skill drafts/versions、agent skill bindings/snapshots 等 workspace skill 管理表及索引。 |
| 15 | `9b7c6d5e4f3a` | accounts 加 `normalized_email` 非唯一索引并回填；Gmail/googlemail 去点号、去加号别名，其他邮箱小写。既有同义账号可并存，但新注册会防碰撞；审计同义分组。 |
| 16 | `5578e028b2f2` | 5 张 provider/model 表 legacy model_type 归一，删除冲突重复行并重写 credential 引用；downgrade `pass`。在生产快照上比较去重组、胜出行和被删行，准备数据库级回滚。 |
| 17 | `c3f1a9b2e6d4` | dataset API token binding 表和两侧 FK cascade；无 binding 代表租户内全部知识库，N 行代表限定集合。检查已有 dataset key 无意改变语义、token 删除级联与 fork quota 软删。 |

迁移顺序：备份 → 克隆库先查两链 current/heads → 主链 upgrade → 扩展链 upgrade → 两个版本表与结构/行数核对 → 业务回归。命令明确在 `api` 目录执行，并指定 Flask 入口，避免从仓库根目录找不到 app：

```bash
cd /Users/liuxingwang/go/src/dify-plus/api
FLASK_APP=app.py uv run --project /Users/liuxingwang/go/src/dify-plus/api flask db current
FLASK_APP=app.py uv run --project /Users/liuxingwang/go/src/dify-plus/api flask extend_db current
FLASK_APP=app.py uv run --project /Users/liuxingwang/go/src/dify-plus/api flask db heads
FLASK_APP=app.py uv run --project /Users/liuxingwang/go/src/dify-plus/api flask extend_db heads
FLASK_APP=app.py uv run --project /Users/liuxingwang/go/src/dify-plus/api flask db upgrade
FLASK_APP=app.py uv run --project /Users/liuxingwang/go/src/dify-plus/api flask extend_db upgrade
```

生产仍需维护窗口、快照恢复演练；对第 6、11、13、16 项不能把 `downgrade` 视为数据回滚。第 16 项 MySQL/PostgreSQL SQL 分支均应各自演练。

## 5. 现有契约文件需更新的点

- `openspec/changes/archive/2026-07-05-p2-merge-upstream-1-14-2/specs/billing-hook-relocation/spec.md` 和 `.../upstream-merge-baseline/spec.md`：十挂点新坐标、RESUMPTION/retry、dataset key 权限与 OAuth gateway；原断言仍可作为行为基础。
- `openspec/changes/p4-billing-quota-hardening/{proposal,design,tasks}.md` 及 `specs/service-api-token-context`、`billing-deduction-idempotency`、`quota-check-caching`：把目标上游 1.17.1 的新 controller/service 结构与当前未实施状态写明；升级 B6/B7 不应顺手宣布 P4 已完成。
- WebApp per-app switch 的已有 1.16.0 之后测试与文档：改成公开/需登录矩阵；记录本轮保持的匿名计费结果。匿名付费策略另列为后续业务决策，不纳入本轮合并实现。主仓 `docs/dify-plus/与上游差异总表.md` §7 与 `上游升级与回归检查清单.md` 的强制登录旧表述需同步修订。

## 6. 后端验证清单（实施阶段运行，分析阶段未运行）

1. 静态/导入：无冲突标记；在上述 `api` 目录执行 `FLASK_APP=app.py uv run --project /Users/liuxingwang/go/src/dify-plus/api python -c 'import app_factory; import controllers.console; import controllers.web; import controllers.service_api'`；API route inventory 确认 `oauth2`、DingTalk、`login_config_bootstrap/login_config`、`system-features/license` 唯一注册；目标新增 service 包均可导入。按项目命令运行 lint、type-check、目标测试；不要把 import 通过当业务验收。
2. 迁移：PostgreSQL 与 MySQL 克隆库按 §4 两链演练，检查两个版本表、17 个 revision、fork 扩展所有表/索引；记录第 6/11/13/16 项数据行数和 JSON 字段前后差异。MySQL 可见的 temporary table 重试路径单独演练；保留 `pg_dump`/等效快照恢复门。
3. 账号/OAuth：setup、邮箱注册、GitHub/Google/OAuth2、邀请、已有账号、重复 normalized_email 各一；每个新 Account 仅一条额度行，token/cookie/tenant 正确，OAuth callback 同源回跳，外域落回 Console。未配置 OAuth2 不能生成空 redirect。
4. WebApp：同一租户两个 app 分别开/关认证，未登录/已登录访问 `/web/login/status`、`/web/passport`、completion/chat/workflow；公开模式匿名生成成功，保护模式 401，跨 app token/EndUser 不串。只有 auth 字段的 AppExtend 行 `retention_number=NULL` 时聊天不 500。
5. 密钥/计费：Service API completion/chat/agent_chat/chatflow/workflow 用 token 生成，检查 join 行、账户和 token 增量、USD/RMB；日/月与总额边界、`-1` 不限；dataset key reveal-once、绑定/不绑定、tenant/RBAC 越权。workflow retry/resume/stop 仅验收合并未新增重复派发，记录现有 Celery 重投非幂等和并发少扣债务，不将“重复投递只扣一次”写成已通过门禁。匿名公开模式记录既有付款人/限额结果，策略修复另立任务。
6. 运行时与定时：三个 extend beat 均存在且仅一次；月初/日初快照、清零；记忆 context register、历史截取；新 Agent v2 workspace/binding 与迁移后会话恢复；目标新增 TTS/error stream 不导致丢失 message trace 或计费信号。

本报告是源码级升级规划，没有实施、测试或迁移执行结果。
