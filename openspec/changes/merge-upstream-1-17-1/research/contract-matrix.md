# A04 跨层行为与接口契约矩阵

基线：本地 fork 源码树 `ec94659769330e7ae253add9127534af099f5b83`（A04 输入 HEAD `2a2482c330091755be23da2ec35788bddb82751c`）；目标 tag `1.17.1` = `8387590ace4a094de812b7847fc6a4c3a27cd52b`。本矩阵冻结 [design.md D3–D5](../design.md) 的**实施目标**，不是已合并代码或业务验收结果。下面的“目标”路径均指 `git show 1.17.1:<path>` 可读的原始上游文件；fork 路径指 A04 输入树。每行只有一个实施 owner，其他节点按契约消费、联测。

约定：Console API 前缀 `/console/api`，Web API 前缀 `/web`；HTTP 状态、字段和权限以目标源码为准。`null`/缺失字段不得被 UI 解释为授权或额度能力。成功写入后的缓存失效属于交付条件；失败必须保留旧状态。M02–M06 在真实合并结果上逐字段确认；源文件路径只用于定位，不能充当行为验收。外部业务负责人或环境负责人尚未签字，本矩阵不声称其签收。

## C01 · 预登录 bootstrap 令牌

- **请求 / 响应 / 错误**：`GET /console/api/login_config_bootstrap` 无登录态，返回 `{ok:true,token:<JWT>}` 并写 HttpOnly、SameSite=Lax、1 小时 Cookie；签名 HS256，载荷含来源 IP 与过期时间。缺令牌、伪造、过期或来源 IP 不符在后续 `GET /login_config` 返回 403；不能从 public snapshot 取得这个受保护响应。
- **身份权限 / 缓存事务**：仅给预登录客户端签发短期配置令牌，不代表 Console 登录授权；浏览器可带 Cookie，跨域 Cookie 不可用时用 `X-Login-Config-Token`。令牌只保留于当前浏览器 transport 内存，SSR 不持久化或跨请求复用；无数据库写入。
- **fork → 目标**：`api/controllers/console/feature.py::{_issue_login_config_jwt,_verify_login_config_token,LoginConfigBootstrapApi}`、`web/service/client.ts`、`web/contract/console/system.ts` → 目标 `api/controllers/console/feature.py` 的 public feature 基础上保留 fork 路由；transport 迁至 `web/service/console/{browser,contract-loader}.ts`，fork 自有 contract 段并入 `packages/contracts/console.ts`，不改 generated 文件。
- **唯一 owner / 可观察验收**：**M05**。无 Cookie 的跨域 Header 成功，Cookie 路径成功；缺失、过期、错误签名、IP 改变均 403；浏览器登录前后与 SSR 并发请求不串令牌。M02 配套实现后端校验，M05 拥有跨层协议验收。

## C02 · 公开 system-features 快照

- **请求 / 响应 / 错误**：`GET /console/api/system-features` 未登录可读，返回目标 `SystemFeatureModel` 的真实公开字段（例如 `branding`、登录方式、`license.status`），不是 fork 当前 `{ping:true}`。网络错误可在 optional SSR 路径返回未定义/安全默认，但 `{ping:true}` 和缺核心形状的 200 必须视为无效，不可进入配置缓存。
- **身份权限 / 缓存事务**：公开快照只含上游允许预登录公开的字段；不得出现 `license.expired_at/workspaces/seats`。浏览器与 SSR 共用同一语义，SSR 的 request cache 不得把一个用户的 fork 配置或令牌传给另一请求；公开查询键与登录后完整 license 查询键分开。只读。
- **fork → 目标**：fork `api/controllers/console/feature.py::SystemFeatureHealthApi`、`web/features/system-features/{client,server}.ts` → 目标 `api/controllers/console/feature.py::SystemFeatureApi`、`api/services/system_feature_service.py::get_public_system_features`、`api/services/entities/feature_entities.py::SystemFeatureModel`、`web/features/system-features/{client,server}.ts`。
- **唯一 owner / 可观察验收**：**M02**。匿名请求返回有 `branding` 的真实形状且仅有 license status；SSR optional 失败不崩溃，hard 读取不吞掉必须呈现的错误；登录页不依赖假健康响应。M05 负责消费与缓存联测。

## C03 · fork login_config 与公开快照合成

- **请求 / 响应 / 错误**：在 C01 成功后 `GET /console/api/login_config` 带 Cookie 或 Header，返回公开快照所需字段加 fork 登录扩展：`is_custom_auth2`、按钮/登出地址、钉钉标志与客户端/企业 ID、`rmb_to_usd_rate`。403 时不得从兼容回退取得受保护字段；字段缺失只用明确安全默认，不能把 `{ping:true}` 当完整配置。
- **身份权限 / 缓存事务**：预登录令牌仅允许公开登录配置；当前 fork `FeatureService.get_system_features(is_authenticated=...)` 的完整 license 字段必须按 C04 分离。浏览器先 C01 后 C03，与 C02 按字段合成；同名字段以上游公开快照为基础、fork 扩展单独命名。身份变化和配置更新后失效相应 query，不能跨登录态缓存敏感对象。只读。
- **fork → 目标**：fork `api/controllers/console/feature.py::LoginConfigApi`、`api/services/feature_service.py`、`web/features/system-features/{client,extend}.ts`、`web/service/console-router-loader.ts` → 目标 `api/controllers/console/feature.py`、`api/services/system_feature_service.py`、`web/service/console/{browser,server,contract-loader}.ts`、`packages/contracts/console.ts`；fork 自有 contract 不进入 generated。
- **唯一 owner / 可观察验收**：**M05**。登录页在 Cookie 与 Header 路径显示相同有效选项，RMB 汇率可读；无效 bootstrap 返回 403 且不泄露 license 详情；SSR 仅用 C02 的有效公开形状，客户端随后合成 fork 字段；不同账号/登出后不残留旧 fork 配置。

## C04 · 完整 license 隔离

- **请求 / 响应 / 错误**：`GET /console/api/system-features/license` 返回目标 `LicenseModel` 的 status、到期、workspace 与 seat 用量；无有效 Console 账号 admission 时按上游鉴权拒绝。`/system-features` 和 `/login_config` 仅给预登录所需 status，不能透出完整详情。
- **身份权限 / 缓存事务**：由目标 `@console_account_admission()` 保护，不能凭 C01 的 JWT 获得完整 license；license 查询缓存按当前账号/工作区及登录态隔离，登出清理。只读。
- **fork → 目标**：fork `api/services/feature_service.py::_fulfill_params_from_enterprise` 以 `is_authenticated` 控制详情；目标 `api/controllers/console/feature.py::SystemFeatureLicenseApi`、`api/services/entities/feature_entities.py::{LicenseStatusModel,LicenseModel}`。旧单个 SystemFeatureModel 中含完整 license 的形状不得迁成公开响应。
- **唯一 owner / 可观察验收**：**M02**。匿名、仅 bootstrap 令牌均无法读到 expiry/seat/workspace；已登录且通过 admission 可读完整详情；序列化与生成契约一致。

## C05 · built-in WebApp 访问矩阵

- **请求 / 响应 / 错误**：`GET /web/login/status?app_code=<code>` 回 `console_logged_in`、`webapp_auth_enabled_extend` 等现有状态；built-in completion/chat/workflow 生成请求：`AppExtend.webapp_auth_enabled` 为 `NULL/true` 且无 Console 登录时 401，`false` 时按上游公开 WebApp 会话继续；有 Console 登录时按既有权限继续。缺失/非法 `app_code`、应用不存在和配置读失败不得被当成 `false` 放行；前端显示不可用/登录状态，后端同样拒绝未授权生成。
- **身份权限 / 缓存事务**：fork 开关只管 built-in WebApp 的 Console 登录门禁，不替代上游 enterprise WebApp passport/访问控制。读缓存键 `webapp_auth_enabled_extend:{app_id}`，DB 不可读时默认需认证；写入后的失效按 C07。`false` 匿名路径不得调用 Console 的 `/message/context`。
- **fork → 目标**：fork `api/services/webapp_auth_service_extend.py`、`api/controllers/web/{login,completion,workflow}.py`、`web/service/webapp-auth.ts`、`web/app/(shareLayout)/components/authenticated-layout.tsx` → 目标 `api/services/web_passport_service.py`、`api/controllers/web/{passport,login,completion,workflow}.py`、`web/app/components/app/access-point/built-in-access-points/web-app-card.tsx` 与新 WebApp transport；保留 fork switch 与三个生成端点检查。
- **唯一 owner / 可观察验收**：**M03**。以 `NULL/true/false × Console 登录/未登录 × completion/chat/workflow` 跑矩阵，逐 app 隔离；非法 code、Redis/DB 失败不匿名放行；off 的匿名 WebApp 可生成且无 Console context 401。M06 检查新卡片回填与文案。

## C06 · environment WebApp 独立授权

- **请求 / 响应 / 错误**：environment 地址、访问模式和启用状态走目标 enterprise app-deploy/environment site 契约；`GET /web/passport` 按 `X-App-Code` 与目标 `WebPassportService.issue` 产生 `access_token`，缺 code/无效 token 401，应用或用户不存在 404，需登录时按 WebAppAuthRequired 路径处理。fork `webapp_auth_enabled_extend` 不添加到 environment 请求或响应。
- **身份权限 / 缓存事务**：遵守目标 `PUBLIC/INTERNAL/EXTERNAL` passport 与 environment 访问控制；不能用 built-in 的 Console 登录开关覆盖 environment。环境 site/subjects 查询按 `{app_id,environment_id}` 缓存及失效；不与 built-in app_code 状态共用 key。只读验证路径。
- **fork → 目标**：fork 没有 environment→AppExtend 作用域映射；目标 `api/services/web_passport_service.py`、`api/controllers/web/passport.py`、`web/app/components/app/access-point/deployed-environment-access-points/{environment-web-app-card,environment-access-control}.tsx`。
- **唯一 owner / 可观察验收**：**M06**。同一应用 built-in 开关变动不改变 environment 的访问结果；environment public/internal/external 各按其 passport 身份返回成功或 401/404；错误环境地址不回退至 built-in 公开状态。

## C07 · App Site fork 字段、事务与缓存

- **请求 / 响应 / 错误**：`POST /console/api/apps/{app_id}/site` 可选 `webapp_auth_enabled_extend:boolean|null`，`null` 表示不改；标准站点字段仍按目标 `AppSiteUpdatePayload`→`AppSiteChanges`。响应保持目标 `AppSiteResponse`，fork 开关由应用详情/登录状态各自回填；无权限 403、应用/站点不存在 404、字段非法 4xx，事务失败非成功响应。不能把 fork 字段传入上游 `AppSiteChanges(**model_dump())`。
- **身份权限 / 缓存事务**：沿用目标 `console_account_admission` 的 owner/admin/editor 与 `APP_RELEASE_AND_VERSION`、Agent access point RBAC；`Site` 与 `AppExtend` 在**同一会话、同一提交**成功或回滚，成功提交后删除 `webapp_auth_enabled_extend:{app_id}` 并刷新 UI 的 app/site/status 查询。当前 fork 在提交前删 Redis 有旧值回填窗口，目标实现需关掉该窗口；缓存失效失败须可见并防旧值持续授权。
- **fork → 目标**：fork `api/controllers/console/app/site.py`、`api/services/webapp_auth_service_extend.py`；目标 `api/controllers/console/app/site.py::{AppSiteUpdatePayload,AppSite}`、`api/services/app_site_service.py::{AppSiteChanges,AppSiteService}` 及其 store；UI 新 built-in access card。
- **唯一 owner / 可观察验收**：**M03**。只改普通 site、只改 fork 开关、两者同改、非法 payload、DB 提交失败均断言响应/两表一致；成功后下一次读与重载均见新开关，失败 UI 不显示假成功；无双重路由或额外字段 TypeError。

## C08 · workspace summary 权限字段

- **请求 / 响应 / 错误**：登录后 `GET /console/api/workspaces/current/summary` 返回上游 `id,name,role,plan,credits`，加 fork `admin_extend:boolean`、`tenant_extend:boolean`；归档工作区 409，未登录/无工作区按目标 admission 拒绝。布尔值必须真实计算，不以缺失值或前端默认 `false` 掩盖后端未输出。
- **身份权限 / 缓存事务**：`admin_extend` 来源于当前账号是否 fork 超管 ID；`tenant_extend` 来源于当前租户是否 fork 超管租户 ID。保留当前系统管理前端 owner-only 入口与现有后端 API 装饰器，不因这两个位扩权。summary query 按账号及当前 workspace 隔离，切换 workspace、登录/登出后失效；只读。
- **fork → 目标**：fork `api/services/workspace_service.py::get_tenant_info`、`api/controllers/console/workspace/workspace.py::TenantInfoResponse`、前端旧 workspace normalizer；目标 `api/services/workspace_service.py::get_current_workspace_summary`、`api/controllers/console/workspace/workspace.py::CurrentWorkspaceSummaryResponse`、`web/service/console/*` 与 workspace state。
- **唯一 owner / 可观察验收**：**M02**。同一账号切换普通/超管租户、超管/owner/admin/member 分别请求 summary，两个位与真实 ID 比较一致；新字段进入前端 contract/atom，系统管理可见性维持旧角色语义，归档 409 不被吞。M05/M06 消费。

## C09 · app API Key 额度

- **请求 / 响应 / 错误**：`GET/POST /console/api/apps/{resource_id}/api-keys`、fork `PUT` 配置及 `DELETE .../{api_key_id}` 保留 app key 的 `description, accumulated_quota, day_limit_quota, month_limit_quota, day_used_quota, month_used_quota`；老 key 无额度行有明确默认值，`-1` 代表不限。超 10 key 400、无权限 403、错 app/key 404。目标 app/agent key 的既有列表展示方式不被 dataset reveal-once 规则误改。
- **身份权限 / 缓存事务**：先做目标租户过滤、`APP_RELEASE_AND_VERSION`/Agent access point RBAC，再查询额度；创建 key 与额度行一致提交，删除前处理 fork 额度软删/关联清理并失效 `ApiTokenCache`；PUT 只接受本租户 key 且 owner/admin 保持现有修改权。列表/个人余额查询按租户与资源失效；Cloud credits 与个人额度分开。
- **fork → 目标**：fork `api/controllers/console/apikey.py`、`api/models/api_token_money_extend.py`、`api/controllers/service_api/wraps.py`、旧 API Key UI；目标 `api/controllers/console/apikey.py`、`web/app/components/api-key/{api-key-modal,api-key-table}.tsx`、`web/features/agent-v2/agent-detail/access/components/agent-api-key-modal.tsx`。
- **唯一 owner / 可观察验收**：**M04**。app/agent key 增删改、旧 key 兜底、日/月拦截及 token 归因可对账；不同租户和角色 403/404 不泄露 key；失败不留孤儿 quota，删除后缓存不可继续认证。M06 对照统一 UI。

## C10 · dataset API Key scope 与额度显示

- **请求 / 响应 / 错误**：目标 workspace 级 `GET/POST /console/api/datasets/api-keys`，创建 body `{dataset_ids:string[]}`；空/缺失表示该租户全库，N 个 ID 表示仅 N 库，重复 ID 去重，非法/跨租户 ID 400；列表 `ApiKeyItem.dataset_ids` 和**遮罩** token，创建响应只显露一次完整 secret。已有 fork 额度字段只在后端确有可用额度联查/保存/限制时进入 UI；若 dataset quota 未实现，隐藏额度输入，不伪造成功。
- **身份权限 / 缓存事务**：目标 admin/owner + `DATASET_API_KEY_MANAGE` 与租户过滤保留；service API 对 bound key 的无 dataset_id 或越范围请求 403，scope 每请求从绑定表读取。创建 token/binding（以及实际支持的 fork quota）同事务；删除失效 token cache，绑定 FK cascade 与 fork quota 生命周期对齐；列表 query 创建/删除后失效。
- **fork → 目标**：fork `api/controllers/console/{apikey.py,datasets/datasets.py}` 的额度与旧 per-dataset 路径；目标 `api/controllers/console/datasets/datasets.py::DatasetApiKeyApi`、`api/controllers/service_api/wraps.py`、`api/controllers/console/apikey.py::build_masked_api_key_list`、`web/app/components/api-key/{api-key-modal,api-key-table}.tsx`。
- **唯一 owner / 可观察验收**：**M04**。空 binding 全租户、bound N 库、越租户/无 dataset ID 403/400、创建 reveal-once、列表遮罩、删除级联与 quota 清理均逐项可观察；UI 仅显示已由 API 确证可保存的额度字段。M06 验统一 UI。

## C11 · environment API Key 不扩展未定义额度

- **请求 / 响应 / 错误**：目标 enterprise environment key 的 list/create/delete 继续使用 `{app_id,environment_id}`；其 DTO 无 fork `ApiTokenMoneyExtend` 的创建、编辑、扣费契约。本轮 UI 不显示或提交 description/日月额度输入；环境 key 请求失败沿目标错误处理，不假称额度已保存。
- **身份权限 / 缓存事务**：目标 environment access service 的授权和所属环境隔离；创建/删除后失效该环境 key query。没有 fork quota 事务可推定，不借 app/dataset key 端点操作 environment key。
- **fork → 目标**：fork 没有 environment key fork 额度入口；目标 `web/app/components/api-key/api-key-modal.tsx` 的 `scope.type==='environment'` 与 `packages/contracts/enterprise-app-deploy` 合约。
- **唯一 owner / 可观察验收**：**M06**。environment modal 可按目标 API 列/建/删 key，页面无无效额度输入，请求 payload 无 fork quota 字段，环境 A 的 key 不出现于环境 B；错误时不显示创建成功。

## C12 · 实际新账号同事务一次建额度

- **请求 / 响应 / 错误**：setup、邮箱注册、OAuth 首次注册、邀请导致**新 Account 行**时创建且仅创建一条 `AccountMoneyExtend(account_id,used_quota=0,total_quota=ACCOUNT_TOTAL_QUOTA)`；现有账号登录、OAuth 重登、邀请加入新 workspace 不重置额度。邮箱别名冲突、seat 限制、禁用注册及建档失败沿各上游错误返回，无半成功账号/额度。
- **身份权限 / 缓存事务**：选实际共用账户创建边界，保持上游 normalized-email、账户锁及各入口 admission；账号行与额度行同一 Session/commit，依 account_id 唯一性幂等，移除旧 `RegisterService.setup/register` 的重复写。目标 `create_account` 当前在内部 `session.commit()`，M03 必须调整提交边界或在等价原子创建边界写额度，不能只在外围 `create_account_and_tenant` 之后补写。无跨账号缓存。
- **fork → 目标**：fork `api/services/account_service.py::RegisterService.{setup,register}`；目标 `api/services/account_service.py::{create_account,create_account_and_tenant}`、`api/services/account_email_registration_adapters.py::AccountServiceRegistrationGateway.create`、`api/repositories/account_oauth_repository.py::AccountServiceOAuthAccountRegistrationGateway.register` 及邀请入口。
- **唯一 owner / 可观察验收**：**M03**。四个创建入口的 Account/AccountMoneyExtend 一一对应，重复登录或邀已有账号后原额度与使用量不变；在额度写入故障注入时 Account 不落库；邮箱别名/seat 拒绝时均无额度残留。

## C13 · 匿名计费保持基线，不重选付款人

- **请求 / 响应 / 错误**：built-in 公开 WebApp 的 completion/chat/workflow 请求仍走当前 `is_money_limit(end_user)` 前置与 `message_was_created` 后置计费。匿名无额度行时前置当前返回未超限；后置优先 Account、再 `EndUserAccountJoinsExtend` 映射，均无则用 end_user UUID 建/计额度。数据库读取异常当前前置按超限拒绝；升级不得静默放宽。
- **身份权限 / 缓存事务**：仅按现有 end_user/account/token 归因，不能未经产品决定把应用 owner 指定为匿名付款人。消息 signal 的提交与 API token join 时序保持基线；异步任务在请求结束前物化所需 ID。P4 的并发读改写、重复派发、幂等和匿名归因缺口是已知债务，不写成 exactly-once 已实现；本轮只对照升级前后结果。
- **fork → 目标**：fork `api/controllers/web/completion.py::is_money_limit`、`api/events/event_handlers/update_account_money_when_messaeg_created_extend.py::handle`、`api/controllers/service_api/wraps.py`；目标生成/消息/工作流服务和 signal 新路径由 M04 实际 merge 时核定，旧 hook 不能仅凭文本自动合并判通过；参考 `research/backend-analysis.md` §2 十挂点。
- **唯一 owner / 可观察验收**：**M04**。Console、已登录 WebApp、匿名公开 WebApp、Service API 的付款 ID、token join、金额、拦截与基线对账；INITIAL/RESUMPTION/retry 不新增派发。重复 Celery 投递仍有基线风险，单列 P4，不以一次成功样本证明幂等。

## C14 · 匿名上下文与 P4/P6 边界

- **请求 / 响应 / 错误**：匿名公开 WebApp 对话可运行；无 Console `csrf_token` 时前端不请求 `/console/api/message/context`，也不因该接口 401 强制跳登录。已登录上下文和 `retention_number` 继续按 fork 当前路径工作；`AppExtend` 行仅因开关存在、retention 为 NULL 时跳过记忆分割。配置/会话错误不得把匿名会话误认为 Console 用户。
- **身份权限 / 缓存事务**：Console context 仍需 Console 身份；WebApp 的匿名 passport 与 memory 分开。context/retention 的缓存与会话沿当前 fork 行为；P4 计费硬化和 P6 管理 UI 标准化仍是独立未实施 change，不把拟议 contextvar、扣费幂等、admin 可见性扩张或新 contract/query 架构写为本轮既成能力。
- **fork → 目标**：fork `web/app/components/base/chat/chat/index.tsx` 的 `csrf_token` 守卫、`api/core/app/apps/base_app_runner.py`、`api/core/memory/token_buffer_memory.py`、`openspec/changes/{p4-billing-quota-hardening,p6-console-manage-standardization}`；目标 chat UI/Agent v2 新宿主与现有 memory 管道需在 M04/M06 分别复核。
- **唯一 owner / 可观察验收**：**M06**。匿名 off 模式多轮聊天无 Console context 请求与 401 跳转；已登录 context 增删及 retention 生效，NULL 不报错；系统管理仍按旧 owner-only 前端守卫，P4/P6 状态未被本次升级误标通过。M04 负责后端 memory 调用链。

## 交接门槛

- C01–C04：M02 和 M05 对同一 public/login_config/license 字段表及错误状态联测；`/system-features` 公开快照与 fork 配置有清晰来源，license 详情只有受保护入口。
- C05–C07：M03 和 M06 联测 built-in 与 environment 独立矩阵；App Site 写入必须证明原子提交与提交后缓存失效。
- C08–C12：M02/M03/M04/M05/M06 按各行唯一 owner 实施；真实响应、前端 contract、权限和数据库行数同看，不能只用类型或 fixture。
- C13–C14：用现有行为作回归基线；P4/P6 与匿名付款人规则留独立变更。真实业务账户/部署数据验收属于 A03 后的 V05，当前没有该授权或证据。
