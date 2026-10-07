# 当前本地验收批次与最小真实步骤

本批次覆盖24个可本地推进的场景ID；“可推进”不等于整项通过。已有unit/旧镜像/overlay证据仅作为参考，不代替当前完整镜像live验证。业务样本全部为实际持久化的专用隔离测试资源，不用mock/fixture的结果冒充live。

运行执行者独占一个ego-browser TaskSpace及容器操作；API/Web重建期间停止UI写入。先N02完整源码归档→N03完整镜像更新，然后批次1–5。长批次按独立证据增量交付；发现失败交回限定开发，不重跑已通过且hash未变项。

| 场景 | 批次 | 最小真实步骤 | Expected | Readback | 实际样本与参考边界 |
| --- | --- | --- | --- | --- | --- |
| G01/G02/F05 | 0：候选与运行身份 | 完整current-source archive含当前uv.lock、六文件及新测试；录commit/tree和hash；核对实际image/container源码；重用未改变前端build证据 | 所有路径无漏归档；实际容器源码与archive一致；旧candidate仅历史输入 | manifest、image ID/digest/platform；build logs与容器文件hash | 不造功能数据；现有迁移/模型/开发manifest按hash参考，不能代替新镜像 |
| B01 | 1：身份/角色 | 新隔离数据库setup；邮箱/邀请账号流使用本地收件sink或既有安全测试账号；已有账号登录前后读quota | 每个创建账号仅1行额度且金额正确；已有账号不重置；失败不留半条 | accounts/tenant joins与account_money_extend仅脱敏count/余额；请求结果 | 专用owner/admin/member/第二tenant；禁止向真实人员发邀请 |
| B02 | 1：身份/角色 | 测试邮箱大小写、Gmail/googlemail别名冲突与邀请/login行为 | 符合当前normalized_email契约且不串号/误合并 | normalized_email脱敏哈希、账号ID关系/拒绝类型 | 合成账号仅在隔离库；旧数据碰撞审计另D02 |
| B03 | 1：身份/角色 | 先安全盘点provider配置是否启用；已配置SSO做真实code/state/session/API调用；未配置证明关闭/失败路径 | state/同源回跳有效，会话可调用授权API；外部启用态缺账号明确blocked | 配置presence/启用布尔、callback结果与无密钥会话API结果 | 现有真实SSO凭据才可测启用态；mock仅历史参考 |
| B04 | 1：身份/角色 | logout后Cookie/Header两种login_config链路；验证缺token/错签/IP错和SSR | 公开bootstrap无license泄露；受保护配置拒绝无效请求；正确双阶段可保存 | 脱敏network请求状态与response allowlist；刷新后SSR实际页面 | 新的隔离浏览器上下文；不录token/cookie值 |
| F03 | 1：身份/角色 | owner/admin/member分别看入口、直接访问URL、调用系统API | 与当前baseline角色边界一致；无入口不等于API拒绝 | 三角色UI/URL/API三列结果，Forbidden/成功字段 | 专用角色账号；不改现有owner角色 |
| F04 | 1：身份/角色 | 专用quota编辑分页、forward token/代码执行控制create-read-delete；集成配置测试按真实配置presence | 保存刷新真实回读，角色拒绝成立；外部测试不伪造 | 脱敏record ID/counters与删除后不存在、接口结果 | 临时资源实际创建后清理或保留标注；集成缺真实账号单列 |
| F01 | 2：发布/安装中心 | 完整新image复验旧缺stats+已publish/installed app卡片和打开；新专用chat/chatflow/workflow发布后中心显示；分类/搜索/排序/去重/Home | 无需DB回填旧app即可显示和打开；草稿/未安装不扩入产品；筛选结果与installed ID正确 | UI卡片app/installed IDs、实际API rows、stats只读count、打开结果 | 现有Quota smoke chat只读复验；新增用途专用App；旧image失败仅前证 |
| F02 | 2：发布/安装中心 | 有权限测试账号sync/cancel模板并刷新/重开；普通成员尝试 | 角色组合有效且同步/取消/缓存真实刷新；member API拒绝 | recommended_apps行变化/页面状态；拒绝结果 | 专用published app；不要同步/取消用户现有应用 |
| B05 | 2：发布/安装中心 | 访问认证on/off×登录/匿名×completion/chat/workflow共12实际组合；错误code/跨app | on拒匿名、off可生成；code错误拒绝；跨app不串会话 | 真实生成与登录页面/API错误结果、消息/运行归属 | 专用各模式App和匿名浏览器；有限短LLM请求 |
| B06 | 2：发布/安装中心 | WebApp启用与访问认证分别只改单字段，刷新验证；environment配置关闭或已启用passport/logout/401 | 两开关独立缓存正确；环境认证仍按目标scope | app/site/AppExtend脱敏布尔、实际刷新与passport结果 | 専用App；环境未启用须有关闭有效证据 |
| B11 | 2：发布/安装中心 | NULL/无配置/仅认证开关App及有限retention登录多轮；匿名观察context network；跨租户context GET/DELETE | 创建正常，多轮截断；匿名不发Console context；跨租户404无读写 | AppExtend值、context markers数量与请求路径/状态 | 专用会话、合成3轮；不得删除用户现有上下文 |
| B07 | 3：额度/key/计费 | 真实Console、Explore和登录WebApp短请求；读价格currency/token/actor及quota before/after，USD与RMB路径 | 非零已记录price与used_quota增量对应；trusted account一致；匿名付款人保持baseline | message/run/node usage，account join、quota delta和汇率读回 | 现有DeepSeek凭据可用；零价须定位provider模型报价首断点，不以tokens充扣费 |
| B08 | 3：额度/key/计费 | 专用key五模式chat/agent_chat/completion/chatflow/workflow调用；-1 unlimited、有限日/月拒绝与跨tenant | 成功有token→message/run join；有限边界拒绝且不派发；累计usage不是lifetime cap | api_token_money_extend日/月/累计before/after与request result | 专用masked key，仅内存使用；模式未启用给真实关闭态；无密钥入日志 |
| B09 | 3：额度/key/计费 | 专用workflow LLM完成/停止/恢复/retry；读node usage及账目 | 合并不引入派发/归因/扣费绕过，停止保留用量；既有非幂等P4明确标注 | run/nodes/message关联，任务事件与quota delta | 有限请求，禁止重投用户旧holding任务；不宣称原P4已修复 |
| K01 | 3：额度/key/计费 | app key quota create-edit-delete；dataset key bound/unbound；environment无契约UI | 关联行和mask/权限正确，dataset key原支持边界保持；无误导环境额度输入 | key/额度record counts和scope，UI回读/删除 | 专用key只录ID与presence；不输出secret |
| B10 | 4：任务/服务边界 | 检查三beat注册唯一和queue；隔离专用时钟/数据验证月初/每日reset | 日/月/个人reset覆盖正确且所有队列消费者存在 | schedule注册摘要、隔离reset前后账目/快照/queue消费 | 必须新隔离scheduler项目；不能对共享测试库全量reset |
| O01 | 4：任务/服务边界 | 启动明确完整拓扑，检查nginx/API/Web/plugin/worker/queue真实任务、自动迁移false | 无新502，必要所有task queues消费者覆盖；新image实际运行 | image list、入口network、queue消费与运行ID | 现workflow-only消费者只是参考；registry供货缺项单列 |
| O02 | 4：任务/服务边界 | 安全读Agent/协同/归档/Human Input启用态；已启用按文件/工具/暂停续跑/归档readback实际运行 | 启用态真实成功；关闭态证明关闭且未破坏其他服务 | feature flags、WS101、实际任务/文件/归档或disabled拒绝 | Agent外部密钥缺项精确列出，不得凭feature=false当启用态通过 |
| O03 | 4：任务/服务边界 | 通用允许host、拒绝host及Agent允许文件/stub、拒任意内网分别做受控请求 | 允许成功/禁止拒绝；Agent规则不扩大通用allowlist | 实际proxy响应/日志脱敏host类别和status | 新可控本地probe endpoints，不探测无关用户网络 |
| D01 | 5：知识库/数据安装 | 完整新image在新PG/MySQL各全链并setup，省略ID插入quota模型实际读写/rollback | 双heads目标一致，真实新账号数据正常；无需overlay | DB heads、表/默认值、模型ID presence、读写结果 | 独立新项目/卷；旧PG与MySQL失败现场保留 |
| D05 | 5：知识库/数据安装 | 启受支持vector，专用小文档upload→index→retrieve引用实际文本→delete/rebuild；文件读取 | 知识库可真实写/查/删除重建；检索引用匹配输入 | dataset/document/task/index status、vector对象count/查询结果 | 现无dataset/vector，需新专用样本与向量配置；旧vector逐站升级另scope |

## 批次6：本地一致恢复与交付（N06）

对本地PG/向量/文件/plugin存储及Redis所有写入者采取一致停写；备份可读并记同一恢复点，产生专用样本写入后恢复，重启完整精确镜像并核对登录、额度、App、知识库引用、文件和队列。保留holding任务/无TTL/hash，不擅重投旧debug请求。记录RTO/RPO与新增样本写入损失。这里接受的是新安装环境恢复，原O04旧版本整套恢复仍延期，不勾历史O04。

## 划分外部缺项

B03每个真实SSO provider、F04钉钉/OAuth2/Email外部测试、O01私有registry供货须按已配置/未配置与缺什么真实条件分别记录。可以先完成本地UI/API权限和disabled/error路径，不能将其转换成全部启用功能通过。已提供的DeepSeek调用直接推进；0USD价格是待查业务断点，不自动归类外部凭据阻塞。


## 具体子项补充与分派

详见`../acceptance-ledger-2026-09-30.md`末尾coverage子项表。app_center独立权限/双engine执行者负责四model省略ID实际flush/readback/rollback与多账号quota分页权限；runtime负责tag CRUD绑解/category/cache、key真实生命周期、已支持Tool node配置save/refetch/publish/run、必要workflow与extend consumers真实投递以及KB。四model未复现前仅coverage hole；独立角色栈与原用户模型工作区分开，容器重建/同浏览器写操作仍串行。
