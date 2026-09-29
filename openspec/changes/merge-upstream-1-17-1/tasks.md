# Tasks: merge-upstream-1-17-1

> 当前现场：M00–M05、M07 已通过；M06 仍在实施。Logstore 完整配置保留修复已通过 46 项测试与独立 Luna 复核；统计恢复由唯一 Astra writer 整合。随后完成个人总览 UI、六端点契约生成及前端宿主验证。M08/V01/V02/R01 等待前置，A03 与真实环境/生产链仍受既有授权边界阻塞。详见 execution-graph.json 与各 evidence。

## 1. A00 实施与分支授权（前置：无）

- [x] 1.1 [A00] 确认用户已授权本地代码实施、校验及创建指定执行分支 `codex/merge-upstream-1.17.1`；授权原文与边界见 `evidence/A00/result.json`。
- [x] 1.2 [A00] 已选择指定执行分支，当前分支实施的替代决定不适用；不以此表示分支已创建，见 `evidence/A00/result.json`。
- [x] 1.3 [A00] 节点验收：授权记录明确涵盖动作、分支名称及环境边界；证据绑定当前源码版本，节点状态已更新。

## 2. A01 冻结基线与保护共享工作区（前置：A00）

- [x] 2.1 [A01] 已检查原始 HEAD、工作区、upstream URL、tag，并全量核对未跟踪路径与目标路径碰撞；统计和命令输出见 `evidence/A01/result.json`、`execution.log`。
- [x] 2.2 [A01] 已按 20 个明确路径独立提交本次规划文件，保留其他未跟踪文件；提交与暂存范围见 `evidence/A01/result.json`。
- [x] 2.3 [A01] 已在确认分支不存在后创建 `codex/merge-upstream-1.17.1`，原始 HEAD 与恢复锚点见 `evidence/A01/result.json`。
- [x] 2.4 [A01] 节点验收：原始完整 SHA 与分析一致，受影响路径无未保存修改或碰撞；证据已绑定原始源码和两个保护提交，状态已更新。

## 3. A02 刷新差异与冲突所有权（前置：A01）

- [x] 3.1 [A02] 通过 upstream 取固定 tag 并核对 full SHA，不自动追 main；核验：在 `evidence/A02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 3.2 [A02] 刷新 merge-base、提交/文件差异和冲突预测；检查四个 fork 后续提交全部入账；核验：在 `evidence/A02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 3.3 [A02] 为新增/移动宿主分配独占 owner，91 路径初表仅是当前快照；核验：在 `evidence/A02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 3.4 [A02] 节点验收：目标确为 8387590ace4a094de812b7847fc6a4c3a27cd52b；所有冲突唯一归属；所有文件改写需求有 owner；保存绑定版本的证据并更新节点状态。

## 4. A03 盘点真实部署与数据（前置：无）

- [ ] 4.1 [A03] 填 runbook 环境表，查询实际服务镜像/版本/两个 DB head/数据卷/备份/队列；核验：在 `evidence/A03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 4.2 [A03] 盘点真实 Agent、SSO、模型和向量库；记录历史 backfill 完成状态；核验：在 `evidence/A03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 4.3 [A03] 确认副本访问与业务验收负责人，准备环境操作草案与备份恢复命令；不得输出凭据或完整含密钥 Compose；核验：在 `evidence/A03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 4.4 [A03] 节点验收：环境清单必填项完成，缺失项标 blocked；具备可恢复副本与隔离环境方案；保存绑定版本的证据并更新节点状态。

## 5. A04 冻结跨层行为与接口契约（前置：A02）

- [x] 5.1 [A04] 按 design D3 逐条写请求/响应/错误/权限/缓存契约；核验：在 `evidence/A04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 5.2 [A04] 确定 public snapshot 与 fork login_config 合成；workspace summary 权限源；核验：在 `evidence/A04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 5.3 [A04] 保留现有 app/dataset key 支持范围；environment 无计费契约则不显示无效输入；核验：在 `evidence/A04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 5.4 [A04] 记录匿名计费及 P4 已知债务，不因未答复改变付款人；核验：在 `evidence/A04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 5.5 [A04] 节点验收：按已授权 design D3 与源码证据冻结身份/前端/额度共用契约，各实施 owner 依此交接；不虚构外部负责人签字，也不以旧文件路径代替后续行为验收；保存绑定版本的证据并更新节点状态。

## 6. M00 形成非部署 merge 基线提交（前置：A04）

- [x] 6.1 [M00] 确认 A01 所有保护措施完成；核验：在 `evidence/M00/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 6.2 [M00] 在授权分支执行正式 merge；冲突退出码不等于失败，核对 MERGE_HEAD 与固定 target；核验：在 `evidence/M00/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 6.3 [M00] 将实际冲突逐项与 A02 的 91 路径及唯一 owner 核对；不一致则阻断并返回 A02 更新台账；核验：在 `evidence/M00/result.json` 附逐路径对照。
- [x] 6.4 [M00] 全部冲突选择 upstream 1.17.1 版本，上游删除则维持删除；逐项记录处置、保留 fork 第一父提交及后续适配 owner；核验：在 `evidence/M00/result.json` 附来源与结果。
- [x] 6.5 [M00] 核验无未解决索引后提交双父 upstream merge 基线；记录父提交和树摘要，标记为不可部署基线；核验：提交后查父提交、索引和工作树。
- [x] 6.6 [M00] 节点验收：第一父为步骤 1 记录的执行前授权分支 HEAD、第二父为固定 target；91 路径逐项核对且 fork 原始内容可追溯；后续适配与验证门槛仍待执行，保存证据并更新节点状态。

M00 结果：merge commit `948fefb69ae87abefa7e91a13968213696b3e310`，tree `56d356a4eb2cacbea577b5fd885bd19637b1203f`；逐路径处置见 `evidence/M00/result.json`。基线不可部署；后续适配与验证状态未提前通过。

## 7. M01 工具链、依赖与生成物（前置：M00）

- [x] 7.1 [M01] 对齐 Node24.20.0/pnpm12.3.4/Python3.12，保留 fork 专属依赖；本节点同步目标版本字段，避免演练后才改变镜像版本标识；核验：在 `evidence/M01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 7.2 [M01] 先处理声明/锁文件冲突；其余 owner 通过需求单申请新增依赖；核验：在 `evidence/M01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 7.3 [M01] fork 自有契约不直接写 generated；最终锁文件在 M08 再统一生成；核验：在 `evidence/M01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 7.4 [M01] 节点验收：依赖变化可解释；无抹除钉钉/pypinyin 等 fork 依赖；生成物来源明确，工具链版本可复现；保存绑定版本的证据并更新节点状态。

M01 结果：**passed（初始锁专项）**。pnpm 12.3.4 官方包及原生二进制 SRI 已核对；初始解析使用获授权的 Node 24.19.0；随后获取 Node 24.20.0 官方归档并通过官方 SHA256 校验，用精确版本重跑仓库锁检查通过。仓库 `pnpm-lock.yaml` 已恢复五个 fork 依赖及必要依赖/peer，已有 package/snapshot 未修改或删除，frozen/offline/lockfile-only 检查通过。Python 3.12.9 使用真实本地缓存的 `--locked --offline --no-sync` 检查通过，`api/uv.lock` SHA 不变。五项 peer 问题与原锁完全相同。此前源码/定向检查证据保留；非代码 ESLint、完整安装/构建仍待 M08/V02，不计为本次通过。M08 保有最终锁/生成物刷新权；M02/M07 ready，M03–M06/M08/V01/V02/R01 pending，A03/环境/生产状态不变。证据见 `evidence/M01/result.json` 的 `retry_2026_09_29` 和 `execution.log`。

## 8. M02 后端基础与共同契约适配（前置：M01）

> M02 原 8.1–8.5 与独立验收已通过。M06 安全交接发现 `/message/context` 缺少 conversation/app/tenant 与 app/Agent 权限校验，现仅重开 8.6–8.7，四个后端源码/测试路径已登记，8.6 独立 Astra 实施、36 项测试、Ruff、Schema 与独立 Luna 复核均通过；8.7 收束完成并恢复 M02 passed。原通过范围及 review 证据保留。详见 `evidence/M02/result.json`、`evidence/M06/result.json#/preflight/contract_handoff`。

- [x] 8.1 [M02] 采用新 application service/admission/session 模型与 fork 导出；核验：在 `evidence/M02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 8.2 [M02] 实现 A04 的 public/login_config/license 与 workspace summary 共同契约；核验：在 `evidence/M02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 8.3 [M02] 逐一审计 extend 对重构 service 的调用和懒加载 ORM 属性，保持 session 生命周期；核验：在 `evidence/M02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 8.4 [M02] 路由注册、安全包装、模型独立文件保留；同步迁移或补充对应定向测试源码，随本节点独立提交冻结，M08 复核；核验：在 `evidence/M02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 8.5 [M02] 原节点验收：public/license 分级无泄露；summary 权限字段真实可读；无旧签名/双重路由；保存绑定版本的证据并更新节点状态。
- [x] 8.6 [M02] 安全修复 message/context：缺参保持 400；服务端解析 Conversation→App 并限制当前 tenant，跨租户/缺失统一 404 且不得读写 context；GET 校验 APP_VIEW_LAYOUT，DELETE 校验 APP_EDIT，Agent 应用补充 AgentBehindApp 权限；更新已登记路由/服务及两项定向测试。实现完成，36 项聚焦测试、四文件 Ruff/格式、Swagger schema 断言均通过，代码已提交 `30410b8fff`，36 项测试与独立 Luna 复核均通过；契约见 `evidence/M06/result.json#/preflight/contract_handoff`，结果见 `evidence/M02/result.json#/followup_context_contract`。
- [x] 8.7 [M02] 冻结 8.6 的 HTTP 权限矩阵、schema 结果及独立 Luna 复核；保留原 8.1–8.5 证据并将 M02 follow-up 状态收束。

## 9. M03 账号、OAuth 与 WebApp 后端（前置：M02）

- [x] 9.1 [M03] 所有实际账号创建入口同事务幂等建额度；不重置已有账号；核验：在 `evidence/M03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 9.2 [M03] 将 OaOAuth/Casdoor 注册到新 gateway，适配 token 类型、state 与同源回跳，保留钉钉；核验：在 `evidence/M03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 9.3 [M03] WebApp site 字段隔离转换，同事务写入扩展和正确缓存失效；核验：在 `evidence/M03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 9.4 [M03] built-in 与 environment passport 分流，保留公开/需登录逻辑；同步迁移或补充对应定向测试源码，随本节点独立提交冻结，M08 复核；核验：在 `evidence/M03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 9.5 [M03] 节点验收：setup/邮箱/OAuth/邀请均有一条正确额度；公开矩阵与新环境认证契约清晰，禁止复活旧 controller；保存绑定版本的证据并更新节点状态。
- [x] 9.6 [M03 follow-up from M06 12.2] 为 Studio 同步状态填充当前 app list page 中存在于 RecommendedApp 的 app IDs；保留现有 optional `recommended_apps: string[]` 契约。仅改已登记 `api/controllers/console/app/app.py` 与 `api/tests/unit_tests/controllers/console/app/test_app_response_models.py`；覆盖页内 synced、unsynced 和页外 synced app。该 follow-up 不改 schema；29 项定向测试、Ruff 与 diff check 通过，代码提交 `11fe1809f30c64d095d5a2a2ac81cc37eeb9e5c6`；证据见 `evidence/M03/result.json#/followups/9.6_recommended_apps_page_status`。

## 10. M04 计费、Service API 与记忆挂点（前置：M03）

- [x] 10.1 [M04] 建立十项挂点完整证据：M04 实施/复核九项计费与记忆挂点；OAuth 引用 M03 9.2 与已通过证据，不重复实现；审计自动合并调用者；核验：在 `evidence/M04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 10.2 [M04] 保留 token kwargs/请求线程 extras/初始执行 join；恢复路径不新增派发；核验：在 `evidence/M04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 10.3 [M04] 保留上游 dataset binding、RBAC、遮罩和 Cloud 限额；fork 配额语义独立；核验：在 `evidence/M04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 10.4 [M04] 保留 NULL retention、匿名 context 修复、三个 beat reset；记录非幂等既有债务；同步迁移或补充对应定向测试源码，随本节点独立提交冻结，M08 复核；核验：在 `evidence/M04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 10.5 [M04] 节点验收：九项由 M04 实施/复核并有新路径与调用证据，OAuth 引用 M03 证据闭合十项总览；所有被装饰入口签名兼容；不新增漏扣/重复派发；保存绑定版本的证据并更新节点状态。

## 11. M05 Console transport 与前端契约（前置：M03）

- [x] 11.1 [M05] 在目标 Console 架构注册 fork segment，迁移双阶段 Header/Cookie 协议；核验：`evidence/M05/result.json` 记录 1.17.1 标准生成、请求局部 token/Cookie/no-store、403 不盲重试、systemManage 唯一 owner 与 201；9 项聚焦测试通过，原 HEAD 同样复现的 6 项完整套件失败如实记录，独立 Luna 无 P1/P2。
- [x] 11.2 [M05] SSR optional/hard 调用守卫形状，public snapshot 与 fork 配置按 A04 合成；核验：`evidence/M05/result.json` 记录缓存/hydration 前的 generated schema 校验、公开 license 裁剪、身份隔离 login_config、SSR 禁止网络调用及 public-first 合成；20 项聚焦测试与 feature scoped check 通过。M06 必须迁移额度徽章以读取配置汇率。
- [x] 11.3 [M05] 生成 schema 校验 workspace summary 并保留 admin_extend/tenant_extend 到权限 atom；清理已移除 Console client 导入；11 项权限链、与 bootstrap 合计 24 项及 9 文件 scoped check 通过。代码执行控制页面 suite 因现有 `cn` 依赖缺失未能收集用例，见 `evidence/M05/result.json`。
- [x] 11.4 [M05] Sol 核验 C01–C04/C08/systemManage 的字段、权限与错误语义；将 workspace summary 生成 schema 校验移入 queryFn，确保畸形 HTTP 200 不进入 TanStack 原始 cache，同时保留 select 校验 hydration/同 key 预填缓存；12 项聚焦测试、25 项受影响测试、两路径 check 通过，独立 Luna 无阻断项。代码提交 `fceb461237f2f175160c8bf56da242f1c4a412c6`；证据见 `evidence/M05/result.json`。
- [x] 11.5 [M05] Sol 对当前源码与 11.1–11.4 证据执行只读节点验收：旧 Console 服务未恢复为双源；SSR 校验真实 public snapshot、匿名不能读详细 license；workspace 两扩展权限位在缓存前生成 schema 校验；`systemManage` 仍由手写契约唯一持有。节点源码验收通过，绑定 `abe3b9ae33b99a373bc3445e0919b6ab21349fe5`；V02 浏览器验收与 M06 汇率消费者仍待下游。
- [x] 11.6 [M05] M02 8.6 已通过后，通过标准 API schema 命令更新已登记的 message 三文件；GET 必填 `conversation_id` → `string[]`，DELETE 必填 `conversation_id/message_id` → 字符串 `ok`。64 个生成任务、191 个格式化文件、schema 断言、仅三条登记路径变化及 Astra task worktree 字节比对均通过。记录见 `evidence/M05/11.6-message-contract-generation.log` 与 `evidence/M05/result.json#/followup_context_contract`。

## 12. M06 前端业务挂载与国际化（前置：M04, M05）

- [x] 12.1 [M06] 迁移 built-in access-point 认证 Switch、environment address/passport、匿名 context guard；M02 8.6 权限与 M05 11.6 类型契约均已通过；12.1a、12.1b 已分别提交，代码审查、聚焦测试和限定检查均通过。详情见 `evidence/M06/result.json#/preflight/contract_handoff`。
  - [x] 12.1a [M06] built-in WebApp 开关独立于站点启用及 environment/global auth；8 个参数化用例由独立 Luna 在隔离 worktree 全部通过（卡片套件 22 项），两文件 scoped `vp check` 通过；独立代码审查无阻断，现有地址/认证套件另有 15 项通过。主工作区缺少已锁定 `cn@0.2.4`，因此未改依赖或 lockfile。证据见 `evidence/M06/result.json#/subtasks/12.1a`。
  - [x] 12.1b [M06] 已由 M02 8.6 和 M05 11.6 解锁；四条登记路径由 Astra 实现并以 `9ee0119ca1ae501008db1913777ac4582af17205` 提交。Node 24.20.0 / pnpm 12.3.4 下两条聚焦测试共 20 项通过，四文件 `vp check` 的格式、lint、限定路径类型检查通过；独立 Luna 未发现阻断。主工作区复跑的组件套件因缺少已声明依赖 `cn@0.2.4` 在收集前失败，隔离 worktree 的相同已提交文件通过；未改依赖或 lockfile。Sol 路径交叉核对仍 provision 中，若发现缺少必需路径再重开。证据见 `evidence/M06/12.1b-path-registration.md` 和 `evidence/M06/result.json#/subtasks/12.1b`。
- [x] 12.2 [M06] 应用中心与 Studio 同步菜单父任务完成。12.2a 应用中心和 12.2c Studio 菜单均已独立实施、测试并提交；M03 9.6 当前页同步状态数据源已通过；12.2c 提交 78c099003f06b06a4820bb5c62999a7c0c65e0e8。证据分别见 evidence/M06/12.2a-verification.md、evidence/M06/12.2c-verification.md 与 evidence/M03/result.json#/followups/9.6_recommended_apps_page_status。
- [x] 12.2a [M06] 修复 fork 应用中心的 installed list hook；在 /installed/apps queryFn 用 Zod 于入缓存前校验；按 tag ID→name 修复分类/标签筛选，多标签按 app_id 去重，并用 buildInstalledAppPath 导航。三份定向测试共 26 项通过，提交 82227748fcbe7b727da1d4aed67306f44ef00a4b；六个登记文件的格式、lint、类型检查通过。主工作区 Base UI 1.6.0 与锁定 1.8.0 不符，已在锁定依赖隔离环境复验；未改依赖、lockfile 或全局 suppression。保留 7155 与重复 f7b7 worktree，不集成未登记的 suppression 变更。证据见 evidence/M06/12.2a-verification.md 与 evidence/M06/result.json#/subtasks/12.2a_app_center_repair。
- [x] 12.2c [M06] Studio app-card 同步/取消同步菜单使用 current-page sync IDs；manager、admin_extend 与 tenant_extend 三重权限门、确认与 pending 防重、成功后失效 apps 列表缓存，失败不改缓存状态。2 个测试文件 137 项通过，提交 78c099003f06b06a4820bb5c62999a7c0c65e0e8；五文件 scoped check 格式正确且无 lint/type 错误。两条既有文本截断 warning 在基线复现。证据见 evidence/M06/12.2c-verification.md 与 evidence/M06/result.json#/subtasks/12.2c_studio_sync_menu。
- [x] 12.3 [M06] API key modal/table 按 scope 显示已支持额度，余额独立显示；必须消费 M05 11.2 login_config 汇率契约。实现提交 `12160c8b2b406dd92d9bb08dc440d6685749ec51`，后续修订 `b49d389cb41783535028647a3acc3505b8d5a54e`、`4526fd02f2039f568ea605012329f4da2a1bc514`。独立 Luna 对最终 12.3+12.4 源码快照验证两条 spec 共 97 项通过；六目标文件与实现提交字节一致，eafe 目标文件哈希恢复且完整状态未变。V02 scope check 仍属后续节点。证据 `evidence/M06/12.3-12.4-final-verification.md`。
- [x] 12.4 [M06] 保活系统管理三类路由及代码执行控制页并保持当前权限，P6 不实施。Sol 分析确认移除 owner-only MainNav 入口为首个断点；Astra 提交 `a7b7ddeeda` 恢复 `SystemManageNavExtend` 并补充 owner/非 owner/路由/角色切换测试。独立 Luna 联合 12.3 验证两条 spec 共 97 项通过；维持权限边界，无 API 权限变更。证据 `evidence/M06/12.4-analysis.md`、`12.3-12.4-final-verification.md`。
- [x] 12.5 [M06] 补齐24个locale的extend namespace并清理零引用旧 secret-key quota modal；category、template-card、app-card-utils 均保留。三组locale代码及完整24语言检查通过；12.5a/b/c 的独立 Luna findings 已修复并 follow-up 通过（id-ID `0b360565b4`，pl-PL `84805a9318`）。修订后官方 `check-i18n.js --file extend` 由直接 Node/tsx 对 24 个 locale 命令级复验通过；12.5d 删除提交 `5bb83572ca`，无活动引用，strict OpenSpec 通过。证据见 `evidence/M06/12.5-final-verification.md`、`12.5-luna-review.md`、`12.5-follow-up-review.md`。
  - [x] 12.5a [M06/Astra] 填充 ar-TN、de-DE、es-ES、fa-IR、fr-FR、hi-IN、id-ID、it-IT 的85个缺失key，并为lo-LA新建完整135-key `extend.json`；仅改对应9个登记locale路径，保留插值token；按语言组运行 `pnpm i18n:check --file extend --lang ...`；已由独立 Astra 子任务实现并提交 `5673d013768afd0daae3cdbc92259c9096da4ff9`；9个 locale 135键顺序、插值及原50值检查通过，限定 i18n check 9种语言均0缺键。独立 Luna 初审无阻断、提出的印尼语 P3 已修复并经 follow-up 通过（`evidence/M06/12.5-follow-up-review.md`）。
  - [x] 12.5b [M06/Astra] ja-JP、ko-KR、nl-NL、pl-PL、pt-BR、ro-RO、ru-RU 七组各补齐85个缺失key；仅改7个登记路径并保留原50值及插值token。源任务提交 `37c4f5c71775880912286347c842106e4a9593ab` 已集成至 `bb83bc818d5e18b45dc0bfdafe40f7f8236fd6d2`；135键顺序、原值与占位符核对通过，Node 24.20.0/pnpm 12.3.4 下语言定向检查七种均0缺键。独立 Luna 发现的波兰语租户语义问题已在 `84805a93186032f84245c01bdbc3ec620c325d63` 修正并由 follow-up 确认通过。证据：`evidence/M06/12.5b-verification.md`。
  - [x] 12.5c [M06/Astra] sl-SI、th-TH、tr-TR、uk-UA、vi-VN、zh-Hant 六组各补齐85个缺失key；仅改6个登记路径并保留原50值及插值token。源任务提交 `9290d1ff47de61b66ce0870241914f42eca183c4` 已集成至 `44395f560311018132d85c4f2bc4d0edadf3789b`；135键顺序、原值与占位符核对通过，Node 24.20.0/pnpm 12.3.4 下语言定向检查六种均0缺键。证据：`evidence/M06/12.5c-verification.md`。 独立 Luna 对组 B/C 的审查 finding 已随 12.5e follow-up 关闭。
  - [x] 12.5d [M06/Astra，前置：12.3 passed] 确认新 API-key quota modal/table 已验收；全仓搜索未发现旧 `web/app/components/develop/secret-key/secret-key-quota-set-modal-extend.tsx` 的静态、动态或测试引用，提交 `5bb83572ca` 删除该组件。有效 category、template-card、app-card-utils 保留；diff check、strict OpenSpec 和 24 locale extend 检查通过。证据见 `evidence/M06/12.5-final-verification.md`。
  - [x] 12.5e [M06/Astra→Luna] 修正并复核 12.5a/b/c 的语言 findings：id-ID 两处 Edit→Ubah，提交 `0b360565b4c733660225faecfd337538a0d635c3`；pl-PL 两处租户术语由 `dzierżawa` 改为 tenant，提交 `84805a93186032f84245c01bdbc3ec620c325d63`。Node24 JSON/key-order/placeholder/值断言与 diff check 通过；pnpm parity 未运行（自动依赖安装启动后停止）。独立 Luna follow-up 无剩余 P1/P2/P3，见 `evidence/M06/12.5-follow-up-review.md`。修订后由独立 Luna 对话 `01a0eb3e-ade0-7380-a27b-b548332ce0c8` 完成24语言静态键集合/顺序/插值校验及 id-ID、pl-PL 原有值对照，均通过；项目 pnpm 检查未运行（本地 pnpm/tsx 环境不满足），见 `evidence/M06/12.5e-post-fix-parity.md`，不可记为命令级通过。
- [ ] 12.6 [M06] 节点验收暂未通过。12.6a 独立应用中心 MainNav 入口已实现并通过聚焦测试；仍待 12.6b、12.6d、12.6i 的依赖恢复后测试验收，12.6e–h 个人统计 workflow/生成/页面链路以及 V02 浏览器验收。`user_overview_extend` 保持 direct-only，不新增未经证实的导航。24 locale 与 WebApp 两开关源码检查通过，但不能代替浏览器验收。详细依据 `evidence/M06/12.6-analysis.md` 与 `evidence/M06/12.6a-verification.md`。
  - [x] 12.6a [M06/Astra→Luna] 将 fork app center 作为独立 MainNav 项接入 `/explore/apps-center-extend`，保留上游 Home `/`；覆盖真实渲染链接、全角色可见性及两路由 active 边界。提交 `6061766c11348e9179cc3eb03b1d747bd3f19726`；MainNav 92/92 通过，三文件 scoped check 零错误、1 条既存 warning，diff check 通过。测试 overlay 文件恢复哈希及 eafe 全工作树状态一致；独立 Luna 无 P1/P2/P3。证据：`evidence/M06/12.6a-verification.md`、`evidence/M06/12.6a-review.md`。
  - [ ] 12.6b [M06/Astra] system-integration 宿主行为测试已实现，覆盖默认 DingTalk 和 OAuth2/Email API/Forward Token 切换及旧面板卸载；focused Vitest 与 scoped check 因 checkout 缺少 Vitest/Vite Plus 依赖而未能启动（均 MODULE_NOT_FOUND，exit 1），diff-check 通过。独立 Luna 无 P1/P2/P3；上游无同路径，另有竞争重复提交 `45180f1b6a` 明确不合并。测试文件 SHA-256 `4088c14548fc66871dba2ba9fa2c63ea52cae3bcc3653c2546290eddb54a1b4d`；待依赖可用环境复验。证据：`evidence/M06/12.6b-validation.md`、`evidence/M06/12.6b-review.md`。
  - [x] 12.6c [M04/M03/Sol] 只读契约分析完成：六个图表需六个端点支持可选 `account=true`，身份只取 authenticated `RequestContext.account_id`；普通统计过滤依赖 `from_account_id`；WebApp workflow 需先贯通可信 account actor 到 SQLAlchemy/Logstore 的持久化和查询，旧数据不得推断归属。分析结论与后端交接见 `evidence/M06/12.6-analysis.md`，具体 owner 路径已登记在执行图 12.6d–h。
  - [x] 12.6d [M04/Astra→Luna] 三项 app 统计图端点现支持可选 `account=true`，身份取 authenticated `RequestContext.account_id`、过滤可信 `from_account_id`，默认仍为全应用。五条登记路径集成提交 `fcc0b0e7a544c804d72b628828ad93776aec12f8`；独立 Luna 无 P1/P2/P3；两份定向单测 22/22 通过，Ruff check 与 format 均通过。首次验证的覆盖文件权限问题已通过临时 Python 环境、临时 uv cache 和关闭默认 coverage 输出解决。证据：`evidence/M06/12.6d-verification.md`、`evidence/M06/12.6d-review.md`。
  - [x] 12.6e [M03/M02/Sol→Astra] 已在提交 `e9359f9bfe688f24212112cdc3e5f2ac6560c910` 落实可信 WebApp actor 的 SQLAlchemy/Logstore/Celery 写入与恢复。使用 fork-only `workflow_run_account_extend` 关联表和扩展迁移 `020_workflow_run_account`（前置 `019_webapp_auth_switch`），未改上游 `workflow_runs`；匿名/旧 run 不推断归属，`created_by` 继续代表 EndUser。首轮七文件回归 **202 项通过**；21 路径 Ruff/格式通过，业务源 Pyrefly 零诊断，测试严格类型检查为 120 条既存基线诊断且无新增。首轮独立 Luna 发现两个 P2，已由 follow-up 提交 `2ca9e0d7cbb3b1cc79148d44659129cf84f98451` 修复。追加七文件回归 **208 项通过**、两文件定向测试 **60 项通过**、四路径 Ruff/格式与 diff-check 通过；独立 Luna 对 follow-up 静态复核无 P1/P2。主迁移 head 保持 `c3f1a9b2e6d4`。未做真实 PostgreSQL/MySQL 升级、Aliyun Logstore 或跨进程可见性验证；同名但列定义错误的索引可能留下性能退化，不影响查询正确性。证据：`evidence/M06/12.6e-analysis.md`、`evidence/M06/12.6e-verification.md`、`evidence/M06/12.6e-review.md`。
  - [x] 12.6e_review_followup [M02/Astra；前置 12.6e Luna review] 修复双写 scope 校验吞异常后仍写 Logstore、以及迁移表存在时未补建 scope index 两个 P2。提交 `2ca9e0d7cbb3b1cc79148d44659129cf84f98451`；两文件定向测试 **60 项通过**，原七文件回归 **208 项通过**，Ruff/format/diff-check 均通过；独立 Luna 静态复核无 P1/P2。已解除 12.6f 的 12.6e 前置。性能边界和未验证的数据库方言见 `evidence/M06/12.6e-review.md`。
  - [x] 12.6f_logstore_actor_index [M04/Astra] 在提交 `17a755d104` 为 workflow_execution 注册 fork-owned `from_account_id` text/doc_value 索引；follow-up `af2dadff5e` 修复既有同名键的 text/doc_value 校验，并保持其他字段原有 JSON/text 兼容和自定义索引。根代理重跑登记测试 **10/10 通过**；Ruff check/format 与 diff-check 通过。未连接 Aliyun，历史记录索引重建/可见性留给部署验收。证据：`evidence/M06/12.6f-logstore-index-verification.md`。
  - [ ] 12.6f_logstore_actor_index_reconciliation [M04/Astra follow-up；重新打开] 首轮修复 `from_account_id` 的 text/doc_value 合并后，新的独立 Luna 复审发现更新索引会丢弃完整 `IndexConfig` 中既有 TTL、全文索引、文本分析和 reduce 设置；仅修正 actor `doc_value` 时也可能覆盖用户 text options。此前状态与复核证据失效，见 config-preservation 子任务。
  - [x] 12.6f_logstore_actor_index_config_preservation [M04/Astra→独立 Luna] 修复 Aliyun `update_index` 覆盖写会丢失现有完整 `IndexConfig` 顶层字段及 actor 自定义 text options 的 P2；精确保留 TTL、all_keys、log_reduce、docvalue_max_text_len、reduce lists、line/scan/custom keys，再只修正必要 actor 设置。路径与验收已登记于 execution-graph.json，证据：`evidence/M06/12.6f-logstore-index-config-preservation.md`。 Astra 对话 `01a0ebc4-6350-7951-a5e5-d508c9ad298e` 正在实现；主代理接手统计前置等待此项及独立复核通过。
  - [ ] 12.6f [M04/Sol→Astra；前置 12.6e review follow-up、两个 Logstore actor 索引子任务] 后端只读分析已完成，五个实现/四个测试路径已登记。SQLAlchemy 通过 `workflow_run_account_extend` 按 workflow/tenant/app/account 联结过滤；Logstore 使用可信 per-run `from_account_id`。两份 Astra 对话并发改写同一共享 checkout 后已停止；统计改动尚未验证/提交，交由单 writer 整合任务收敛。分析证据：`evidence/M06/12.6f-analysis.md`。
  - [ ] 12.6f_statistics_integration_recovery [M04/Astra；单 writer；进行中，对话 `01a0ebc5-0fe7-7861-9560-a10dc5aa82b8`] 整合重复共享 diff，清理 SQL/Logstore 重复参数与谓词，补齐 controller/Logstore 行为测试；仅使用已登记九路径和 `evidence/M06/12.6f-verification.md`。旧对话 `01a0ebbd-d178-7982-9141-834a93649224`、`01a0ebbd-c2bd-7583-9dc6-928554eb7af0` 均停止；统计实现完成后仍需等 `12.6f_logstore_actor_index_config_preservation` 通过才可验收 12.6f。
  - [ ] 12.6g [M06/Astra；前置 12.6d/f] 修复用户总览 Promise params、全时间 account 参数与 `canMonitor` 守卫，增加页面/布局行为测试；保留 direct-only 路由，不新增未经证实的导航入口。
  - [ ] 12.6h [M05/M08/Astra；前置 12.6d/f] 两类后端契约稳定后再生成并审查 Console OpenAPI 与 apps TS/Zod/oRPC 类型，生成物所有权仍归 M05/M08。
  - [ ] 12.6i [M06/Astra；前置 12.3] API-key 测试已在 `a602bb8a58` 提交：新建后点击复制并读回完整 secret；三个 scope 在 `canManage=false` 时不显示编辑/删除且 mutation 未调用。focused test 与 scoped check 因 Vitest/Vite Plus 依赖不完整未能启动（exit 1），diff-check 通过；独立 Luna 无 P1/P2/P3。Luna 独立重试复用了指向已删除 worktree 的 Vitest 悬空 symlink（0 tests），Vite Plus 仍缺失且依赖副本哈希未变；另一份重叠测试提交 `777238cc` 不合并。待依赖可用环境运行 focused spec 与 scoped check。证据：`evidence/M06/12.6i-validation.md`、`evidence/M06/12.6i-review.md`。

  - [ ] 12.6j [M06/Astra] 使用精确 Node/pnpm 和 frozen lock 恢复当前 checkout 的前端依赖；仅写 ignored 依赖与专属证据，不改 manifest/lock，不创建 worktree。依赖缺失与源码验证分开记账。证据 `evidence/M06/12.6j-dependency-recovery.md`。

## 13. M07 综合部署和 CI 对齐（前置：M01）

- [x] 13.1 [M07] 人工移植上游 Compose 契约到 fork 综合 Compose，保留独立 worker/sandbox-full/私有镜像；核验：`evidence/M07/result.json` 记录 1.17.1 源文件、39 项环境键/默认值对照、私有 tag 与 worker 结构断言，以及默认/全 profile Compose 检查。
- [x] 13.2 [M07] 单执行者双迁移；发布业务容器关闭自动迁移；核验：`evidence/M07/result.json` 记录自动迁移固定关闭、单次私有镜像迁移服务的主链/扩展链顺序、成功和失败位置断言；默认/全 profile Compose 解析通过。单执行者的跨 project 并发防护仍是操作约束。
- [x] 13.3 [M07] 更新 Agent token/网络/SSRF/卷、plugin版本/队列、Web Next/Vinext 与 ingress；核验：`evidence/M07/result.json` 记录 token 双端、专用 SSRF/隔离网/持久卷、plugin 0.6.10-local、Vinext 和未改动的上游同版 ingress/entrypoint；83 项静态断言、默认/全 profile Compose 解析通过。
- [x] 13.4 [M07] 已更新 906 行 env 逐变量审计，根样例只保留 `COMPOSE_PROFILES`、`DIFY_AGENT_SERVER_SECRET_KEY`；五项 Compose fallback、嵌套 PostgreSQL 默认值及 SECRET_KEY 持久化策略通过前序复验。提交 `1b1ddf4eb3` 新增 `init_secret_key` 串行 seed 七个消费者、`init_permissions` 失败闭合及 `--no-deps` migration 说明；358 项专项断言、52 项旧 env 检查、83 项 seed-aware Compose 检查及 SSRF 单测通过。独立 Luna 复核无 P1/P2；记录了 `--no-deps`、直接启动及跨 Compose project 并发的 P3 操作边界。证据见 `evidence/M07/result.json#/subtasks/13.4/startup_gate_implementation` 和 `evidence/M07/13.4-independent-review.md`。
- [x] 13.5 [M07] GitHub build validate 与 GitLab 私有镜像供货分开记账；两份 GitHub workflow 与上游相同且不为 fork 推送；GitLab manifest/artifact 静态风险见 `evidence/M07/ci-analysis.md`。
- [x] 13.6 [M07] 节点验收通过（源码静态边界）：fork Compose、环境样例、私有 API/Web 镜像矩阵及无文本冲突路径均经独立 Sol 审查；GitLab 多架构 manifest P2 由 Astra 对话 `01a0eb4f-1818-7e70-8047-fa503161a19a` 在 `.gitlab-ci.yml` 提交 `c707b8fcdc58b30bb45785426cc586ec848f05af` 修复，移除跨 job 同名 txt 文件，改用 build job 已推送的 API/Web amd64/arm64 tags。Ruby Psych YAML 解析、6个 job `sh -n`、静态 tag/config 断言、diff check 与独立 Luna 对话 `01a0eb51-38be-7641-ad06-5c98228b034b` 复核均通过（无 P1/P2/P3）。证据：`evidence/M07/13.6-independent-review.md`、`evidence/M07/13.6-manifest-fix-followup-review.md`。未运行 GitLab pipeline 或私有镜像发布；镜像 digest/推送、容器启动、真实密钥及迁移运行仍不属于本节点验收。

## 14. M08 集成审查与候选源码提交（前置：M02, M03, M04, M05, M06, M07）

- [ ] 14.1 [M08] 核对 M01–M07 各自已提交代码、配套测试、证据与状态；由各 owner 交付冲突处理与自动合并语义清单；核验：在 `evidence/M08/result.json` 附各节点提交与清单，不以规划代替完成。
- [ ] 14.2 [M08] M01 将锁文件写入权显式移交集成负责人，由 M08 统一刷新最终锁文件/契约生成物；扫描 orphan、旧 import、安全包装、四个后续提交；核验：在 `evidence/M08/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 14.3 [M08] 只暂存逐项列出的本轮集成修复、证据和状态文件，不 stage 未跟踪用户目录；创建候选源码提交，不再创建 upstream merge commit；核验：在 `evidence/M08/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 14.4 [M08] 更新冲突表每项处置/新宿主/验收映射；核验：在 `evidence/M08/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 14.5 [M08] 节点验收：未解决索引为零、冲突标记和孤儿宿主为零；M00 双父 merge 祖先仍可追溯，M01–M07 各有独立提交，十挂点和全部91路径有审查记录；保存绑定版本的证据并更新节点状态。

## 15. V01 后端静态与定向回归（前置：M08）

- [ ] 15.1 [V01] 读取 api/AGENTS.md；按合并后工具配置运行静态/导入和定向测试；核验：在 `evidence/V01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 15.2 [V01] 执行已随候选提交冻结的新注册/gateway/site/service_api/key/message/workflow/session/beat/retention 用例；需要修改源码或用例则退回对应 M 节点，更新候选后重验；核验：在 `evidence/V01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 15.3 [V01] 新增问题交还对应 M 节点修复，固定新 commit 后重验；核验：在 `evidence/V01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 15.4 [V01] 节点验收：单测报告明确用例、commit、结果；已有债务与新增回归分开；无运行权限或依赖不可用记 blocked；保存绑定版本的证据并更新节点状态。

## 16. V02 前端检查、定向测试与双构建（前置：M08）

- [ ] 16.1 [V02] 在固定候选提交执行认证/新 access-point/api-key/应用中心用例；先核对实际收集到 browser 场景，零用例或路径失效不得记通过；若需改测试源码，退回 M05/M06 并经 M08 冻结新候选；核验：在 `evidence/V02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 16.2 [V02] 执行 frozen 安装、check/tss/i18n、unit/browser 和双构建；核验：在 `evidence/V02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 16.3 [V02] 新增场景必须实际进入新宿主，不能仅保留无人调用旧组件用例；核验：在 `evidence/V02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 16.4 [V02] 节点验收：Node/pnpm 与锁文件一致，检查与双构建成功；定向认证与角色矩阵通过；24份extend可加载；保存绑定版本的证据并更新节点状态。

## 17. R01 源码合并候选验收（前置：V01, V02）

- [ ] 17.1 [R01] 汇总全部冲突/语义/静态测试记录；核验：在 `evidence/R01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 17.2 [R01] 记录候选 commit 与待环境验证项；不得此时声称已上线；核验：在 `evidence/R01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 17.3 [R01] 节点验收：源码候选可构建，阻断级新增回归清零；状态明确为 code-ready，仍须环境链；保存绑定版本的证据并更新节点状态。

## 18. V03 目标镜像、空库与存量双链演练（前置：R01, A03）

- [ ] 18.1 [V03] 先准备环境专属 override/备份恢复命令草案；构建固定候选 fork 镜像并核对 digest、平台与供货，把实际 digest 固定到隔离 override，复核无生产存储/队列连接后才开始演练；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.2 [V03] 空库从零两链；实际旧库副本主链再extend，记录17迁移与两head；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.3 [V03] 审计Agent删表/JSON、normalized email、模型去重/凭据引用与可解密；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.4 [V03] 实际生产DB引擎必测；声称同时支持PG/MySQL则两者均测；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.5 [V03] 节点验收：双head为 c3f1a9b2e6d4 / 019_webapp_auth_switch；数据差异符合预审、破坏性数据有处置决定、耗时记录；保存绑定版本的证据并更新节点状态。

## 19. V04 向量库副本演练（前置：V03）

- [ ] 19.1 [V04] Weaviate 按真实版本逐minor演练至目标；低于1.27先专项方案；核验：在 `evidence/V04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 19.2 [V04] 每站验证schema/对象/向量/检索/备份恢复；必要时逻辑导出重建；核验：在 `evidence/V04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 19.3 [V04] 非Weaviate不升级无关引擎，记录驱动读写检索验证；核验：在 `evidence/V04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 19.4 [V04] 禁止修改生产数据卷，记录gRPC/TLS及与Dify客户端适配；核验：在 `evidence/V04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 19.5 [V04] 节点验收：实际数据可写可检索，和基线查询集可比；恢复路径与耗时有证据；其他向量库分支也必须通过；保存绑定版本的证据并更新节点状态。

## 20. V05 真实业务与权限验收（前置：V03, V04）

- [ ] 20.1 [V05] 使用目标 fork 镜像，按 verification-matrix 全部必测场景验收；核验：在 `evidence/V05/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 20.2 [V05] 验证真实模型返回/计费、SSO、WebApp、知识库、系统管理、worker/beat/插件；核验：在 `evidence/V05/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 20.3 [V05] Agent/协同/归档启用态真实验证；关闭态记录未启用和关闭有效；核验：在 `evidence/V05/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 20.4 [V05] 禁止用HTTP200、fixture或build成功替代业务通过；核验：在 `evidence/V05/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 20.5 [V05] 节点验收：矩阵每项有请求/结果/数据库对账证据；缺真实账号或凭据记blocked，不宣称生产就绪；保存绑定版本的证据并更新节点状态。

## 21. V06 整套恢复演练（前置：V05）

- [ ] 21.1 [V06] 新版本先产生样本写入；停止所有写入者再恢复同一旧静止点数据与旧镜像；核验：在 `evidence/V06/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 21.2 [V06] 验证旧双head、解密、文件/向量、登录/工作流/计费；核验：在 `evidence/V06/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 21.3 [V06] 避免Redis恢复导致旧任务重放；记录清算/暂停/去重策略与RPO损失；核验：在 `evidence/V06/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 21.4 [V06] 记录RTO与操作负责人；禁止image-only downgrade；核验：在 `evidence/V06/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 21.5 [V06] 节点验收：旧版可真实运行，数据来自同一恢复点；RTO/RPO可接受且无不受控任务重放；保存绑定版本的证据并更新节点状态。

## 22. R02 版本文档与环境上线操作包（前置：V06）

- [ ] 22.1 [R02] 核验 M01 已对齐的版本字段，更新十挂点新坐标、运行手册、AGENTS baseline 与所有验证证据；核验：在 `evidence/R02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 22.2 [R02] 修订旧强制WebApp登录/旧migration018/旧前端路径；P4/P6仅标后续重定基线；核验：在 `evidence/R02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 22.3 [R02] 根据 V03–V06 实际结果定稿此前演练使用的环境操作草案，填 runbook 环境表、备份恢复命令、镜像 digest、维护窗口与阈值；核验：在 `evidence/R02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 22.4 [R02] 提交本轮明确路径；code候选通过后生成 fork-merged-1.17.1 留档tag，不覆盖已有tag；核验：在 `evidence/R02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 22.5 [R02] 源码或依赖若在文档收尾变化，返回对应验证节点；核验：在 `evidence/R02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 22.6 [R02] 节点验收：可执行环境包无占位符，演练证据完整；tag对应已验证代码，明确不代表生产已上线；保存绑定版本的证据并更新节点状态。

## 23. D00 生产变更授权与放行（前置：R02）

- [ ] 23.1 [D00] 提交精确版本、停机窗口、数据删除影响、备份恢复点、RPO/RTO给用户确认；核验：在 `evidence/D00/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 23.2 [D00] 确认所需镜像已可拉取、备份容量与责任人在线；超阈值则延期；核验：在 `evidence/D00/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 23.3 [D00] 节点验收：取得明确生产执行授权且全部前置通过；保存绑定版本的证据并更新节点状态。

## 24. D01 封入口、停写与一致快照（前置：D00）

- [ ] 24.1 [D01] 按runbook停新请求、触发器/beat，按截止时间排空或冻结在途任务，再停worker/API/Agent/plugin等写入者；核验：在 `evidence/D01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 24.2 [D01] 核实无外部写入者，记录DB/向量/文件/Redis同一静止点；核验：在 `evidence/D01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 24.3 [D01] 备份并验证可读取恢复，记录所有版本与密钥受控引用；核验：在 `evidence/D01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 24.4 [D01] 节点验收：写入静止，备份完整可恢复；在途任务和恢复后重投策略已记录；保存绑定版本的证据并更新节点状态。

## 25. D02 执行环境向量路径（前置：D01）

- [ ] 25.1 [D02] 仅执行V04已演练同版本同拓扑路径，逐站验证后推进；核验：在 `evidence/D02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 25.2 [D02] 其他向量库分支执行健康/兼容确认，不更换无关镜像；核验：在 `evidence/D02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 25.3 [D02] 任一步异常停止；恢复当前站配对快照，后续不得启动；核验：在 `evidence/D02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 25.4 [D02] 节点验收：实际版本/schema/检索与演练一致；保存绑定版本的证据并更新节点状态。

## 26. D03 执行单一双链迁移（前置：D02）

- [ ] 26.1 [D03] 仅启动必要中间件，用目标 fork API 单job先db upgrade再extend_db upgrade；核验：在 `evidence/D03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 26.2 [D03] 核对两head与破坏性迁移审计，不盲目stamp或重试；核验：在 `evidence/D03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 26.3 [D03] 失败保留现场，查已提交revision/残留索引再执行已批准恢复路线；核验：在 `evidence/D03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 26.4 [D03] 节点验收：双head、数据对账、模型凭据读回满足目标；所有业务容器自动迁移关闭后才允许启动；保存绑定版本的证据并更新节点状态。

## 27. D04 启动、业务放行与观察（前置：D03）

- [ ] 27.1 [D04] 用配对 digest 启动 API/Web/必要中间件及受控 worker/必要队列，beat/trigger 继续暂停；在封闭入口完成登录/模型/异步工作流/真实扣费/知识库 smoke；核验：在 `evidence/D04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 27.2 [D04] 必测通过后逐步开放入口，按既定策略开放其余任务消费并恢复 beat/trigger 调度；核验：在 `evidence/D04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 27.3 [D04] 按窗口监控5xx/登录/扣费偏差/任务堆积/检索；超阈值封入口并整套恢复；核验：在 `evidence/D04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 27.4 [D04] 保存最终时间线、版本和结果；旧快照保留期结束另行处理；核验：在 `evidence/D04/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 27.5 [D04] 节点验收：真实业务负责人签收与观察通过，才标 deployed；任何恢复后的新写入损失按RPO记录，绝不自动删除旧快照；保存绑定版本的证据并更新节点状态。
