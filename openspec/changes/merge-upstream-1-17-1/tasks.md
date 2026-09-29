# Tasks: merge-upstream-1-17-1

> 当前现场：M00–M07 已通过。候选 `783859de3ebc098f2c9916516c831d92e547a913` / tree `badf6fb2196fe201185a1e81883504eb35cbffc0` 通过 M08/14.16 结构验收，但 V02/16.10 在 i18n 阶段被 `uk-UA` layout/oauth 缺 19 个 key 阻断；全仓 check、TypeScript lint 通过，单测与双构建按首失败规则停止。M08/14.17 已补齐 19 个乌克兰语 key，JSON/key parity 与定向 i18n 检查通过。当前 M08/14.18 按精确白名单冻结新候选并复核结构，之后运行 V02/16.11。个人 `total_quota/used_quota` 仍独立于 API Key 日/月限额，`accumulated_quota` 是累计使用量；生产与真实环境验收仍在后续节点。详见 execution-graph.json 与证据。

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

- [x] 11.7 [M05] 修复 V02 首轮 `pnpm check` 暴露的 M05 所有权文件 `packages/contracts/openapi-ts.api.config.ts` 格式债务；仅允许格式器规范化；需 exact-path formatter 通过、逐行确认无语义变化并记录原始 diff，写报告并形成独立 M05 follow-up 提交 `c304fe188e949733fa4ac1697004e3b99d9db95d`。

- [x] 11.8 [M05/Astra] 修复 V02/16.8 暴露的 API 输出类型与 UI cache updater/测试夹具不一致：保留 WebApp auth 默认 true 与显式 false 的语义；对 workspace summary fixture 补齐后端当前必填的 `admin_extend` / `tenant_extend` 默认值。四路径 scoped check 零错误（1 条既有 warning）、两套定向测试 53/53、false/default probe 和 diff-check 通过；证据 `evidence/M05/11.8-v02-contract-reconciliation.md` SHA-256 `70755ff3753dfeebcdbe62bc8e0955ccfc560454604d6eb5e79f81c459e691fa`。

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
- [x] 12.6 [M06] 全部子项已通过；集成快照 c474b9c42b 的 16 个测试文件共 399 项通过，24 语言 extend、六端点契约、OpenSpec strict 与 diff-check 通过。源码节点完成，真实浏览器/角色、全仓检查、双构建与部署仍由 V02/环境节点验收。证据 `evidence/M06/12.6-final-aggregate.md`。
  - [x] 12.6a [M06/Astra→Luna] 将 fork app center 作为独立 MainNav 项接入 `/explore/apps-center-extend`，保留上游 Home `/`；覆盖真实渲染链接、全角色可见性及两路由 active 边界。提交 `6061766c11348e9179cc3eb03b1d747bd3f19726`；MainNav 92/92 通过，三文件 scoped check 零错误、1 条既存 warning，diff check 通过。测试 overlay 文件恢复哈希及 eafe 全工作树状态一致；独立 Luna 无 P1/P2/P3。证据：`evidence/M06/12.6a-verification.md`、`evidence/M06/12.6a-review.md`。
  - [x] 12.6b [M06] 系统集成页宿主测试已提交；依赖恢复后独立 Luna 实测 2 项通过，限定格式/lint/type 检查通过，文件哈希与原提交一致。证据 `evidence/M06/12.6b-validation.md`。
  - [x] 12.6c [M04/M03/Sol] 只读契约分析完成：六个图表需六个端点支持可选 `account=true`，身份只取 authenticated `RequestContext.account_id`；普通统计过滤依赖 `from_account_id`；WebApp workflow 需先贯通可信 account actor 到 SQLAlchemy/Logstore 的持久化和查询，旧数据不得推断归属。分析结论与后端交接见 `evidence/M06/12.6-analysis.md`，具体 owner 路径已登记在执行图 12.6d–h。
  - [x] 12.6d [M04/Astra→Luna] 三项 app 统计图端点现支持可选 `account=true`，身份取 authenticated `RequestContext.account_id`、过滤可信 `from_account_id`，默认仍为全应用。五条登记路径集成提交 `fcc0b0e7a544c804d72b628828ad93776aec12f8`；独立 Luna 无 P1/P2/P3；两份定向单测 22/22 通过，Ruff check 与 format 均通过。首次验证的覆盖文件权限问题已通过临时 Python 环境、临时 uv cache 和关闭默认 coverage 输出解决。证据：`evidence/M06/12.6d-verification.md`、`evidence/M06/12.6d-review.md`。
  - [x] 12.6e [M03/M02/Sol→Astra] 已在提交 `e9359f9bfe688f24212112cdc3e5f2ac6560c910` 落实可信 WebApp actor 的 SQLAlchemy/Logstore/Celery 写入与恢复。使用 fork-only `workflow_run_account_extend` 关联表和扩展迁移 `020_workflow_run_account`（前置 `019_webapp_auth_switch`），未改上游 `workflow_runs`；匿名/旧 run 不推断归属，`created_by` 继续代表 EndUser。首轮七文件回归 **202 项通过**；21 路径 Ruff/格式通过，业务源 Pyrefly 零诊断，测试严格类型检查为 120 条既存基线诊断且无新增。首轮独立 Luna 发现两个 P2，已由 follow-up 提交 `2ca9e0d7cbb3b1cc79148d44659129cf84f98451` 修复。追加七文件回归 **208 项通过**、两文件定向测试 **60 项通过**、四路径 Ruff/格式与 diff-check 通过；独立 Luna 对 follow-up 静态复核无 P1/P2。主迁移 head 保持 `c3f1a9b2e6d4`。未做真实 PostgreSQL/MySQL 升级、Aliyun Logstore 或跨进程可见性验证；同名但列定义错误的索引可能留下性能退化，不影响查询正确性。证据：`evidence/M06/12.6e-analysis.md`、`evidence/M06/12.6e-verification.md`、`evidence/M06/12.6e-review.md`。
  - [x] 12.6e_review_followup [M02/Astra；前置 12.6e Luna review] 修复双写 scope 校验吞异常后仍写 Logstore、以及迁移表存在时未补建 scope index 两个 P2。提交 `2ca9e0d7cbb3b1cc79148d44659129cf84f98451`；两文件定向测试 **60 项通过**，原七文件回归 **208 项通过**，Ruff/format/diff-check 均通过；独立 Luna 静态复核无 P1/P2。已解除 12.6f 的 12.6e 前置。性能边界和未验证的数据库方言见 `evidence/M06/12.6e-review.md`。
  - [x] 12.6f_logstore_actor_index [M04/Astra] 在提交 `17a755d104` 为 workflow_execution 注册 fork-owned `from_account_id` text/doc_value 索引；follow-up `af2dadff5e` 修复既有同名键的 text/doc_value 校验，并保持其他字段原有 JSON/text 兼容和自定义索引。根代理重跑登记测试 **10/10 通过**；Ruff check/format 与 diff-check 通过。未连接 Aliyun，历史记录索引重建/可见性留给部署验收。证据：`evidence/M06/12.6f-logstore-index-verification.md`。
  - [x] 12.6f_logstore_actor_index_reconciliation [M04] 原验证因完整配置丢失 P2 失效；后续 preservation 修复 8466ad5ff7 经 46 项测试及独立 Luna 复核恢复通过。证据 `evidence/M06/12.6f-logstore-index-config-preservation.md`。
  - [x] 12.6f_logstore_actor_index_config_preservation [M04/Astra→独立 Luna] 修复 Aliyun `update_index` 覆盖写会丢失现有完整 `IndexConfig` 顶层字段及 actor 自定义 text options 的 P2；精确保留 TTL、all_keys、log_reduce、docvalue_max_text_len、reduce lists、line/scan/custom keys，再只修正必要 actor 设置。路径与验收已登记于 execution-graph.json，证据：`evidence/M06/12.6f-logstore-index-config-preservation.md`。 提交 `8466ad5ff7`；46 项测试、Ruff/格式检查及独立 Luna 复核通过，无 P1/P2/P3；未连接 Aliyun。
  - [x] 12.6f [M04] SQLAlchemy/Logstore 三个统计端点支持可信 account=true，保留默认全应用与 daily-terminals。40 项聚焦测试、63 项 Schema 测试、九文件 Ruff/格式、源码/测试 Pyrefly 通过；独立 Luna 无 P1/P2/P3。证据 `evidence/M06/12.6f-verification.md`。
  - [x] 12.6f_statistics_integration_recovery [M04/Astra] 已将共享 diff 收敛为九路径实现和测试；原 writer 均停止，最终文件哈希与独立审阅相符，索引前置 8466ad5ff7 已通过。
  - [x] 12.6g [M06/Astra→Luna] Promise params、全周期 account=true、canMonitor/加载/app-ID 守卫与空图日期回退完成；保持 direct-only。独立 Luna 使用 Node 24.20.0 实测 62 项通过，六路径格式/lint/type 与 diff-check 通过，最终哈希相符。证据 `evidence/M06/12.6g-verification.md`。
  - [x] 12.6h [M05/M08/Astra→Luna] 标准 Console OpenAPI/Markdown 与 apps TS/Zod/oRPC 生成完成；六端点 account 布尔参数经 AST/实际 schema 解析及独立 Luna 复核通过。其余 196 个生成输出哈希未变；忽略的 JSON 不强制提交。证据 `evidence/M06/12.6h-verification.md`。
  - [x] 12.6i [M06/Astra→Luna] API-key 复制完整 secret、三 scope 只读权限行为测试已提交；恢复依赖并修复夹具类型后，最终独立 Luna 在 Node 24.20.0 验证 23 项、scoped format/lint/type 与 diff-check 通过，无 P1/P2/P3。证据 `evidence/M06/12.6i-validation.md`。

  - [x] 12.6i_fixture_typing [M06] queryKey 使用 { input }，以显式类型的现有 key 构建更新夹具；未削弱断言或引入抑制/类型强转。最终独立复核与 23 项测试及限定检查通过。

  - [x] 12.6j [M06/Astra] 使用精确 Node/pnpm 和 frozen lock 恢复当前 checkout 的前端依赖；仅写 ignored 依赖与专属证据，不改 manifest/lock，不创建 worktree。根安装和 contracts/web workspace 入口均通过；21 失效链接、28 旧包装器已备份后恢复。依赖就绪与源码验证分开记账。证据 `evidence/M06/12.6j-dependency-recovery.md`。

- [x] 12.7 [M06/Astra] 关闭 M08/14.2 登记的 `parameter-item-extend.tsx` 未决 dormant-file 处置：保留 fork 组件，将失效的 `@langgenius/dify-ui/radio` 导入改为包实际导出的 radio-group 入口；确认本地无调用方。单文件 `vp check` 与 diff-check 通过，无 warning/lint/type error；证据 `evidence/M06/12.7-parameter-item-disposition.md`，源码 SHA-256 `acd496c6a1c5b865288caca9ec5d23c2f813251f210c41e9464c8cc19bed4ef4`，证据 SHA-256 `a75d9e237cd0f72a0ff10d38c9d1ab6a15e72348a24f17f7114d32b3408651b7`。未提交。

## 13. M07 综合部署和 CI 对齐（前置：M01）

- [x] 13.1 [M07] 人工移植上游 Compose 契约到 fork 综合 Compose，保留独立 worker/sandbox-full/私有镜像；核验：`evidence/M07/result.json` 记录 1.17.1 源文件、39 项环境键/默认值对照、私有 tag 与 worker 结构断言，以及默认/全 profile Compose 检查。
- [x] 13.2 [M07] 单执行者双迁移；发布业务容器关闭自动迁移；核验：`evidence/M07/result.json` 记录自动迁移固定关闭、单次私有镜像迁移服务的主链/扩展链顺序、成功和失败位置断言；默认/全 profile Compose 解析通过。单执行者的跨 project 并发防护仍是操作约束。
- [x] 13.3 [M07] 更新 Agent token/网络/SSRF/卷、plugin版本/队列、Web Next/Vinext 与 ingress；核验：`evidence/M07/result.json` 记录 token 双端、专用 SSRF/隔离网/持久卷、plugin 0.6.10-local、Vinext 和未改动的上游同版 ingress/entrypoint；83 项静态断言、默认/全 profile Compose 解析通过。
- [x] 13.4 [M07] 已更新 906 行 env 逐变量审计，根样例只保留 `COMPOSE_PROFILES`、`DIFY_AGENT_SERVER_SECRET_KEY`；五项 Compose fallback、嵌套 PostgreSQL 默认值及 SECRET_KEY 持久化策略通过前序复验。提交 `1b1ddf4eb3` 新增 `init_secret_key` 串行 seed 七个消费者、`init_permissions` 失败闭合及 `--no-deps` migration 说明；358 项专项断言、52 项旧 env 检查、83 项 seed-aware Compose 检查及 SSRF 单测通过。独立 Luna 复核无 P1/P2；记录了 `--no-deps`、直接启动及跨 Compose project 并发的 P3 操作边界。证据见 `evidence/M07/result.json#/subtasks/13.4/startup_gate_implementation` 和 `evidence/M07/13.4-independent-review.md`。
- [x] 13.5 [M07] GitHub build validate 与 GitLab 私有镜像供货分开记账；两份 GitHub workflow 与上游相同且不为 fork 推送；GitLab manifest/artifact 静态风险见 `evidence/M07/ci-analysis.md`。
- [x] 13.6 [M07] 节点验收通过（源码静态边界）：fork Compose、环境样例、私有 API/Web 镜像矩阵及无文本冲突路径均经独立 Sol 审查；GitLab 多架构 manifest P2 由 Astra 对话 `01a0eb4f-1818-7e70-8047-fa503161a19a` 在 `.gitlab-ci.yml` 提交 `c707b8fcdc58b30bb45785426cc586ec848f05af` 修复，移除跨 job 同名 txt 文件，改用 build job 已推送的 API/Web amd64/arm64 tags。Ruby Psych YAML 解析、6个 job `sh -n`、静态 tag/config 断言、diff check 与独立 Luna 对话 `01a0eb51-38be-7641-ad06-5c98228b034b` 复核均通过（无 P1/P2/P3）。证据：`evidence/M07/13.6-independent-review.md`、`evidence/M07/13.6-manifest-fix-followup-review.md`。未运行 GitLab pipeline 或私有镜像发布；镜像 digest/推送、容器启动、真实密钥及迁移运行仍不属于本节点验收。

## 14. M08 集成审查与候选源码提交（前置：M02, M03, M04, M05, M06, M07）

- [x] 14.1 [M08] 核对 M01–M07 已提交代码、测试、证据与状态，并交付 91 个原始冲突路径清单；Sol 只读分析与独立 Luna 复核均确认 91/91 唯一覆盖，记录见 `evidence/M08/14.1-analysis.md` 与 `evidence/M08/result.json`。17 个未决项已明确留给后续 owner 处置；这只完成分析子项，不代表 M08 通过。
- [x] 14.2 [M08] M01 锁/契约最终写入权已交接；Astra 对照锁、生成产物哈希证明无需刷新，并完成静态 import、安全包装、额度边界和四个 fork 后续提交扫描。发现的旧余额 wrapper 与 14 个删除路径已注册后续处置；证据 `evidence/M08/14.2-integration-scan.md`。
  - [x] 14.2a [M08/Astra→Luna] 删除迁移后零引用的 account/money wrapper、DTO 与唯一 suppression；Node 24.20 / Vitest 4.1.11 下主导航余额/API-key 两组测试 115/115 通过。个人额度、Console 余额链路及 Key 日/月限制保留。证据 `evidence/M08/14.2a-orphan-cleanup.md`、`evidence/M08/14.2a-luna-verification.md`。
  - [x] 14.2b [M08/Sol] 对 14 个删除冲突路径逐项完成只读 owner disposition，并复核 active template-card 宿主映射；三个待闭合项转后续 owner/验证任务。证据 `evidence/M08/14.2b-deleted-path-disposition.md`。
  - [x] 14.2c [M08/Sol] 完成根目录项目说明和 Dify-Plus 文档处置只读核查；保留账户独立额度与 API Key 日/月限额语义，文档整合由协调者按登记路径继续。证据 `evidence/M08/14.2c-doc-disposition.md`。
  - [x] 14.2d [M08/Sol] 按 14.2c 核查整合根目录与 Dify-Plus 文档；明确 quota 语义、历史文档边界和本次 1.17.1 执行入口。15 个相对链接与 diff whitespace 检查通过，证据 `evidence/M08/14.2d-documentation-integration.md`。
  - [x] 14.2e [M08/Astra→Luna] 在新 AccessPoint 宿主保留原 workspace-manager-only App API Key 管理门槛；无 manager 权限时不读取或打开密钥管理，保留 AccessPoint ACL 与日/月额度流程。修正 workspace fixture schema 后单文件复验 9/9 通过。证据 `evidence/M08/14.2e-manager-only-apikey-boundary.md`、`evidence/M08/14.2h-luna-verification.md`。
    - [x] 14.2e-Luna 首轮 [M08/Luna] 首次运行 2/9 的发现是测试夹具缺少 schema 必需字段，修复见 14.2g；不作为源码行为失败。证据 `evidence/M08/14.2e-luna-verification.md`。
  - [x] 14.2g [M08/Sol] 修复权限 spec 的 workspace fixture：补齐 `admin_extend` / `tenant_extend`，确保 manager 正向场景和撤权场景走有效角色快照；只改测试夹具。未运行测试。证据 `evidence/M08/14.2g-test-fixture-correction.md`。
  - [x] 14.2h [M08/Luna] 修正夹具后只复跑 `api-secret-key-button.spec.tsx`，验证 manager/ACL 双门槛、查询阻断与权限撤销；9/9 通过。证据 `evidence/M08/14.2h-luna-verification.md`。
  - [x] 14.2i [M08/Astra→Luna] 按 fork 原有 90vw/1200px 上限恢复统一 API Key modal 的响应式宽度；Astra Chromium 2/2，Luna 独立复核 2/2，输入哈希匹配。证据 `evidence/M08/14.2i-quota-modal-layout.md`、`evidence/M08/14.2j-luna-verification.md`。
- [x] 14.2j [M08/Luna] 独立核验 14.2i 源码、测试与哈希，并只复跑登记的 Chromium browser spec；2/2 通过，无额外依赖或测试范围扩展。证据 `evidence/M08/14.2j-luna-verification.md`。
  - [x] 14.2f [M08/Sol] 类型与调用链核实旧 Home TagFilter 将 Tag UUID 与 catalog category 字符串直接比较，没有可用映射；保留有效的 Home 分类筛选和已安装应用 TagFilter，不恢复错误比较。证据 `evidence/M08/14.2f-home-template-tagfilter-disposition.md`。
- [x] 14.3 [M08] 仅将登记的 34 个源码、文档、图表和证据路径提交为候选 `ba33fe26dc21ae3a308f14a61999009fc2849779`（父 `ac5fca9ace9401734bca31d87b03548b9ff83b18`）；未纳入用户未跟踪目录，M00 merge 祖先不变。V01/V02 尚未运行。
- [x] 14.4 [M08] 更新冲突表每项处置/新宿主/验收映射；原始 91 路径逐条映射，验证责任标明 V01/V02 pending，未冒称候选验收。证据 `evidence/M08/14.4-conflict-ownership-reconciliation.md`。
- [x] 14.5 [M08] 候选 `ba33fe26dc21ae3a308f14a61999009fc2849779` 结构验收通过：未解决索引/冲突标记/旧 wrapper 引用均为零；M00 双父 merge 与 M01–M07 的 42 个提交可追溯；91 路径与十个计费/OAuth 挂点均有候选证据。V01/V02 仍待执行。证据 `evidence/M08/14.5-source-acceptance.md`。

- [x] 14.6 [M08] 修复候选新增浏览器 spec `web/app/components/api-key/__tests__/api-key-modal-layout.browser.spec.tsx` 的格式问题；测试逻辑不变，exact-path formatter 通过，重跑该单一 Chromium spec 2/2，并记录新哈希。此修改产生新源码候选，随后重做 V02。

- [x] 14.7 [M08/Luna] 对 14.6 格式化后的精确 spec 哈希 `93d0239aae9fed0db61c5e2cecf793bacef67d1af7a81395369e16583759b120` 独立复核，只重跑该 Chromium 文件并核实 2/2；写 `evidence/M08/14.7-luna-verification.md`。

- [x] 14.8 [M08] 基于 M05 follow-up 与 14.6/14.7 完成状态，按显式路径提交 M05/M08 修复为新候选；在新候选复验结构（M00–M07 祖先、91 路径、10 个挂点、额度边界、无冲突/旧 wrapper），并记录候选 SHA/tree。V02 必须绑定新候选重跑。

- [x] 14.9 [M08/Astra+协调者] 根据 V02/16.6 已归档的 63 条精确格式路径（结果 JSON SHA-256 `09eef2b100176239cabf35820d99075d015bef93d1a65a0edde4b310f36b82f5`）运行仓库格式器；Astra 仅改 `.github/workflows/deploy-rag-dev.yml` 与两个 `packages/dify-ui/src/dialog` 路径，协调者负责其余 60 条及当前图/tasks/V01、16.5/16.6、14.8 证据。证明 JSON/YAML 数据和 Markdown 文义不变、源码差异仅格式；精确路径格式检查与 diff-check 通过。不得跑其他门禁、改功能/测试语义、安装、暂存或提交；完成后冻结新候选并重新做结构验收。 结果：通过，检查 68 条精确路径；报告 `evidence/M08/14.9-v02-formatter-debt-remediation.md`，逐文件哈希见 `evidence/M08/14.9-v02-formatter-path-manifest.json`。

- [x] 14.10 [M08/协调者] 按执行图 allowlist 冻结候选 `95101d76b955481ce6c9519596aeea426680fa71`（tree `ec530d435c3e8ac6697dcecb8f10cb1c584d42e6`）；73 条允许路径内有 71 条实际变化，2 条 TSX 字节未变。M00 双父与 M01–M07 42 个提交、91 个宿主、十个计费/OAuth 锚点、独立个人额度与 API Key 日/月限额均通过；API tree 与 V01 一致，复用 160/160。报告 `evidence/M08/14.10-final-candidate-structural-acceptance.md`。V02/16.7 绑定该 SHA。

- [x] 14.11 [M08/Astra] 修复上游配置 hook 拆分时丢失的 fork 记忆上下文保留轮数闭环：恢复默认 5/env 覆盖、AppDetail 回填、Context 状态/setter 与发布透传；测试覆盖已保存 12/999、缺省 5、env 覆盖 8、编辑值 17。新增配置回归 19/19，发布工具 16/16，六路径 scoped check 与 diff-check 通过。证据 `evidence/M08/14.11-retention-number-restoration.md` SHA-256 `4664b9821828fd4f11851edac98203c51ca68be4509e668cc988f6e864c02bc1`。
- [x] 14.12 [M08/Astra] 修复 `web/app/sw.ts` 中上游新 lint 规则触发的 disable 指令：在同一行 suppression 后补准确理由且不扩大作用域；单路径 `vp check` 与 diff-check 通过，无格式/lint/type 错误。证据 `evidence/M08/14.12-oxlint-disable-description.md`，源码 SHA-256 `895c12431f63000746027b46052a1b50748f00c89e9aad2845bd83f8106fcf39`，证据 SHA-256 `92fb83d7bd01b0465b1f97710773c048d8b8700d5594506df9b375c7c68ffd00`。
- [x] 14.13 [M08/Luna] 独立复核 M05/11.8、M06/12.7、M08/14.11、14.12 修复快照；四个定向 spec 一次运行通过，4 files / 88 tests；额度路径无改动、12 个源码路径均登记，无阻断。证据 `evidence/M08/14.13-independent-v02-fix-verification.md` SHA-256 `11f6ca94b26cc9bc8036073f94f0b647d06526ce19c485b5a7a656c0edd76138`。

- [x] 14.14 [M08/协调者] 按精确 commit_allowlist 提交 27 条登记路径，候选 `686a5cfa5614ea45b0d9973e52014b07c13b07e7` / tree `219f855a099258cbb712daae55e5a365390189ac`；M00 双父与旧候选 ancestry 保持，91/91 owner map 和 10 个锚点沿用已验收祖先，API 子树与 V01 SHA 一致，个人额度和 API Key 日/月边界未变。报告 `evidence/M08/14.14-recandidate-structural-acceptance.md` SHA-256 `5bf4c2813b239c39cff2b510a5d6b5984798fb9c8f393f94da075d60b4c5c0a4`。

- [x] 14.15 [M08/协调者] 格式化 V02/16.9 报告的 7 条 coordinator-owned 路径，以及 formatter precheck 额外发现的 `tasks.md`；刷新 M05/M08 证据哈希。固定 Node 24.20.0 下 exact-path `vp fmt --check` 12/12、`git diff --check` 通过；产品源码/测试路径未变化。证据 `evidence/M08/14.15-v02-evidence-format-followup.md` SHA-256 `378a8c08f7e39b1ffec66e8d433cd5ea12c2442e2da011f1b2b102031b2c4111`。
- [x] 14.16 [M08/协调者] 精确暂存并提交 13 路径 metadata-only 候选 `783859de3ebc098f2c9916516c831d92e547a913` / tree `badf6fb2196fe201185a1e81883504eb35cbffc0`；allowlist 完全匹配，产品源码/测试字节不变，M00/M01–M07 ancestry、91/91 冲突路径、十个计费/OAuth 锚点及 API 子树结构通过。报告 `evidence/M08/14.16-recandidate-structural-acceptance.md` SHA-256 `3b185d76f07d31bacb495883b551b115746a527f1842cc4ae79a2ac036c4e65b`。
- [x] 14.17 [M08/Astra] 修复 V02/16.10 发现的 `uk-UA` 翻译缺口：layout 2 keys、oauth 17 keys，共 19 条乌克兰语；JSON 与 `en-US` exact key parity 通过，定向 `i18n:check --file layout oauth --lang uk-UA` 沙箱外重试 exit 0。仅改两份 locale JSON 和报告；证据 `evidence/M08/14.17-ukrainian-locale-parity.md` SHA-256 `87c183df488a8e620f915e02d5c9a9cc0eea66bf15700fa4d127c9753da8e117`。
- [ ] 14.18 [M08/协调者] 在 14.17 通过后，按 `execution-graph.json` 的精确 `commit_allowlist` 暂存并提交新候选（直接子提交 `783859de`），完成 ancestry、91 路径冲突映射、冲突标记、API 子树及独立额度边界验收；不得暂存无关用户目录。候选 commit/tree、输入清单、SHA 与验收结果写入 `evidence/M08/14.18-final-candidate-structural-acceptance.md`；不声称 V02/生产通过。

## 15. V01 后端静态与定向回归（前置：M08）

- [x] 15.1 [V01] 读取 api/AGENTS.md；按合并后工具配置运行静态/导入和定向测试；核验：在 `evidence/V01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 15.2 [V01] 执行已随候选提交冻结的新注册/gateway/site/service_api/key/message/workflow/session/beat/retention 用例；需要修改源码或用例则退回对应 M 节点，更新候选后重验；核验：在 `evidence/V01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 15.3 [V01] 新增问题交还对应 M 节点修复，固定新 commit 后重验；核验：在 `evidence/V01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [x] 15.4 [V01] 节点验收：单测报告明确用例、commit、结果；已有债务与新增回归分开；无运行权限或依赖不可用记 blocked；保存绑定版本的证据并更新节点状态。

## 16. V02 前端检查、定向测试与双构建（前置：M08）

- [ ] 16.1 [V02] 在固定候选提交执行认证/新 access-point/api-key/应用中心用例；先核对实际收集到 browser 场景，零用例或路径失效不得记通过；若需改测试源码，退回 M05/M06 并经 M08 冻结新候选；核验：在 `evidence/V02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 16.2 [V02] 执行 frozen 安装、check/tss/i18n、unit/browser 和双构建；核验：在 `evidence/V02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 16.3 [V02] 新增场景必须实际进入新宿主，不能仅保留无人调用旧组件用例；核验：在 `evidence/V02/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 16.4 [V02] 节点验收：Node/pnpm 与锁文件一致，检查与双构建成功；定向认证与角色矩阵通过；24份extend可加载；保存绑定版本的证据并更新节点状态。

- [ ] 16.5 [V02/Luna] 在 `/private/tmp` 使用 `git archive` 建立仅含候选跟踪文件的临时源码快照，复用已安装依赖 symlink；锁与 Node/pnpm 哈希不变则引用首次 frozen-lock 结果。逐项跑全局 check、tss/i18n、unit、双构建；浏览器范围唯一 spec 复用 M08/14.7 精确哈希 2/2，不重复执行。首次失败即停，证据写到 `evidence/V02/16.5-final-candidate-result.json` 与日志。

- [ ] 16.6 [V02/Luna] 针对 16.5 在 pnpm 自动依赖验证生命周期内未能启动 `vp check` 的环境阻塞，新建候选 `97f94625d2d51fe1780f69aaecce5a56d3073db4` 的干净源码快照；固定 Node/pnpm 与锁哈希，使用 `pnpm --config.verify-deps-before-run=false check` 且不安装/同步依赖。若通过，继续 tss/i18n、登记的 unit 和双构建；首个失败即停，复用 M08/14.7 精确哈希浏览器结果。只写 `evidence/V02/16.6-final-candidate-result.json` 与日志，不改源码/测试、图或 tasks，不暂存/提交。 结果：首个 `vp check` 门禁执行成功启动，但 63 条 formatter 路径失败（15 条相对首父提交变更，48 条基线不变）；未运行后续 V02 门禁，证据见 `evidence/V02/16.6-final-candidate-result.json` 与日志。
- [ ] 16.7 [V02/Luna] 候选 `95101d76b955481ce6c9519596aeea426680fa71` 的 clean archive `pnpm check` 在 formatter 阶段被两个与父提交未变的 Dify UI 文件阻断；lint:tss、i18n、单测与构建按首失败规则停止。根因是快照仅链接 root/web node_modules，缺少已配置的 `packages/dify-ui/node_modules`；临时补齐该链接后同一精确 formatter 检查通过。保留结果与日志，详见 `evidence/V02/16.7-final-candidate-result.json`。
- [ ] 16.8 [V02/Luna] 对同一候选重试 clean archive V02，链接 root/web 与所有已存在 workspace package `node_modules`（跳过 web/.next 生成缓存），不安装、不同步、不联网；记录依赖 overlay 并先运行 `pnpm check`，首失败停止。尝试结果已记录：formatter 8,934 文件通过，lint:eslint 首失败为 12 errors / 1,931 warnings；归因发现 retention 功能回归、contract fixture/cache 类型问题及 lint suppression 描述问题；无后续门禁。证据 `evidence/V02/16.8-final-candidate-result.json` 与日志，待 M05/M08 修复并冻结新候选后重跑。
- [ ] 16.9 [V02/Luna] 已尝试候选 `686a5cfa5614ea45b0d9973e52014b07c13b07e7` / tree `219f855a099258cbb712daae55e5a365390189ac`；12 个依赖 overlay（含 SDK）及候选树校验通过。首门禁在 formatter 因 7 个 OpenSpec 状态/证据文件格式问题阻断，后续门禁未运行；报告 `evidence/V02/16.9-final-candidate-result.json`，log 同目录。M08/14.15、14.16 已通过，转由 16.10 在新候选重试。
- [ ] 16.10 [V02/Luna] 候选 `783859de3ebc098f2c9916516c831d92e547a913` / tree `badf6fb2196fe201185a1e81883504eb35cbffc0`：full workspace check 通过（8,945 文件 formatter；ESLint 0 errors / 1,931 warnings），`lint:tss` 6,266/6,266 通过。初始 i18n 进程受沙箱 Unix socket `EPERM` 阻断；同一归档解除该限制后检查器实际运行，发现 `uk-UA` layout/oauth 19 个 key 缺失，exit 1。按首失败规则，unit 与双构建未运行；结果/日志 `evidence/V02/16.10-final-candidate-result.json`、`.log`。
- [ ] 16.11 [V02/Luna] 在 M08/14.17 翻译修复、14.18 新候选结构验收通过后，对新 commit/tree 重跑完整 clean-archive V02；包括 12 项现有 workspace overlay 和 SDK，不安装/同步/联网，首失败即停。复用仍匹配的 V01 160/160 与浏览器 2/2，只写 `evidence/V02/16.11-final-candidate-result.json` 与 log。

## 17. R01 源码合并候选验收（前置：V01, V02）

- [ ] 17.1 [R01] 汇总全部冲突/语义/静态测试记录；核验：在 `evidence/R01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 17.2 [R01] 记录候选 commit 与待环境验证项；不得此时声称已上线；核验：在 `evidence/R01/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 17.3 [R01] 节点验收：源码候选可构建，阻断级新增回归清零；状态明确为 code-ready，仍须环境链；保存绑定版本的证据并更新节点状态。

## 18. V03 目标镜像、空库与存量双链演练（前置：R01, A03）

- [ ] 18.1 [V03] 先准备环境专属 override/备份恢复命令草案；构建固定候选 fork 镜像并核对 digest、平台与供货，把实际 digest 固定到隔离 override，复核无生产存储/队列连接后才开始演练；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.2 [V03] 空库从零两链；实际旧库副本主链再extend，记录17迁移与两head；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.3 [V03] 审计Agent删表/JSON、normalized email、模型去重/凭据引用与可解密；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.4 [V03] 实际生产DB引擎必测；声称同时支持PG/MySQL则两者均测；核验：在 `evidence/V03/result.json` 附该步骤实际输入、输出及结果，不以规划代替完成。
- [ ] 18.5 [V03] 节点验收：双head为 c3f1a9b2e6d4 / 020_workflow_run_account；数据差异符合预审、破坏性数据有处置决定、耗时记录；保存绑定版本的证据并更新节点状态。

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
