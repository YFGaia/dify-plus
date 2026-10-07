# Dify-Plus Web 与原生管理后台

Dify-Plus 的应用使用、应用开发和系统管理共用 Dify Web 与 Flask API。系统管理直接位于 Console 中，不需要 GVA 管理前端、Go 管理服务或独立管理账号。

本文介绍当前用户可访问的二开功能。后端计费、身份规则和接口语义见[后端与数据层说明](./二开功能详解-后端与数据层.md)，运行配置见[部署配置与运维说明](./二开部署配置与运维说明.md)。

## 应用中心与模板共享

应用中心位于 `/explore/apps-center-extend`，提供统一的已安装应用入口。应用卡片展示名称、图标、类型与说明，用户可通过“新建对话”进入对应的已安装应用。列表保持服务端的使用量排序，支持分类、标签、名称或说明关键词筛选；同一个应用具有多个标签时，筛选后的卡片按应用 ID 去重。

应用开发者可在工作室应用卡片菜单中“同步到应用模板”或“取消同步”，供模板发现与安装使用。此操作要求当前用户具有工作区管理权限，同时满足二开管理与工作区权限，且已取得应用的同步状态；普通应用使用者不具有模板发布权限。

| 能力 | 当前源码入口 |
| --- | --- |
| 应用中心页面 | `web/app/(commonLayout)/explore/apps-center-extend/page.tsx` |
| 分类、标签、关键词筛选 | `web/app/components/explore/app-list-center-extend/index.tsx` |
| 应用卡片、新建对话 | `web/app/components/explore/app-card-extend/index.tsx` |
| 已安装应用查询与排序保留 | `web/service/use-explore.ts`、`web/service/explore.ts` |
| 模板同步操作与确认 | `web/app/components/apps/app-card/interactions.tsx` |
| 应用目录同步状态 | `web/app/components/apps/app-list-catalog.tsx` |
| 主导航与个人余额 | `web/app/components/main-nav/index.tsx` |

## 企业 SSO 与登录

### Casdoor 统一身份认证

登录页通过服务端公开的 Casdoor 展示接口判断入口是否可用，使用配置的按钮名称发起企业 SSO 登录。授权和回调由后端承接，Web 不读取 Client Secret，也不把授权码或访问令牌作为结果页的展示内容。登录结果页 `/signin/casdoor-result` 提供登录结果与必要提示。

Casdoor SSO 配置支持工作区角色映射，让通过企业认证的用户进入对应工作区。角色映射按工作区选择目标，支持 `admin`、`editor`、`normal`，单份配置最多 100 条映射，不能通过映射赋予 `owner`。

| 能力 | 当前源码入口 |
| --- | --- |
| 普通登录入口 | `web/app/signin/normal-form.tsx`、`web/features/casdoor/signin/entry.tsx` |
| SSO 授权与回调 | `api/controllers/console/auth/casdoor_extend.py` |
| 登录结果 | `web/features/casdoor/signin-result/index.tsx` |
| 工作区角色映射 | `web/features/casdoor/configuration-form/workspace-mappings.tsx` |

### Web App 访问认证

应用的 Web App 访问卡片提供“访问认证”开关，默认开启。拥有编辑权限且 Web App 已运行时可切换；界面只提交 `webapp_auth_enabled_extend` 字段并更新应用详情。

开启时，访问遵循平台账号和应用访问权限要求；关闭可允许匿名访问，但仍需满足 Web App 已发布、可运行等条件。组织成员、指定成员组与外部成员 SSO 采用对应的 Web App 登录流程。登录回跳目标经过校验后才用于导航。

源码：`web/app/components/app/access-point/built-in-access-points/web-app-card.tsx`、`web/service/webapp-auth.ts`、`web/app/(shareLayout)/webapp-signin/page.tsx`。

## 个人余额与应用 API Key 限额

主导航展示个人已用额度、总额度与余额。金额来自 `/account/money`，按系统配置的汇率换算成人民币显示，并对余额较低等情况提示。未取得有效账号、额度或汇率，或者总额度为零时不展示余额组件。

个人额度是 Dify-Plus 的使用余额，不是 Dify 订阅计划额度。管理员设置的是总额度，已用额度保留；余额为总额度减去已用额度。

应用 API Key 弹窗支持：

- 创建密钥时填写用途描述和日、月限额；描述最多 50 个字符。
- 编辑现有应用密钥的描述和限额。
- 查看累计用量、当日用量、当月用量及相应限额。
- 复制或删除密钥，按应用权限控制可用操作。

日、月限额接受非负数，`-1` 表示不限额；`0` 是零额度限制。累计用量 `accumulated_quota` 仅作统计，不是终身消费上限。日、月限额表单适用于应用密钥，不应套用到知识库或环境密钥。

源码：`web/app/components/main-nav/components/account-money-extend.tsx`、`web/app/components/api-key/api-key-modal.tsx`、`web/app/components/api-key/api-key-table.tsx`、`web/app/components/base/param-item/day-limit-item-extend.tsx`、`web/app/components/base/param-item/month-limit-item-extend.tsx`。

## 会话上下文控制

经典聊天应用配置提供记忆上下文数量控制，可用滑杆或数字输入设置保留数量。默认 5，最小 1，最大 20，分别由 `NEXT_CONTEXT_RETENTION_DEFAULT_COUNT`、`NEXT_CONTEXT_RETENTION_MIN_COUNT`、`NEXT_CONTEXT_RETENTION_MAX_COUNT` 控制。界面关闭数量控制时使用后端约定值 `999`。

会话中显示已清除上下文的分界标记，用户可恢复对应分界。上下文记录通过当前 Console 会话查询，匿名 Web App 没有 Console 会话时不会发起受保护的上下文读写。

源码：`web/app/components/app/configuration/retention-number-extend/index.tsx`、`web/app/components/base/chat/chat/index.tsx`、`web/service/message-context-extend.ts`。

## 原生系统管理

系统管理位于 `/system-manage-extend`，默认进入系统集成页。系统管理权限由服务端认定：当前工作区必须是数据库固定关联的初始化工作区，且当前用户在该工作区的实际角色是 `owner` 或 `admin`。其他工作区的 owner/admin 不自动获得全局管理权限。导航、页面布局和后端接口共同检查权限；Casdoor 配置还需要服务端授予 `can_manage_casdoor`。

权限源码：`web/features/system-management/access.ts`、`web/features/casdoor/management-access/use-casdoor-management-access.ts`、`web/app/(commonLayout)/system-manage-extend/layout.tsx`、`api/controllers/console/system_manage_extend.py`、`api/services/system_management_access_service_extend.py`。

### 系统集成

`/system-manage-extend/system-integration` 包含 Casdoor、钉钉和 OAuth2 标签页，标签保存在 URL 的 `tab` 参数中。

| 集成 | 配置与交互 | 当前源码入口 |
| --- | --- | --- |
| Casdoor | 浏览器访问地址、组织、应用、Client ID、Secret；高级后端地址与 issuer；工作区角色映射；保存、测试登录和启用/停用 | `web/features/casdoor/configuration-form/` |
| 钉钉 | 启用状态、Corp ID、Agent ID、App Key、App Secret；保存与连接测试 | `web/app/(commonLayout)/system-manage-extend/system-integration/dingtalk-config.tsx` |
| 钉钉邮箱查询 | 可选查询 URL、GET/POST、用户 ID 参数名、结果路径、认证方式、请求头与请求体；使用指定测试用户 ID 测试邮箱查询 | 同上 |
| OAuth2 | 启用状态、Client ID/Secret、服务器/授权/Token/用户信息/退出地址、scope、按钮名称和回调地址；保存与连接测试 | `web/app/(commonLayout)/system-manage-extend/system-integration/oauth2-config.tsx` |

Casdoor 配置区分草稿和生效版本。保存草稿后可测试登录；启用需要服务端确认指定版本的验证与测试结果。并发修改、保存结果无法确认或读取失败时，页面要求刷新重新核对。Secret 输入为空表示保留既有 Secret，已保存 Secret 不回填到表单。

SSO 回调地址由配置区提供，需与 Casdoor 应用登记的回调地址一致。工作区角色映射为登录用户指定可进入的目标工作区和角色。

### 用户额度管理

`/system-manage-extend/quota-management` 提供按成员名或邮箱搜索的用户列表，展示使用排名、头像、成员、邮箱、已用额度、总额度和余额，金额单位为 USD。支持分页和 10/30/50/100 条每页选择。

管理员可修改用户总额度为非负数，提交成功后刷新列表。额度管理面向账号个人余额，不能理解为工作区订阅计划管理。

源码：`web/app/(commonLayout)/system-manage-extend/quota-management/page.tsx`、`web/service/system-manage-extend.ts`。

### 代码执行控制

`/system-manage-extend/code-execution-control` 管理代码节点完整执行环境授权名单。管理员通过工作区 owner 邮箱新增授权项，查看邮箱和创建时间，并通过确认弹窗删除授权项。后端以运行工作区的 owner 邮箱匹配名单，并据此选择 `sandbox-full` 完整执行端点；页面保存失败或缓存投影未同步时会给出相应提示。

此功能依赖部署中实际配置完整执行环境，添加名单本身不会部署或启动 `sandbox-full`。

源码：`web/app/(commonLayout)/system-manage-extend/code-execution-control/page.tsx`、`web/contract/console/system-manage.ts`、`api/core/workflow/nodes/code/control_extend.py`。

## 功能截图与维护

![应用中心](./images/apps-center.png)

![应用 API 密钥日/月限额](./images/api-key-quota.png)

![Casdoor SSO 配置（已脱敏）](./images/casdoor-configuration.png)

![用户额度管理（已脱敏）](./images/account-quota.png)

![Web App 访问认证（已脱敏）](./images/webapp-access.png)

![代码执行控制（已脱敏）](./images/code-execution-control.png)

功能截图随根目录 [README](../../README.md) 的功能展示维护。截图应来自真实页面，使用演示数据或不可逆遮挡；Casdoor/OAuth2 地址、组织、Client ID、账号、邮箱、密码、Secret、Token 与密钥都需要检查。密码输入框的圆点遮罩不能代替对页面其他敏感字段的检查。

新增功能说明时，应先核对实际路由、生产挂载点和后端权限。仅存在源码文件的旧组件或未挂载组件不构成可用页面。本文以当前可访问入口为范围，Casdoor 说明聚焦 SSO 集成。
