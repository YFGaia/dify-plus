# 1.17.1 扩展兼容契约

## MODIFIED Requirements

### Requirement: extend service 适配显式 session 与 select() 模式

fork 扩展 SHALL 适配目标 1.17.1 的显式会话与服务接口，在有效会话范围读取关联数据并保持既有事务语义。异步计费所需身份 SHALL 在请求结束前确定，不能依赖已经失效的请求上下文。

#### Scenario: 跨请求计费

- **WHEN** 请求完成后异步处理用量
- **THEN** 账号和密钥归因仍正确，没有失效会话或缺少身份导致的新增漏扣

#### Scenario: 事务边界不回归

- **WHEN** 扩展配置或账号创建事务失败
- **THEN** 不留下成功假象、重复额度行或部分提交的关联数据

#### Scenario: 无旧签名调用残留

- **WHEN** 合并后检查扩展对上游的调用并执行导入与定向验证
- **THEN** 不存在因旧调用签名而新增的 TypeError 或 AttributeError，目标会话使用方式正确

### Requirement: extend controller 适配 user/tenant 注入模式

扩展管理与业务接口 SHALL 适配 1.17.1 的账户/租户授权边界，保持当前系统管理权限和租户隔离，并兼容 fork 计费所需的调用身份。

#### Scenario: extend 管理接口可用

- **WHEN** owner、admin、member 和其他租户用户分别调用管理接口
- **THEN** 结果符合本轮冻结的现有权限基线；升级不增加越权入口或因参数注入不兼容而失败

### Requirement: fork 安全防护包装保留

登录配置的双阶段令牌、来源绑定和 Header/Cookie 兼容 SHALL 保留并适配目标公开系统配置。完整 license 信息 MUST 受登录保护；服务端页面不得将健康响应误作完整配置。

#### Scenario: 缺失或失效配置令牌

- **WHEN** 登录配置请求缺令牌、令牌错误或来源绑定不符
- **THEN** 请求被拒绝，不通过兼容路径泄露受保护字段

#### Scenario: SSR 与代理环境

- **WHEN** 服务端渲染或浏览器 Cookie 不可用
- **THEN** 页面使用有效公开快照/允许的配置降级与 Header 协议，不因缺少字段崩溃，也不绕过敏感信息授权

#### Scenario: 防护层存在性检查

- **WHEN** 审查后端登录配置入口和迁入新宿主的前端契约
- **THEN** 双阶段令牌防护在真实调用链仍有效，且与上游公开配置和受保护 license 边界兼容

### Requirement: web 侧 extend 挂载保持有效

fork 登录、应用中心、个人额度、API Key 限额、系统管理和国际化扩展 SHALL 在 1.17.1 实际页面与契约中生效。每应用 WebApp 认证开关 MUST 在新访问入口可操作，并与上游环境 WebApp 作用域隔离。

#### Scenario: 公开 built-in 应用

- **WHEN** 有权限用户关闭某应用的访问认证开关并匿名打开该 built-in WebApp
- **THEN** completion/chat/workflow 可运行，匿名上下文不调用需 Console 会话的接口，其他应用开关不受影响

#### Scenario: 环境 WebApp 与无效地址

- **WHEN** 访问环境 WebApp 或缺失/无效的 built-in 应用标识
- **THEN** 环境地址使用其自身授权，错误 built-in 标识不会被当成公开应用放行

#### Scenario: 前端构建与扩展功能可用

- **WHEN** 用户访问新导航中的应用中心、模板同步、个人额度与系统管理
- **THEN** 真实权限字段、调用与状态刷新有效，24 种语言扩展资源可加载，无丢失挂载或权限扩张

### Requirement: 账号初始化额度逻辑保留

所有实际新账号创建入口 SHALL 创建且仅创建一条正确初始额度记录，已有账号登录或邀请加入工作区 SHALL NOT 重置额度。

#### Scenario: 新账号获得初始额度

- **WHEN** 用户通过 setup、邮箱、OAuth 或邀请创建新账号
- **THEN** 账号和初始额度一致提交且不重复，创建失败则两者不产生不完整状态

#### Scenario: 已有账号登录

- **WHEN** 已有账号再次登录或加入另一个工作区
- **THEN** 保留原额度与使用记录
