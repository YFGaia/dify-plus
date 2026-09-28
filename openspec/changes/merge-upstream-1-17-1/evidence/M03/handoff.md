# M03 实施与交接

## 实施边界

- `AccountService.create_account` 保持上游共同建档边界，在同一个 Session/commit 内加入独立额度 helper。setup、邮箱、OAuth、邀请均调用该边界；已有账号登录/邀请不补写或重置额度。setup 失败仅额外清理本次新建账号的额度，保持上游其余清理步骤。
- `ForkOAuthProviderGateway` 从 active SystemIntegration 行读取并物化配置/密钥，关闭读取 Session 后再调用既有 OaOAuth；支持字符串和 Casdoor dict token、id_token、state 的邀请/语言/时区/回跳字段、旧 token callback。新的 OAuth application service 继续拥有 normalized-email、identity/account claim 锁、workspace admission 和 session 签发。
- 自定义 `oauth2` 使用 active 集成作为 provider 开关。`ENABLE_SOCIAL_OAUTH_LOGIN` 配置描述仅控制 GitHub/Google，默认 false；旧 fork controller 没有该限制。独立 `oauth_admission_extend.py` 只对 oauth2 选择集成准入路径；其余 provider 复用原 social 装饰器。两条路由都保留 setup_required。无配置/禁用集成无法创建 gateway client。
- `AppSiteChangesExtend` 将扩展字段与上游 Site 字段集合分离；repository 沿完整 workspace/app 所属条件加载 Site，并以 Site 行锁串行更新，Site 与 AppExtend 在同一事务内写入，commit 后才失效缓存。
- 开关 NULL/缺行默认需认证；DB 失败默认需认证。Redis 仅为投影，授权决策每次查 DB，避免双向开关切换在失效失败后继续使用旧值。这增加一次索引查询，以换取提交后下一请求语义；key 命名保持不变，投影TTL60秒。
- built-in 状态与三生成入口共用只接受 Console access Cookie 的身份 helper，验证签名/过期/Console API Passport purpose/账户与workspace。WebApp/login/passport bearer 不能冒充 Console。upstream load_user 会提交/关闭其 Session，因此 helper 使用独立 Session，不关闭持有 EndUser 的调用方 Session。
- `completion.py` 仅批准的 `is_end_login` hunk 与相关 imports 改动；`is_money_limit` 和所有生成类 AST 与输入版本一致，计费继续属于 M04。

## 复核修正

Sol 一次性 `/root/m03_contract_review` 提出两项：setup 新增额度清理不可全表删除；Redis 旧 true 缓存会使关闭认证延迟。已限定 created_account_id，并让缓存两种状态都重查 DB。新增 setup late-failure 及双向缓存失效失败测试。独立 Session 另有真实 EndUser 绑定持久化断言。

Luna 分工互不重叠：`/root/m03_site_tests` 仅改缓存service和site repository测试，`/root/m03_identity_tests` 仅改既有WebApp login和switch测试；主实施者复核后补actual setter、commit failure及真实JWT/SQLite矩阵。所有子agent一次性结束，未复用分派新任务。它们是本thread内agent，不是独立App thread，没有工具可用的独立thread id。

## 下一节点消费

- M04：批准的 Console 身份解析 hunk 已完成；billing/memory/Service API保持待实施。保留匿名计费旧规则，勿因新身份helper重选付款人。
- M05/M06：`GET /web/login/status` 四字段 `logged_in/app_logged_in/console_logged_in/webapp_auth_enabled_extend`；fork标记不等于上游passport授权。缺code安全默认true，非法code沿现有ValueError错误，不能当false。Site payload扩展boolean严格类型，null/缺省表示不改，Site响应不添加伪造扩展字段，读取仍由app状态源负责。
- M05/M08：Swagger从最终source生成到临时目录并断言形状；未手改`packages/contracts/generated`。最终TypeScript/Zod/OpenAPI文档由各自节点刷新。
- M06：保留Casdoor `id_token` 回跳参数及已有前端登出消费（token URL规则为既有fork行为）。回跳仍经过同源校验，并正确编码query和保留fragment。
- C06：environment访问走上游enterprise app-deploy/site/transport，`/web/passport`与WebPassportService源码未加入fork开关；其本地现有单测复跑。专有enterprise实现、真实SSO、浏览器Cookie跨域、生产DB和目标镜像必须留A03/V03–V05验收。

## 证据限度

证明Account+quota二表原子性，不宣称与后续workspace provisioning形成跨服务单事务；上游分段提交保持。SQLite覆盖真实Session/提交/回滚，但不证明PostgreSQL行锁并发调度或真实provider网络。测试中的外部模型生成、Redis、provider HTTP为桩；生成前置门禁、签名JWT、Account/EndUser/App/Site/AppExtend读写为真实代码与SQLite。两项现有弃用警告及LiteLLM离线价格表回退单列于日志，未更改锁或仓库虚拟环境。本节点passed仅代表源码节点交付，不可部署。
