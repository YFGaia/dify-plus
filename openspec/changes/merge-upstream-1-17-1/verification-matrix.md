# 验收矩阵与证据契约

> 此文件定义完整原验收契约，当前逐项状态见 [29场景机器台账](acceptance-ledger-state-2026-09-30.json)。原历史源码检查与28.x source-overlay保持当时边界；最新HEAD-lock445d完整镜像、原23000最新UI及一致快照恢复已有真实证据。原provider生成/retention/停止补包与尚未满足的计费、Agent、外部、旧版本和生产要求逐项记录，不由历史或子集通过替代。

每项证据记录：场景 ID、候选 commit/tree、镜像 digest、配置摘要、DB 起止 head、UTC 时间、输入（脱敏）、实际结果、预期、执行者和失败归属。真实凭据不得入库。原有缺陷标 `known-baseline-defect`，新增失败标 `regression`，缺环境标 `blocked`；条件不适用须写原因与环境证据，不能空白划过。

| ID / 节点       | 场景                                                     | 通过标准与必要证据                                                                                                     |
| --------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| G01 M08         | 91 个预测及全部实际冲突                                  | 每项处置和新宿主记录；索引无 U，代码无冲突残留，无保留但无人调用的旧卡片/client                                        |
| G02 M08         | 四个 tag 后提交                                          | 发布/构建调整仍在；每应用认证开关和匿名上下文修复均有实际场景对应                                                      |
| B01 V01/V05     | setup、邮箱、OAuth、邀请、已有账号                       | 每个新账号仅一条初始额度，金额正确；已有账号不重置；失败事务不留半条数据                                               |
| B02 V01/V05     | normalized_email 同义邮箱                                | Gmail/googlemail别名和大小写碰撞行为符合目标；历史重复账号不误合并、登录/邀请不串号                                    |
| B03 V05         | GitHub/Google/OAuth2/Casdoor/钉钉                        | code/fallback/id_token/邀请/未配置/失败路径；state和同源回跳有效；返回的会话可实际调用授权 API                         |
| B04 V01/V02/V05 | login_config 双阶段 + SSR                                | Cookie与Header两路径，代理/Cookie不可用仍可配置；缺失/错签/IP不符拒绝；SSR不缓存ping作配置；预登录无完整license泄露    |
| B05 V05         | built-in WebApp 矩阵                                     | on/off × 登录/匿名 × completion/chat/workflow共12组合；on阻挡匿名、off可实际生成；缺失/非法code拒绝匿名；跨应用隔离    |
| B06 V02/V05     | 两个开关与环境地址                                       | 启用/停用WebApp与访问认证独立；单字段保存、刷新与缓存正确；environment passport/logout/401恢复沿用目标作用域           |
| B07 V01/V05     | Console调试/Explore/WebApp登录计费                       | 真模型用量、RMB/USD、账号和消息对账；公开匿名另记录基线缺陷，不能造付款人规则                                          |
| B08 V01/V05     | Service API chat/agent_chat/completion/chatflow/workflow | 每条token→message/run join可追；日/月/总额边界与-1无限额正确；签名无TypeError；跨租户拒绝                              |
| B09 V01/V05     | workflow LLM计费/恢复/停止/retry                         | 新合并不新增派发、归因丢失或扣费路径绕过；停止用量保留；既有Celery非幂等和并发少扣另记P4，不宣称已解决                 |
| B10 V01/V05     | 月初/每日重置                                            | 三项beat注册唯一、开关与队列有效，受控时钟/隔离数据演练快照与重置，禁止在生产触发真实全量重置测试                      |
| B11 V01/V05     | 记忆retention和匿名context                               | NULL/无配置/仅认证开关创建AppExtend均正常；登录多轮截断；匿名不发Console context请求，无NameError                      |
| K01 V01/V02/V05 | app/dataset/environment key                              | 已支持额度create/edit/delete与关联行对应；dataset bound/unbound、遮罩与租户权限保留；environment无契约时无误导额度输入 |
| F01 V02/V05     | 默认落点、应用中心、上游Home                             | 无redirect进fork应用中心；显式合法目的地保留；分类/标签/搜索/排序/去重与installed id打开有效，Home仍可访问             |
| F02 V02/V05     | 模板同步                                                 | 新workspace summary权限位真实；manager+fork权限组合正确，新卡片同步/取消状态和缓存刷新；普通成员拒绝                   |
| F03 V02/V05     | 系统管理权限                                             | 分owner/admin/member：入口、URL、API分别记录与当前基线一致；不借此次合并扩大权限；已存在UI/API差异单列                 |
| F04 V05         | 系统管理业务                                             | 额度分页编辑、钉钉/OAuth2/Email API配置和测试、forward token、代码执行控制增删；真实回读且角色边界成立                 |
| F05 V02         | UI/契约/工具链                                           | pnpm frozen、check、tss、i18n与定向unit/browser；24 locale extend加载；Next/Vinext双build；目标产物真正进入镜像        |
| D01 V03         | 空库双链                                                 | 全主链和extend链从零成功；版本表、扩展表、setup/新账号可运行                                                           |
| D02 V03         | 存量双链                                                 | 实际DB引擎副本17项，两个head正确；并发索引/部分提交可诊断；旧extend018到019正确                                        |
| D03 V03         | 破坏性Agent变更                                          | 删表/JSON前后计数及业务数据清单；业务保留需求已处理或书面接受迁移影响；不可用downgrade冒充恢复                         |
| D04 V03         | 模型凭据去重                                             | 五表重复组、胜出行、引用重写、删除记录脱敏对账；存储密文仍可用原密钥解密并真模型调用                                   |
| D05 V04/V05     | 向量数据                                                 | 每站schema/对象/向量数量与检索对照；批量入库、检索、删除/重建；gRPC/TLS；知识库文档读取真实成功                        |
| O01 V03/V05     | 镜像/Ingress/队列                                        | 所有启用服务digest可拉取；API/Web/worker/plugin真实可用，nginx无新502，队列覆盖所有任务，双自动迁移禁用                |
| O02 V05         | Agent/协同/归档/Human Input                              | 使用环境必测真实运行、文件、工具、暂停续跑/协同/归档读回；关闭环境证明关闭有效及新依赖不破坏启动                       |
| O03 V05         | SSRF                                                     | 通用白名单内可达、外拒绝；Agent专用允许文件/agent-stub，拒绝任意内网；不为Agent扩大通用白名单                          |
| O04 V06         | 整套恢复                                                 | 新版产生写入后恢复旧静止点与旧digest；DB/vector/Redis/files/plugin/Agent一致；无意外任务重放；RTO/RPO实测              |
| O05 D04         | 生产放行                                                 | 受控smoke→真实业务签收→开放→监控窗口；任何账目/鉴权/数据异常零容忍；记录实际部署时间与digest                           |

## 验收分层

1. **规划通过**：OpenSpec严格校验、DAG无环与引用完整、冲突所有权覆盖。本层历史已通过，后续必须按当前候选重新核对受影响项。
2. **源码候选（R01）**：M08 + V01/V02；可以交付含 M00 双父 merge 祖先与逐节点提交的候选源码，不能报告迁移或生产通过。
3. **上线准备（R02）**：V03–V06，实际环境操作包、镜像供货、数据处置和恢复闭环。
4. **生产完成（D04）**：独立授权、生产执行、业务和观察证据。任何无凭据/无环境场景不能用模拟结果代替。
